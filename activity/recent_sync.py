# activity/recent_sync.py

import json
import time
from pathlib import Path
from threading import Lock

import sys
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from config import get_organized_root

MAX_ITEMS = 5
STATE_PATH = get_organized_root() / ".radxa_state" / "recent_sync.json"

_lock = Lock()


def _load():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            return []
    return []


def _save(items):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(items, indent=2))


def record_sync(path: str, category: str):
    with _lock:
        items = _load()

        filename = Path(path).name

        # Remove duplicates (same path)
        items = [item for item in items if item["path"] != path]

        items.insert(0, {
            "path": path,
            "filename": filename,
            "category": category,
            "synced_at": int(time.time()),
        })

        items = items[:MAX_ITEMS]

        _save(items)


def get_recent_syncs():
    with _lock:
        return _load()
