"""
Ship the reviewed demo with the app.

On your machine, after you've reviewed the demo brief in the app:
    python demo_seed.py export        # writes demo_seed.json from fandna_cache.sqlite

Commit demo_seed.json. On a fresh host (Streamlit Cloud starts with an empty cache),
app.py calls load_if_empty() once, so the first visitor sees the reviewed brief
instantly, with no Qloo or Groq call. Rows keep their original retrieval times, so
the app shows honestly when the data was fetched; anything older than the cache
TTL is refreshed live when the keys work, and served as "older cached result" when not.
"""
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

SEED = Path(__file__).parent / "demo_seed.json"


def _db_path() -> str:
    return os.environ.get("FANDNA_CACHE", "fandna_cache.sqlite")


def export(path: str | None = None) -> dict:
    with closing(sqlite3.connect(path or _db_path())) as con:
        cache = con.execute("SELECT key, path, params, body, fetched_at FROM cache").fetchall()
        briefs = con.execute("SELECT key, body, created FROM briefs").fetchall()
        has_answers = con.execute("SELECT name FROM sqlite_master WHERE name='answers'").fetchone()
        answers = con.execute("SELECT key, body, created FROM answers").fetchall() if has_answers else []
    data = {"cache": cache, "briefs": briefs, "answers": answers}
    SEED.write_text(json.dumps(data, ensure_ascii=False))
    return {"qloo_results": len(cache), "briefs": len(briefs), "answers": len(answers)}


def load_if_empty(path: str | None = None) -> int:
    """Copy the seed into an empty cache. Returns rows loaded (0 if not needed)."""
    if not SEED.exists():
        return 0
    with closing(sqlite3.connect(path or _db_path())) as con, con:
        con.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, path TEXT, "
                    "params TEXT, body TEXT, fetched_at REAL)")
        con.execute("CREATE TABLE IF NOT EXISTS briefs (key TEXT PRIMARY KEY, body TEXT, created REAL)")
        con.execute("CREATE TABLE IF NOT EXISTS answers (key TEXT PRIMARY KEY, body TEXT, created REAL)")
        if con.execute("SELECT COUNT(*) FROM cache").fetchone()[0]:
            return 0
        data = json.loads(SEED.read_text())
        con.executemany("INSERT OR IGNORE INTO cache VALUES (?,?,?,?,?)", data.get("cache", []))
        con.executemany("INSERT OR IGNORE INTO briefs VALUES (?,?,?)", data.get("briefs", []))
        con.executemany("INSERT OR IGNORE INTO answers VALUES (?,?,?)", data.get("answers", []))
        return sum(len(data.get(k, [])) for k in ("cache", "briefs", "answers"))


if __name__ == "__main__":
    if sys.argv[1:] == ["export"]:
        n = export()
        print(f"Wrote {SEED.name}: {n['qloo_results']} Qloo results, {n['briefs']} brief(s), "
              f"{n['answers']} Ask FanDNA answer(s). Commit it.")
    else:
        print(__doc__)
