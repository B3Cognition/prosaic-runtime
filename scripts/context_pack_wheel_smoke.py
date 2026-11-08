"""Installed public SDK acceptance; synthetic loopback inference only."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from importlib import metadata
import hashlib
import json
from pathlib import Path
import sys
from threading import Thread


def installed_origins():
    import prosaic
    import prosaic_runtime
    import prosaic_runtime_postgres
    origins={}
    for module,distribution in ((prosaic,'b3-prosaic'),(prosaic_runtime,'b3-prosaic-runtime'),
            (prosaic_runtime_postgres,'b3-prosaic-runtime-postgres')):
        owner=metadata.distribution(distribution)
        assert module.__version__==owner.version
        assert metadata.packages_distributions()[module.__name__]==[distribution]
        files={Path(owner.locate_file(f)).resolve() for f in owner.files}
        for name,loaded in tuple(sys.modules.items()):
            if name.split('.')[0]==module.__name__ and getattr(loaded,'__file__',None):
                origin=Path(loaded.__file__).resolve()
                assert 'site-packages' in origin.parts and origin in files,(name,str(origin))
        direct=owner.read_text('direct_url.json')
        assert direct is None or not json.loads(direct).get('dir_info',{}).get('editable',False)
        origins[module.__name__]={'version':owner.version,'path':str(Path(module.__file__).resolve())}
    assert origins['prosaic']['version']=='0.5.0'
    assert origins['prosaic_runtime_postgres']['version']=='0.3.0'
    return origins


@contextmanager
def endpoint(provider):
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw=self.rfile.read(int(self.headers['Content-Length']));requests.append(raw)
            if provider=='anthropic':
                body={'id':'synthetic','type':'message','role':'assistant','model':'synthetic-context-pack',
                    'content':[{'type':'text','text':'Checked answer'}],'stop_reason':'end_turn',
                    'usage':{'input_tokens':2,'output_tokens':3}}
            else:
                body={'choices':[{'message':{'role':'assistant','content':'Checked answer'},'finish_reason':'stop'}],
                    'usage':{'prompt_tokens':2,'completion_tokens':3,'total_tokens':5}}
            encoded=json.dumps(body).encode();self.send_response(200)
            self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(encoded)))
            self.end_headers();self.wfile.write(encoded)
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try: yield f'http://127.0.0.1:{server.server_port}/v1',requests
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
        assert not thread.is_alive()


def exercise(provider):
    import prosaic_runtime as api
    counts=[];checks=[];events=[];checkpoints=[]
    class Counter:
        def count_tokens(self,request):
            counts.append(request)
            return 9999 if 'OPTIONAL-CONTEXT-719' in json.dumps(request) else 10
    descriptor=api.TokenCounterDescriptor('synthetic-pack-v1',provider,'synthetic-context-pack',
        'anthropic-messages-v1' if provider=='anthropic' else 'openai-chat-v1',4096,'1','0'*64,'1'*64,
        'exact','Synthetic fixed fixture grammar only.')
    counters=api.TokenCounterRegistry({descriptor.identity:api.TokenCounterRegistration(descriptor,Counter())})
    context_policy=api.ContextPolicy('fullHistory',4096,128,0,descriptor.identity)
    def check(stage,context):
        assert context.authoritative_input_data=={'private':'PRIVATE-VALIDATION-884','allFacts':[14,42]}
        assert 'PRIVATE-VALIDATION-884' not in json.dumps(context.input_data)
        checks.append(stage)
        return api.Verdict(True)
    guard=api.Guard('facts','1',('final',),check,input_access='authoritativeAndModel')
    agent=api.ProsaicArtifact.from_inspection({'id':'pack.md','type':'subagent',
        'frontmatter':{'name':'pack-smoke','description':'Bounded selected context'},
        'body':'ALWAYS validate captured facts. <!-- enforce: facts(stage=final, on_violation=fail) -->\nNEVER invent facts.'})
    with endpoint(provider) as (url,requests):
        config=api.RuntimeConfig({'local':api.EndpointConfig(url,'synthetic-context-pack',provider=provider,
            max_tokens=128,features={'streaming':False})},{},'local',frozenset())
        runtime=api.ProsaicRuntime(config,guards=[guard])
        assert {'guard_input_views_v1','frozen_turn_inputs_v1','context_input_variants_v1'}<=runtime.capabilities
        policy=api.RunPolicy(max_provider_requests=1,max_tool_calls=0)
        limits=api.TurnLimits(max_metadata_bytes=16384,max_evidence_bytes=16384)
        options=dict(message='Exact current question',policy=policy,limits=limits,input_profile='pack-smoke-v1',
            authoritative_input_data={'private':'PRIVATE-VALIDATION-884','allFacts':[14,42]},
            authoritative_input_profile='facts-smoke-v1')
        prepared=runtime.prepare_turn_variants(agent,input_data_variants=[{'task':'Review','optional':'OPTIONAL-CONTEXT-719'},
            {'task':'Review'}],context_policy=context_policy,token_counters=counters,**options)
        assert prepared.input_variant_selection.to_dict()['selectedIndex']==1
        assert prepared.selected_input_data=={'task':'Review'}
        frozen=prepared.export_frozen_input();before=len(counts)
        restored=runtime.restore_turn(agent,frozen_input=frozen,policy=policy,limits=limits,
            context_policy=context_policy,token_counters=counters)
        assert len(counts)==before and checks==[] and requests==[]
        turn=runtime.turn(restored,token_counters=counters,on_event=events.append,
            context_checkpoint=lambda work:checkpoints.append(work.to_dict()))
        assert turn.result.exit_code==0 and len(checks)==1
        assert checkpoints[0]['reservedActiveMicros']>0 and checkpoints[-1]['reservedActiveMicros']==0
        assert turn.evidence.to_dict()['inputViews']==frozen.to_dict()['inputViews']
        assert hashlib.sha256(requests[0]).hexdigest()==frozen.to_dict()['nativePayloadDigest']
        assert 'OPTIONAL-CONTEXT-719' not in requests[0].decode()
        assert 'PRIVATE-VALIDATION-884' not in json.dumps([events,turn.completed_turn.to_dict()])
        byte_turn=runtime.prepare_turn(agent,input_data={'task':'Byte-only review'},**options)
        byte_frozen=byte_turn.export_frozen_input()
        byte_restored=runtime.restore_turn(agent,frozen_input=byte_frozen,policy=policy,limits=limits,fresh_attempt=True)
        assert byte_restored.context_work is None and byte_restored.export_frozen_input().to_dict()['executionAttempt']==2
        assert runtime.turn(byte_restored).result.exit_code==0 and len(checks)==2 and len(requests)==2
        return {'provider':provider,'requests':len(requests),'selectedIndex':1,'counterCalls':len(counts),
            'wireSha256':hashlib.sha256(requests[0]).hexdigest()}


def main():
    assert sys.flags.isolated,'run installed acceptance with python -I'
    providers=[exercise(provider) for provider in ('openai-compatible','anthropic')]
    print(json.dumps({'version':1,'origins':installed_origins(),'providers':providers},sort_keys=True))


if __name__=='__main__': main()
