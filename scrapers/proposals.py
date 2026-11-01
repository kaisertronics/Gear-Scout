"""
Proposals (owner, Oct 31: "I haven't seen any proposals"): ideas for new
features shown on the Home page with Build / Skip buttons. The nightly
update adds one each night and builds the ones you approved.

  python -m scrapers.proposals list
  python -m scrapers.proposals add "Title" "Why it helps, in plain English"
  python -m scrapers.proposals built <id> "What shipped"
"""
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

PATH = Path("/data/proposals.json")


def load() -> list[dict]:
    try:
        return json.loads(PATH.read_text())
    except Exception:
        return []


def _save(items: list[dict]) -> None:
    PATH.write_text(json.dumps(items, indent=1))


def add(title: str, why: str) -> dict:
    items = load()
    p = {"id": uuid.uuid4().hex[:8], "title": title.strip(), "why": why.strip(), "status": "proposed",
         "at": datetime.now(timezone.utc).isoformat()}
    items.append(p)
    _save(items)
    return p


def set_status(pid: str, status: str, note: str = "") -> bool:
    items = load()
    for p in items:
        if p["id"] == pid:
            p["status"] = status
            p[f"{status}_at"] = datetime.now(timezone.utc).isoformat()
            if note:
                p["note"] = note
            _save(items)
            return True
    return False


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "add":
        print(json.dumps(add(sys.argv[2], sys.argv[3])))
    elif cmd == "built":
        print(set_status(sys.argv[2], "built", sys.argv[3] if len(sys.argv) > 3 else ""))
    else:
        for p in load():
            print(f"{p['id']}  {p['status']:9}  {p['title']}")
