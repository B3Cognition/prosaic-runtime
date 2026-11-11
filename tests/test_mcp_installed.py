"""Installed-wheel witness for actual MCP dispatch and journal replay."""
import test_mcp_dispatch


def test_installed_runtime_mcp_roundtrip(tmp_path):
    test_mcp_dispatch.test_journal_replay_has_zero_mcp_io_and_checks_current_grant(
        "openai-compatible", tmp_path)
