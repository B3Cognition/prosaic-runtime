"""Native CLI command projection and conservative terminal-event normalization."""
import json
import math
from pathlib import Path
import tempfile

from .native_process import run_process
from .types import Result


_LIMITS = {'rate_limit': 'rate_limit', 'rate_limit_error': 'rate_limit',
           'rate_limit_exceeded': 'rate_limit', 'usage_limit_reached': 'usage_limit',
           'usage_limit': 'usage_limit', 'overloaded_error': 'unavailable',
           'overloaded': 'unavailable', 'server_overloaded': 'unavailable'}


def _number(value):
    return not isinstance(value, bool) and isinstance(value, (float, int)) and math.isfinite(value) and value >= 0


def _usage(value, backend):
    if not isinstance(value, dict):
        return None
    fields = ('input_tokens', 'output_tokens')
    if any(type(value.get(key)) is not int or value[key] < 0 for key in fields):
        return None
    total = sum(value[key] for key in fields)
    if backend == 'claude-code':
        for key in ('cache_read_input_tokens', 'cache_creation_input_tokens'):
            if type(value.get(key, 0)) is not int or value.get(key, 0) < 0:
                return None
            total += value.get(key, 0)
    return total


class NativeEvents:
    def __init__(self, backend, on_event=None):
        self.backend = backend
        self.on_event = on_event
        self.terminal = False
        self.success = False
        self.invalid = False
        self.effects = False
        self.work_started = False
        self.failure = None
        self.retry_at = None
        self.text = ''
        self.usage = None
        self.session_id = None
        self.zero_turns = False

    def _error(self, error):
        if isinstance(error, dict):
            code = error.get('code', error.get('type'))
            if code == 'error':
                code = None  # Top-level exec envelope, not a provider error code.
            if code is None and self.backend == 'codex-cli':
                message = error.get('message', '')
                # Exec's documented error envelope exposes message only. Match
                # provider error prefixes, never agent prose or arbitrary stderr.
                if isinstance(message, str):
                    if message.startswith(("You've hit your usage limit", 'Your workspace is out of credits.',
                                           'You hit your spend cap')):
                        code = 'usage_limit'
                    elif message.startswith(('Rate limit reached', 'Rate limit exceeded', '429 Too Many Requests')):
                        code = 'rate_limit'
        else:
            code = error
        if isinstance(code, str):
            normalized = _LIMITS.get(code, code if code in {
                'authentication_failed', 'authentication_error', 'billing_error', 'permission_denied'
            } else 'provider_failure')
            if self.failure is None or self.failure in {'usage_limit', 'rate_limit', 'unavailable'}:
                self.failure = normalized

    def __call__(self, event):
        if self.terminal:
            self.invalid = True
            return
        kind = event['type']
        if self.backend == 'claude-code':
            self._claude(kind, event)
        else:
            self._codex(kind, event)
        if self.on_event:
            # Raw events may contain prompts, commands, credentials or file text.
            self.on_event({'event': 'native_progress', 'backend': self.backend, 'type': kind})

    def _claude(self, kind, event):
        if kind == 'result':
            self.terminal = True
            if type(event.get('is_error')) is not bool or (
                    event.get('is_error') is False and event.get('subtype') != 'success'):
                self.invalid = True
            self.success = (event.get('subtype') == 'success' and event.get('is_error') is False
                            and isinstance(event.get('result'), str))
            self.text = event.get('result', '') if isinstance(event.get('result', ''), str) else ''
            self.session_id = event.get('session_id', self.session_id)
            self.usage = _usage(event.get('usage'), self.backend)
            self.zero_turns = type(event.get('num_turns')) is int and event['num_turns'] == 0
            if event.get('error'):
                self._error(event['error'])
        elif kind == 'rate_limit_event':
            info = event.get('rate_limit_info', event.get('rateLimitInfo', {}))
            if not isinstance(info, dict):
                self.invalid = True
                return
            if info.get('status') == 'rejected':
                self._error('usage_limit')
                reset = info.get('resetsAt', info.get('resets_at'))
                if _number(reset):
                    self.retry_at = reset
        elif kind == 'assistant':
            if event.get('error'):
                self._error(event['error'])
            message = event.get('message', {})
            content = message.get('content') if isinstance(message, dict) else None
            if not isinstance(content, list):
                self.effects = True
            else:
                for block in content:
                    if not isinstance(block, dict):
                        self.invalid = True
                    elif block.get('type') == 'tool_use':
                        self.effects = True
                        self.work_started = True
                    elif block.get('type') == 'text' and isinstance(block.get('text'), str):
                        self.work_started |= bool(block['text'])
                    elif block.get('type') == 'thinking' and isinstance(block.get('thinking'), str):
                        self.work_started |= bool(block['thinking'])
                    elif block.get('type') == 'redacted_thinking' and isinstance(block.get('data'), str):
                        pass
                    else:
                        self.invalid = True
        elif kind == 'system' and event.get('subtype') == 'init':
            self.session_id = event.get('session_id')
        elif kind == 'stream_event':
            inner = event.get('event', {})
            if not isinstance(inner, dict):
                self.invalid = True
                return
            if inner.get('type') not in {'message_start', 'message_delta', 'message_stop',
                    'content_block_start', 'content_block_delta', 'content_block_stop'}:
                self.invalid = True
            block = inner.get('content_block', {})
            if not isinstance(block, dict):
                self.invalid = True
            elif block.get('type') == 'tool_use':
                self.effects = True
            if (isinstance(block, dict) and block.get('type') in {'text', 'thinking', 'tool_use'}
                    or isinstance(inner.get('delta'), dict) and inner['delta'].get('type') in {'text_delta', 'thinking_delta'}):
                self.work_started = True
        else:
            # User/tool results, hook activity and future protocol extensions
            # cannot prove that retrying would be effect-free.
            self.effects = True

    def _codex(self, kind, event):
        if kind == 'thread.started':
            self.session_id = event.get('thread_id')
        elif kind == 'turn.completed':
            self.terminal = True
            self.success = True
            self.usage = _usage(event.get('usage'), self.backend)
        elif kind == 'turn.failed':
            self.terminal = True
            self._error(event.get('error'))
            self.zero_turns = True
        elif kind == 'error':
            self._error(event.get('error', event))
        elif kind in {'item.started', 'item.updated', 'item.completed'}:
            item = event.get('item', {})
            if not isinstance(item, dict):
                self.invalid = True
                return
            self.work_started = True
            if item.get('type') == 'agent_message':
                if kind == 'item.completed':
                    if isinstance(item.get('text'), str):
                        self.text = item['text']
                    else:
                        self.invalid = True
            elif item.get('type') != 'reasoning':
                self.effects = True
        elif kind != 'turn.started':
            self.effects = True


