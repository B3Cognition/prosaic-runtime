"""Real HTTP boundary checks: diagnostics never admit, repair or retry replies."""
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Event, Thread
import time

import pytest
from prosaic_runtime import EndpointConfig, RunPolicy
from prosaic_runtime.accounting import MemoryRecorder
from test_runtime import artifact, runtime

BODY = b'{"id":"fixture-response","model":"test","choices":[{"message":{"content":"done"},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}'


@contextmanager
def wire_server(mode='fixed', body=BODY, *, warning=None, request_id='sk-response-secret', status=200, request_id_header='X-Request-ID'):
    requests=[]; release=Event()
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def do_POST(self):
            requests.append(dict(headers=dict(self.headers),body=self.rfile.read(int(self.headers['Content-Length']))))
            if mode=='headers-stalled':
                release.wait(2);self.close_connection=True;return
            self.send_response(status)
            self.send_header('Content-Type','text/event-stream' if mode=='sse' else 'application/json' if mode!='hostile' else 'Bearer sk-header-secret')
            self.send_header('Set-Cookie','sk-cookie-secret')
            self.send_header(request_id_header,request_id)
            if warning is not None:self.send_header('Warning',warning)
            self.send_header('Connection','close')
            if mode=='chunked':self.send_header('Transfer-Encoding','chunked')
            elif mode!='hostile':self.send_header('Content-Length',str(len(body)+(20 if mode=='truncated' else 0)))
            self.end_headers()
            try:
                if mode=='stalled':
                    self.wfile.write(body[:1]);self.wfile.flush();release.wait(2)
                elif mode=='keepalive':
                    for _ in range(40):
                        self.wfile.write(b' ');self.wfile.flush()
                        if release.wait(.05):break
                elif mode=='chunked':
                    for part in [body[:11],body[11:]]:self.wfile.write(f'{len(part):x}\r\n'.encode()+part+b'\r\n')
                    self.wfile.write(b'0\r\n\r\n');self.wfile.flush()
                else:self.wfile.write(body);self.wfile.flush()
            except (BrokenPipeError,ConnectionResetError):pass
            self.close_connection=True
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield f'http://127.0.0.1:{server.server_port}/v1',requests
    finally:release.set();server.shutdown();thread.join(2);server.server_close()


def run_wire(mode='fixed',body=BODY,*,enabled=True,timeout=2,recorder=None,observer=None,max_bytes=8388608,streaming=False,warning=None,request_id='sk-response-secret',schema=None,status=200,request_id_header='X-Request-ID',max_reported_tokens=None):
    events=[];recorder=recorder or MemoryRecorder()
    def observe(value):
        events.append(value)
        if observer:observer(value)
    with wire_server(mode,body,warning=warning,request_id=request_id,status=status,request_id_header=request_id_header) as (url,requests):
        result=runtime(EndpointConfig(url,'test',features={'streaming':streaming,'transport_diagnostics':enabled},max_response_bytes=max_bytes),accounting=recorder).run(
            artifact(),observer=observe,response_schema=schema,policy=RunPolicy(timeout_s=timeout,max_provider_requests=1,max_tool_calls=0,
                max_reported_tokens=max_reported_tokens))
    return result,events,recorder,requests


def trace(events):
    completions=[e for e in events if e['event']=='provider_request_completed'];assert len(completions)==1
    assert 'transport' in completions[0], 'opt-in HTTP evidence missing'
    return completions[0]['transport']


@pytest.mark.parametrize('mode',['fixed','chunked'])
def test_opt_in_traces_exact_received_bytes_and_host_call_id_without_rewriting(mode):
    result,events,rec,requests=run_wire(mode)
    assert result.exit_code==0 and result.stdout=='done' and result.token_usage==7
    assert len(requests)==1 and len(rec.intents)==len(rec.observations)==1
    t=trace(events)
    assert t['correlation_id']==next(iter(rec.intents))==requests[0]['headers']['X-Correlation-Id']
    assert t['body_bytes']==len(BODY) and t['body_sha256']==hashlib.sha256(BODY).hexdigest() and t['eof'] is True
    assert t['content_length']==(len(BODY) if mode=='fixed' else None)
    assert t['transfer_encoding']==('chunked' if mode=='chunked' else 'absent')
    assert 0<=t['headers_ms']<=t['first_byte_ms']<=t['last_byte_ms']<=t['completed_ms']
    assert t['read_calls']>=1 and t['exception_stage'] is None
    assert all(secret not in json.dumps(events) for secret in ['sk-response-secret','sk-cookie-secret','fixture-response','"content":"done"'])


def test_default_and_nonboolean_configuration_do_not_enable_diagnostics():
    for flag in [False,'true',1,None]:
        result,events,_,_=run_wire(enabled=flag)
        assert result.exit_code==0 and all('transport' not in e for e in events)


