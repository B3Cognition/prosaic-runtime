"""A fresh process must reconstruct host bindings without redispatching effects."""
from pathlib import Path
import subprocess
import sys


WORKER = '''
import sys, time
sys.path[:0] = sys.argv[1:3]
from pathlib import Path
from prosaic_runtime import CustomTool, InvocationScope, StructuredToolLoop, ToolRequest, StructuredToolError
from support.tool_journal import SqliteJournal
root, mode = Path(sys.argv[3]), sys.argv[4]
def effect(args, context):
    with (root / 'effects').open('a') as file:
        file.write(context.operation_key + '\\n')
    if mode == 'crash':
        import os
        os._exit(17)
    return {'recorded': True}
tool = CustomTool('append', 'Synthetic append', {'type':'object','additionalProperties':False},
                  effect, 'v1', with_context=True, operation_key=lambda args, context: 'same-operation')
loop = StructuredToolLoop({'append':tool}, allowed_tools={'append'}, arguments={},
    deadline=time.monotonic()+10, operation_context=InvocationScope(mode, operation_namespace='tenant'),
    tool_journal=SqliteJournal(root / 'journal.sqlite'))
try:
    _, outcome = loop.dispatch(ToolRequest('append', {}))
    print(outcome['recorded'])
except StructuredToolError as error:
    print(error.reason)
    sys.exit(3)
'''


def worker(tmp_path, mode):
    root = Path(__file__).resolve().parents[1]
    return subprocess.run([sys.executable, '-I', '-c', WORKER, str(root / 'src'),
                           str(root / 'tests'), str(tmp_path), mode],
                          capture_output=True, text=True, timeout=20)


def test_fresh_worker_replays_durable_outcome_without_second_effect(tmp_path):
    first = worker(tmp_path, 'first'); second = worker(tmp_path, 'second')
    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    assert first.stdout == second.stdout == 'True\n'
    assert (tmp_path / 'effects').read_text() == 'same-operation\n'


def test_crash_after_effect_leaves_uncertainty_and_next_worker_blocks(tmp_path):
    first = worker(tmp_path, 'crash'); second = worker(tmp_path, 'recovery')
    assert first.returncode == 17 and second.returncode == 3, first.stderr + second.stderr
    assert second.stdout == 'tool_effect_uncertain\n'
    assert (tmp_path / 'effects').read_text() == 'same-operation\n'
