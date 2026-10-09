"""Per-request capture independent of diagnostic sinks and cancellation."""
import json
import uuid
from .accounting import AccountingError, normalize_usage, utc_now


class InvalidEvidence(ValueError):
    pass


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise InvalidEvidence('duplicate response key')
            result[key] = value
        return result
    def constant(value):
        raise InvalidEvidence('nonfinite response value')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


class Capture:
    def __init__(self, recorder, context, artifact, profile, provider='openai-compatible'):
        self.recorder, self.context = recorder, context
        self.artifact, self.profile = artifact, profile
        self.call_ids = []
        self.provider = provider

    def open(self, opener, request, timeout):
        call_id = uuid.uuid4().hex
        payload = json.loads(request.data)
        card = self.recorder.rate_card
        intent = {'version': 1, 'provider_call_id': call_id,
                  'namespace': self.recorder.namespace, 'environment': self.recorder.environment,
                  'context': self.context.to_dict(), 'artifact_id': self.artifact.id,
                  'artifact_sha256': self.artifact.digest, 'profile': self.profile,
                  'provider_id': self.recorder.provider_id, 'credential_account': self.recorder.credential_account,
                  'requested_model': payload['model'], 'started_at': utc_now(),
                  'rate_card': card.to_dict() if card else None}
        try:
            self.recorder.prepare(intent)
        except Exception as exc:
            raise AccountingError('intent persistence failed') from exc
        self.call_ids.append(call_id)
        observer = AnthropicObservation if self.provider == 'anthropic' else Observation
        observation = observer(self.recorder, call_id)
        try:
            response = opener.open(request, timeout=timeout)
        except BaseException:
            observation.finish('failed')
            raise
        return RecordedResponse(response, observation)


class Observation:
    eager_events = True
    def __init__(self, recorder, call_id):
        self.recorder, self.call_id = recorder, call_id
        self.usage, self.model, self.request_id, self.tier = None, None, None, None
        self.conflict = False
        self.finished = False
        self.terminal = False

    def capture(self, event):
        if type(event) is not dict:
            return
        choices = event.get('choices')
        if type(choices) is list and any(type(choice) is dict and choice.get('finish_reason') in {'stop', 'tool_calls', 'length', 'content_filter'} for choice in choices):
            self.terminal = True
        for key, attr in (('model', 'model'), ('id', 'request_id'), ('service_tier', 'tier')):
            value = event.get(key)
            if type(value) is str and len(value) <= 256:
                previous = getattr(self, attr)
                if previous is not None and previous != value:
                    self.conflict = True
                setattr(self, attr, value)
        if event.get('usage') is not None:
            usage = normalize_usage(event['usage'])
            # Repeated snapshots are not deltas. Conflicting nonidentical usage
            # is quarantined rather than choosing a potentially wrong bill.
            if self.usage is not None and self.usage != usage:
                self.conflict = True
            self.usage = usage

    def finish(self, outcome):
        if self.finished:
            return
        usage = self.usage or normalize_usage(None)
        if self.conflict:
            usage = {**usage, 'status': 'untrusted'}
        elif usage['status'] == 'reported' and not self.terminal:
            usage = {**usage, 'status': 'partial'}
        value = {'version': 1, 'provider_call_id': self.call_id,
                 'finished_at': utc_now(), 'outcome': outcome,
                 'response_model': self.model, 'provider_request_id': self.request_id,
                 'service_tier': self.tier, 'usage': usage}
        try:
            self.recorder.observe(self.call_id, value)
        except Exception as exc:
            raise AccountingError('observation persistence failed') from exc
        self.finished = True