def test_untrusted_framing_headers_are_reduced_to_closed_enums():
    result,events,_,_=run_wire('hostile');assert result.exit_code==0
    t=trace(events);assert t['content_type']=='other' and t['connection']=='close'
    assert 'sk-' not in json.dumps(events) and t['content_length'] is None


def test_incomplete_json_never_becomes_known_usage_even_with_transport_eof():
    result,events,rec,requests=run_wire(body=b'{')
    t=trace(events);assert t['body_bytes']==1 and t['eof'] is True
    assert result.exit_code!=0 and len(requests)==1
    assert next(iter(rec.observations.values()))['usage']['status']=='unknown'


def test_partial_body_timeout_retains_unknown_usage_and_deadline():
    start=time.monotonic();result,events,rec,requests=run_wire('stalled',timeout=.25)
    assert result.timed_out and result.exit_code!=0 and time.monotonic()-start<1.5
    assert len(requests)==1 and next(iter(rec.observations.values()))['usage']['status']=='unknown'
    t=trace(events);assert t['body_bytes']==1 and t['body_sha256']==hashlib.sha256(BODY[:1]).hexdigest()
    assert t['eof'] is False and t['exception_stage']=='body' and t['exception_kind']=='timeout'


def test_truncated_invalid_body_is_observed_but_not_repaired():
    result,events,rec,requests=run_wire('truncated',b'{')
    assert result.exit_code!=0 and len(requests)==1 and next(iter(rec.observations.values()))['usage']['status']=='unknown'
    t=trace(events);assert t['body_bytes']==1 and t['content_length']==21


def test_response_size_cap_still_rejects_and_trace_never_captures_body_text():
    result,events,_,requests=run_wire(max_bytes=30)
    assert result.exit_code!=0 and len(requests)==1
    t=trace(events);assert t['body_bytes']==31 and t['eof'] is False
    assert t['body_sha256']==hashlib.sha256(BODY[:31]).hexdigest()


def test_observer_failure_cannot_change_successful_usage():
    def fail(_):raise RuntimeError('sk-observer-secret')
    result,events,rec,requests=run_wire(observer=fail)
    assert result.exit_code==0 and result.token_usage==7 and len(requests)==1
    assert next(iter(rec.observations.values()))['usage']['total_tokens']==7
    assert trace(events)['eof'] is True and 'sk-observer-secret' not in json.dumps(events)


def test_persistence_failure_remains_failure_even_when_http_completed():
    class BrokenRecorder(MemoryRecorder):
        def observe(self,call_id,value):raise RuntimeError('sk-database-secret')
    result,events,rec,requests=run_wire(recorder=BrokenRecorder())
    assert result.exit_code!=0 and len(requests)==len(rec.intents)==1 and not rec.observations
    t=trace(events);assert t['eof'] is True and t['body_bytes']==len(BODY)
    assert 'sk-database-secret' not in json.dumps(events)


def test_header_timeout_is_distinct_from_body_timeout_without_a_retry():
    result,events,rec,requests=run_wire('headers-stalled',timeout=.25)
    assert result.timed_out and len(requests)==len(rec.intents)==1
    assert next(iter(rec.observations.values()))['usage']['status']=='unknown'
    t=trace(events);assert t['headers_ms'] is None and t['first_byte_ms'] is None and t['body_bytes']==0
    assert t['exception_stage']=='headers' and t['exception_kind']=='timeout'


def test_streaming_keeps_accounting_and_counts_wire_body_once():
    body=b'data: {"id":"stream","model":"test","choices":[{"delta":{"content":"done"},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}\n\ndata: [DONE]\n\n'
    result,events,rec,requests=run_wire('sse',body,streaming=True)
    assert result.exit_code==0 and result.stdout=='done' and result.token_usage==7 and len(requests)==1
    t=trace(events);assert t['body_bytes']==len(body) and t['body_sha256']==hashlib.sha256(body).hexdigest() and t['content_type']=='sse'
    assert next(iter(rec.observations.values()))['usage']['total_tokens']==7


@pytest.mark.parametrize('injection',[{'body_bytes':True},{'body_bytes':float('nan')},{'correlation_id':'sk-injected-secret'},{'headers':'sk-injected-secret'},{'exception_kind':'sk-injected-secret'},{'schema_enforcement':'sk-injected-secret'},{'server_request_id_sha256':'sk-injected-secret'},{'warning_present':'true'}])
def test_observer_boundary_rejects_injected_transport_values(injection):
    from prosaic_runtime import InvocationScope
    from prosaic_runtime.telemetry import ObserverEmitter
    _,events,_,_=run_wire();value={**trace(events),**injection};records=[]
    ObserverEmitter(records.append,source='runtime',scope=InvocationScope('safe')).emit('provider_request_completed',transport=value)
    assert len(records)==1 and 'transport' not in records[0] and 'sk-' not in json.dumps(records)
