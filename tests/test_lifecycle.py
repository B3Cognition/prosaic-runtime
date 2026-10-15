import threading

import pytest
from prosaic_runtime import ExecutionLifecycle, Draining, ProsaicRuntime, RuntimeConfig, EndpointConfig


def test_fences_new_work_but_drains_nested_work_and_finally():
    gate = ExecutionLifecycle()
    entered, release, saved = (threading.Event() for _ in range(3))
    def work():
        with gate.work():
            entered.set()
            assert release.wait(5)
            with gate.work():
                saved.set()
    thread = threading.Thread(target=work)
    thread.start()
    assert entered.wait(5)
    gate.begin()
    with pytest.raises(Draining):
        with gate.work():
            pass
    assert not gate.wait(timeout=.01)
    release.set()
    thread.join(5)
    assert saved.is_set() and gate.wait(timeout=1) and gate.active == 0


def test_exception_releases_admission_and_self_wait_is_rejected():
    gate = ExecutionLifecycle()
    with pytest.raises(ValueError):
        with gate.work():
            raise ValueError('fixture')
    with gate.work():
        with pytest.raises(RuntimeError, match='own'):
            gate.wait()
    assert gate.wait(timeout=0)


def test_runtime_rejects_new_calls_before_inspection_after_drain():
    runtime = ProsaicRuntime(RuntimeConfig({"p": EndpointConfig("http://127.0.0.1:9/v1", "fixture")}, {}, "p", frozenset()))
    runtime.lifecycle.begin()
    with pytest.raises(Draining):
        runtime.run('missing.md')
    with pytest.raises(Draining):
        runtime.turn('missing.md', message='fixture')


def test_runtime_tracks_invocation_until_return_and_does_not_cancel_it(monkeypatch):
    from prosaic_runtime import EndpointConfig, Result
    runtime = ProsaicRuntime(RuntimeConfig({'p': EndpointConfig('http://127.0.0.1:9/v1', 'fixture')}, {}, 'p', frozenset()))
    entered, release, saved = (threading.Event() for _ in range(3))
    def execute(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        saved.set()
        return Result(0, 'ok', '')
    monkeypatch.setattr(runtime, '_run_accounted', execute)
    worker = threading.Thread(target=runtime.run, args=('fixture',))
    worker.start()
    assert entered.wait(5)
    assert not runtime.lifecycle.wait(timeout=.01)
    release.set()
    worker.join(5)
    assert saved.is_set() and not worker.is_alive() and runtime.lifecycle.wait(timeout=1)


@pytest.mark.parametrize('timeout', [-1, True, float('inf'), float('nan')])
def test_invalid_wait_timeout_is_rejected(timeout):
    with pytest.raises(ValueError): ExecutionLifecycle().wait(timeout=timeout)
