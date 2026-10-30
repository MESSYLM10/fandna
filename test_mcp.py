"""The MCP server exposes FanDNA's tools and they work end to end (fakes, no network)."""
import asyncio
import json

import brief
import mcp_server
import qloo
from test_agent import FakeQloo2
from test_brief import FakeGroq, NEW


def _text(result) -> str:
    content = result[0] if isinstance(result, tuple) else result
    return "".join(getattr(c, "text", "") for c in content)


def test_mcp_lists_tools_and_runs_them(tmp_path, monkeypatch):
    monkeypatch.setattr(qloo, "CACHE_PATH", str(tmp_path / "c.sqlite"))
    monkeypatch.setenv("FANDNA_CACHE", str(tmp_path / "c.sqlite"))
    monkeypatch.setattr(qloo, "_SESSION", FakeQloo2())
    monkeypatch.setattr(brief, "_CLIENT", FakeGroq([json.dumps(NEW)]))

    async def go():
        names = {t.name for t in await mcp_server.mcp.list_tools()}
        cmp = await mcp_server.mcp.call_tool("compare_clubs", {"kind": "brands", "club_a": "Kaizer Chiefs",
                                                               "club_b": "Orlando Pirates"})
        bad = await mcp_server.mcp.call_tool("audience_tastes", {"kind": "cars"})
        md = await mcp_server.mcp.call_tool("campaign_brief", {"club": "Kaizer Chiefs"})
        return names, _text(cmp), _text(bad), _text(md)

    names, cmp, bad, md = asyncio.run(go())
    assert names == {"audience_tastes", "compare_clubs", "entity_fans", "find_entity", "campaign_brief"}
    assert "Vodacom" in cmp and "only_b" in cmp
    assert "kind must be one of" in bad
    assert "# FanDNA brief: Kaizer Chiefs F.C." in md and "To validate before spending" in md
    assert md.startswith("> Only 4 tags returned")          # Qloo data warnings are passed on