def native_command(profile, policy, *, mcp_file):
    """Final flags constrain native settings; never allow an arbitrary argv tail."""
    if profile.backend == 'claude-code':
        tools = 'Read,Glob,Grep'
        if policy.permission_mode == 'workspace-write':
            tools += ',Edit,Write,Bash'
        argv = [profile.executable, '--restricted', '-p', '--output-format', 'stream-json',
                '--verbose', '--model', profile.model, '--permission-mode', 'dontAsk',
                '--tools', tools, '--allowedTools', tools, '--disable-slash-commands',
                '--strict-mcp-config', '--mcp-config', str(mcp_file)]
        if profile.settings_file:
            argv += ['--settings', profile.settings_file]
        if profile.effort:
            argv += ['--effort', profile.effort]
        # Close variadic flags; prompts are always delivered through stdin.
        argv += ['--']
    else:
        argv = [profile.executable]
        if profile.native_profile:
            argv += ['--profile', profile.native_profile]
        argv += ['--ask-for-approval', 'never', 'exec', '--json', '--ephemeral',
                 '--model', profile.model, '--sandbox', policy.permission_mode,
                 '-c', 'approval_policy="never"',
                 '-c', 'sandbox_workspace_write.network_access=false']
        if profile.effort:
            argv += ['-c', 'model_reasoning_effort=' + json.dumps(profile.effort)]
        argv += ['-']
    return argv


def execute_native(profile, prompt, *, cwd, policy, deadline, env, on_event=None, cancelled=None):
    events = NativeEvents(profile.backend, on_event)
    invocation_env = dict(env)
    if profile.config_dir:
        invocation_env['CLAUDE_CONFIG_DIR' if profile.backend == 'claude-code' else 'CODEX_HOME'] = profile.config_dir
    with tempfile.TemporaryDirectory(prefix='prosaic-native-') as temporary:
        mcp = Path(temporary) / 'mcp.json'
        mcp.write_text('{"mcpServers": {}}', encoding='utf-8')
        outcome = run_process(native_command(profile, policy, mcp_file=mcp), prompt,
            cwd=cwd, env=invocation_env, deadline=deadline, max_output_bytes=policy.max_output_bytes,
            on_message=events, cancelled=cancelled)
    reason = outcome.failure
    if not reason:
        reason = ('malformed_output' if events.invalid else
                  'incomplete_response' if not events.terminal else
                  events.failure if not events.success or events.failure not in {
                      None, 'usage_limit', 'rate_limit', 'unavailable'} else None)
    successful = not reason and events.success and outcome.returncode == 0
    if not successful and not reason:
        reason = 'provider_failure'
    safe = (not outcome.launched and reason == 'unavailable') or (
        outcome.failure is None and not events.invalid and events.terminal
        and not events.success and events.zero_turns and not events.effects
        and reason in {'usage_limit', 'rate_limit', 'unavailable'})
    metadata = {'backend': profile.backend, 'model': profile.model,
        'work_started': events.work_started,
        'permission_owner': 'native', 'safe_to_fallback': safe,
        'effects_uncertain': outcome.launched and (events.effects or not successful and not safe),
        'token_usage_status': 'reported' if events.usage is not None else 'unknown',
        'cost_status': 'unavailable'}
    if not outcome.launched:
        events.usage = 0
        metadata['token_usage_status'] = 'reported'
    if reason:
        metadata['failure_reason'] = reason
    if isinstance(events.session_id, str):
        metadata['session_id'] = events.session_id
    if events.retry_at is not None:
        metadata['retry_at'] = events.retry_at
    return Result(0 if successful else 130 if reason == 'cancelled' else 1,
        events.text if successful else '', outcome.stderr or (reason or ''),
        token_usage=events.usage, timed_out=reason == 'timeout', metadata=metadata)