class AnthropicObservation(Observation):
    eager_events = False

    def __init__(self, recorder, call_id):
        super().__init__(recorder, call_id)
        from .anthropic_stream import StreamState
        self.state = StreamState(lambda text: None)

    def capture(self, event):
        from .anthropic import parse_message
        from .anthropic_usage import normalize_anthropic_usage
        if type(event) is not dict:
            self.conflict = True
            return
        if event.get('type') == 'message':
            message = event
            self.usage = normalize_anthropic_usage(message.get('usage'))
            try:
                parse_message(message)
                self.terminal = message.get('stop_reason') in {
                    'end_turn', 'stop_sequence', 'tool_use', 'max_tokens', 'refusal', 'pause_turn'}
            except (ValueError, TypeError, RecursionError):
                self.conflict = True
        else:
            try:
                self.state.push(event)
                if self.state.stopped:
                    parse_message(self.state.message)
                    self.terminal = True
            except (ValueError, TypeError, RecursionError):
                self.conflict = True
            message = self.state.message or {}
            self.usage = self.state.usage.normalized()
            self.conflict |= self.state.usage.conflict
        for key, attr in (('model', 'model'), ('id', 'request_id')):
            value = message.get(key)
            if type(value) is str and 0 < len(value) <= 256:
                previous = getattr(self, attr)
                if previous is not None and previous != value:
                    self.conflict = True
                setattr(self, attr, value)
            elif key in message:
                self.conflict = True
        raw_usage = message.get('usage')
        if isinstance(raw_usage, dict) and raw_usage.get('service_tier') is not None:
            tier = raw_usage['service_tier']
            tier = 'default' if tier == 'standard' else tier
            if not isinstance(tier, str) or len(tier) > 256 or (self.tier is not None and self.tier != tier):
                self.conflict = True
            else:
                self.tier = tier


class RecordedResponse:
    def __init__(self, response, observation):
        self.response, self.observation = response, observation
        self.data = []
        self.event_name = None
        self.body = bytearray()

    def __getattr__(self, name):
        return getattr(self.response, name)

    def __enter__(self):
        self.response.__enter__()
        return self

    def __exit__(self, typ, value, traceback):
        try:
            if self.body:
                self._capture_body(bytes(self.body))
                self.body.clear()
            if not self.observation.eager_events and (self.data or self.event_name is not None):
                self.observation.conflict = True
            self._flush()
            self.observation.finish('cancelled' if typ and typ.__name__ in {'Cancelled', 'KeyboardInterrupt'}
                                    else 'failed' if typ or not self.observation.terminal else 'completed')
        finally:
            return_value = self.response.__exit__(typ, value, traceback)
        return return_value

    def _flush(self):
        if self.data:
            try:
                event = strict_json('\n'.join(self.data))
                if self.event_name is not None and (not isinstance(event, dict) or event.get('type') != self.event_name):
                    self.observation.conflict = True
                self.observation.capture(event)
            except (ValueError, TypeError, RecursionError):
                self.observation.conflict = True
            self.data = []
        elif self.event_name is not None:
            self.observation.conflict = True
        self.event_name = None

    def _line(self, raw):
        if self.observation.eager_events:
            line = raw.decode('utf-8', errors='replace').strip()
        else:
            try:
                line = raw.decode('utf-8', errors='strict').rstrip('\r\n')
            except UnicodeError:
                self.observation.conflict = True
                return
        if not line:
            self._flush()
        elif not self.observation.eager_events and line.startswith('event:'):
            if self.event_name is not None:
                self.observation.conflict = True
            self.event_name = line[6:].lstrip()
        elif line.startswith('data:'):
            value = line[5:].lstrip()
            if value == '[DONE]' and self.observation.eager_events:
                self._flush()
            else:
                self.data.append(value)
                if not self.observation.eager_events:
                    return
                # Capture before the normal parser can emit a text callback
                # that requests cancellation. Multi-line JSON remains buffered.
                try:
                    event = strict_json('\n'.join(self.data))
                except InvalidEvidence:
                    self.observation.conflict = True
                    self.data = []
                    return
                except ValueError:
                    return
                self.observation.capture(event)
                self.data = []

    def readline(self, *args):
        raw = self.response.readline(*args)
        self._line(raw)
        return raw

    def read(self, *args):
        raw = self.response.read(*args)
        self._capture_body(raw)
        return raw

    def read1(self, *args):
        raw = self.response.read1(*args)
        self.body.extend(raw)
        if not raw:
            self._capture_body(bytes(self.body))
            self.body.clear()
        return raw

    def _capture_body(self, raw):
        try:
            self.observation.capture(strict_json(raw))
        except InvalidEvidence:
            self.observation.conflict = True
        except RecursionError:
            self.observation.conflict = True
        except (ValueError, TypeError):
            for line in raw.splitlines():
                self._line(line)
            self._flush()
