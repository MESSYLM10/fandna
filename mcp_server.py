"""
FanDNA as an MCP server: any MCP client (Claude Desktop, Cursor, other agents) can use
FanDNA's Qloo tools and campaign brief.

    python mcp_server.py              # stdio, for Claude Desktop and similar clients
    python mcp_server.py --http       # streamable HTTP on http://127.0.0.1:8000/mcp

Claude Desktop (claude_desktop_config.json):
    {"mcpServers": {"fandna": {"command": "/path/to/fandna/.venv/bin/python",
                               "args": ["/path/to/fandna/mcp_server.py"]}}}

Keys come from the same places as the app: QLOO_API_KEY / GROQ_API_KEY, or the qloo CLI files.
"""
import os
import sys
from pathlib import Path
from typing import Annotated, Literal

os.chdir(Path(__file__).parent)            # cache, prompt and demo seed live next to this file

from mcp.server.fastmcp import FastMCP     # noqa: E402
from pydantic import Field                 # noqa: E402

import brief                               # noqa: E402
import demo_seed                           # noqa: E402
import qloo                                # noqa: E402
import tools                               # noqa: E402

demo_seed.load_if_empty()
mcp = FastMCP("fandna", instructions=(
    "FanDNA turns Qloo taste data about South African football fans into campaign evidence. "
    "Qloo shows affinity (what an audience ranks highly), not behaviour. Johannesburg is the verified city. "
    "Ages: 35_and_younger, 36_to_55, 55_and_older. Kinds: artists, brands, places, tags."))


Kind = Annotated[Literal["artists", "brands", "places", "tags"],
                 Field(description="Type of Qloo signal: artists, brands, places (venues) or tags (taste tags)")]
Age = Annotated[Literal["35_and_younger", "36_to_55", "55_and_older"], Field(description="Qloo age band")]
Gender = Annotated[Literal["men", "women", "everyone"], Field(description="Audience gender")]
City = Annotated[str, Field(description="City; Johannesburg is the only verified one")]
Club = Annotated[str, Field(description="Club name as fans say it, e.g. Kaizer Chiefs")]


def _safe(fn, **kwargs) -> dict:
    try:
        return fn(**kwargs)
    except (ValueError, TypeError) as e:
        return {"error": str(e)}
    except qloo.QlooError as e:
        return {"error": f"Qloo: {e}"}


@mcp.tool()
def audience_tastes(kind: Kind, club: Annotated[str, Field(description="Optional club, e.g. Kaizer Chiefs")] = "",
                    age: Age = "35_and_younger", gender: Gender = "men", city: City = "Johannesburg") -> dict:
    """Top Qloo artists, brands, places or taste tags for an audience. With a club, each item is
    marked club_specific when it appears only for that club's fans."""
    return _safe(tools.audience_tastes, kind=kind, club=club, age=age, gender=gender, city=city)


@mcp.tool()
def compare_clubs(kind: Kind, club_a: Club, club_b: Club, age: Age = "35_and_younger",
                  gender: Gender = "men", city: City = "Johannesburg") -> dict:
    """Compare two clubs' fans in Qloo for one kind of signal: shared items and items unique to each."""
    return _safe(tools.compare_clubs, kind=kind, club_a=club_a, club_b=club_b, age=age, gender=gender, city=city)


@mcp.tool()
def entity_fans(name: Annotated[str, Field(description="Brand, artist or place, e.g. Bathu")]) -> dict:
    """Age and gender affinity of the audience of a brand, artist or place in Qloo."""
    return _safe(tools.entity_fans, name=name)


@mcp.tool()
def find_entity(name: Annotated[str, Field(description="Name to search for in Qloo")]) -> dict:
    """Search Qloo for an entity (artist, brand, place, team) and return the closest matches."""
    return _safe(tools.find_entity, name=name)


@mcp.tool()
def campaign_brief(club: Club, objective: Annotated[str, Field(description="What the campaign must achieve")]
                   = "Fill the stadium for the next home match",
                   budget_zar: Annotated[int, Field(description="Total budget in rand", ge=20_000)] = 250_000,
                   age: Age = "35_and_younger", gender: Gender = "men") -> str:
    """FanDNA's full campaign brief for a club's fans in Johannesburg, as Markdown: three concepts,
    each with what Qloo shows, what it suggests, activation, budget (10% contingency), KPIs and
    what to validate before spending. Every cited signal is checked against Qloo's results."""
    try:
        aud, _ = tools._audience(age, gender, "Johannesburg")
        ev = qloo.build_evidence(aud, seed_query=club or None)
        name = ev["seed"]["name"] if ev.get("seed_effect") else None
        result = brief.cached_brief(ev, name, objective, int(budget_zar))
        notes = "".join(f"> {w}\n" for w in ev["warnings"])
        return (notes + "\n" if notes else "") + brief.to_markdown(result, name, int(budget_zar))
    except (qloo.QlooError, brief.BriefError) as e:
        return f"FanDNA could not write the brief: {e}"


if __name__ == "__main__":
    mcp.run(transport="streamable-http" if "--http" in sys.argv else "stdio")