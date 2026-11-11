from dataclasses import replace
import json
from threading import get_ident

import pytest

from prosaic_runtime import ToolResultV1
from support.conversation_server import ConversationFixture
from support.tool_journal import SqliteJournal
from test_mcp_catalog import SCHEMA, spec
from test_mcp_transport import endpoint, listing, opened
from test_tool_evidence import bindings


def methods(requests):
    return [item[2]["method"] for item in requests if item[0] == "POST"]


def completion(value=14):
    return (200, {"Content-Type": "application/json"},
            {"resultType": "complete", "structuredContent": {"count": value},
             "content": [], "isError": False})


def queue(fixture, key="svc", alias="lookup"):
    fixture.queue_calls([{"name": alias, "id": "lookup1", "arguments": json.dumps({"name": key})}])
    fixture.queue_text("done")


@pytest.mark.parametrize("provider", ["openai-compatible", "anthropic"])
def test_journal_replay_has_zero_mcp_io_and_checks_current_grant(provider, tmp_path):
    grant = [True]
    owner = get_ident()
    checks = []
    def authorize(*_):
        checks.append(get_ident())
        return grant[0]

    def answer(request):
        return listing() if request["method"] == "tools/list" else completion()

    with endpoint(answer) as (url, requests), opened(url, authorize=authorize) as host, ConversationFixture(provider) as fx:
        snapshot = host.discover(access_scope="scope-1")
        def project(value, _context):
            assert get_ident() == owner
            return ToolResultV1(data=value)
        fx.tools = host.bind_tools(snapshot, [spec(projector=project)])
        journal = SqliteJournal(tmp_path / "calls.sqlite")
        options = dict(message="Review", policy=fx.policy, tool_journal=journal,
                       tool_contract_bindings=bindings())
        runtime = fx.runtime
        for _ in range(2):
            queue(fx)
            result = runtime.turn(fx.agent, **options)
            assert result.result.exit_code == 0, result.result.stderr
        assert methods(requests) == ["tools/list", "tools/list", "tools/call"]
        queue(fx, key="changed")
        result = runtime.turn(fx.agent, **options)
        assert result.result.exit_code != 0
        fx.responses.clear()
        grant[0] = False
        queue(fx)
        runtime.turn(fx.agent, **options)
        assert checks and set(checks) == {owner}
        assert methods(requests) == ["tools/list", "tools/list", "tools/call"]


@pytest.mark.parametrize("fault", ["revocation", "drift", "remote-error", "wrong-id", "input-required", "header-mismatch"])
def test_fault_dispatch_count_and_replay_fence(fault, tmp_path):
    grant = [True]
    listed = [0]
    def answer(request):
        if request["method"] == "tools/list":
            listed[0] += 1
            response = listing()
            if listed[0] == 2:
                if fault == "revocation":
                    grant[0] = False
                if fault == "drift":
                    response[2]["tools"][0]["inputSchema"] = {**SCHEMA, "maxProperties": 1}
            return response
        assert request["method"] == "tools/call"
        if fault == "remote-error":
            status, headers, value = completion()
            return status, headers, {**value, "isError": True}
        if fault == "wrong-id":
            return 200, {"Content-Type": "application/json"}, b'{"jsonrpc":"2.0","id":"wrong","result":{}}'
        if fault == "input-required":
            return 200, {"Content-Type": "application/json"}, {"resultType": "input_required"}
        return 400, {"Content-Type": "application/json"}, b'{"jsonrpc":"2.0","id":1,"error":{"code":-32600,"message":"private header mismatch"}}'

    with endpoint(answer) as (url, requests), opened(url, authorize=lambda *_: grant[0]) as host, ConversationFixture("openai-compatible") as fx:
        fx.tools = host.bind_tools(host.discover(access_scope="scope-1"), [spec()])
        journal = SqliteJournal(tmp_path / "calls.sqlite")
        options = dict(message="Review", policy=fx.policy, tool_journal=journal,
                       tool_contract_bindings=bindings())
        runtime = fx.runtime
        expected_posts = 0 if fault in {"revocation", "drift"} else 1
        for _ in range(2):
            fx.responses.clear()
            queue(fx)
            result = runtime.turn(fx.agent, **options)
            if expected_posts:
                assert result.result.exit_code != 0
            assert "private header mismatch" not in repr(result.result)
        assert listed == [2]
        assert methods(requests).count("tools/call") == expected_posts


def test_missing_journal_and_forced_mcp_fail_before_provider(tmp_path):
    from prosaic_runtime import TurnAdmissionError

    with endpoint(lambda _: listing()) as (url, requests), opened(url) as host, ConversationFixture("anthropic") as fx:
        fx.tools = host.bind_tools(host.discover(access_scope="scope-1"), [spec()])
        for policy, journal in [(fx.policy, None),
                                (replace(fx.policy, initial_tool="lookup"), SqliteJournal(tmp_path / "calls.sqlite"))]:
            with pytest.raises(TurnAdmissionError):
                fx.runtime.turn(fx.agent, message="Review", policy=policy,
                                tool_contract_bindings=bindings(), tool_journal=journal)
        assert fx.requests == []
        assert methods(requests) == ["tools/list"]


def test_sse_ignores_notifications_and_preserves_admitted_arguments(tmp_path):
    def answer(request):
        if request["method"] == "tools/list":
            return listing()
        assert request["params"]["arguments"] == {"name": "svc"}
        _, _, result = completion()
        frame = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})
        body = ('id: never-resume\ndata: {"jsonrpc":"2.0","method":"notifications/message"}\n\n'
                f'id: never-resume\nretry: 1\ndata: {frame}\n\n').encode()
        return 200, {"Content-Type": "text/event-stream"}, body

    def authorize(_alias, arguments, _context):
        arguments["name"] = "must not dispatch"
        return True

    with endpoint(answer) as (url, requests), opened(url, authorize=authorize) as host, ConversationFixture("anthropic") as fx:
        fx.tools = host.bind_tools(host.discover(access_scope="scope-1"), [spec()])
        queue(fx)
        result = fx.runtime.turn(fx.agent, message="Review", policy=fx.policy,
                                 tool_journal=SqliteJournal(tmp_path / "calls.sqlite"),
                                 tool_contract_bindings=bindings())
        assert result.result.exit_code == 0, result.result
        assert [item[0] for item in requests] == ["POST", "POST", "POST"]


def test_close_drains_inflight_io_and_fences_the_dispatched_slot(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered, release = Event(), Event()
    def answer(request):
        if request["method"] == "tools/list":
            return listing()
        entered.set()
        assert release.wait(4)
        return completion()

    with endpoint(answer) as (url, requests), opened(url) as host, ConversationFixture("anthropic") as fx:
        fx.tools = host.bind_tools(host.discover(access_scope="scope-1"), [spec()])
        options = dict(message="Review", policy=fx.policy,
                       tool_journal=SqliteJournal(tmp_path / "calls.sqlite"),
                       tool_contract_bindings=bindings())
        queue(fx)
        runtime, agent = fx.runtime, fx.agent
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                turn = pool.submit(runtime.turn, agent, **options)
                assert entered.wait(3)
                closing = pool.submit(host.__exit__, None, None, None)
                closing.result(timeout=3)
                assert turn.result(timeout=3).result.stderr == "toolEffectUncertain"
            fx.responses.clear()
            queue(fx)
            assert runtime.turn(agent, **options).result.stderr == "toolEffectUncertain"
            assert methods(requests).count("tools/call") == 1
        finally:
            release.set()
