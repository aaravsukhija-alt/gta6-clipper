"""Remembers which videos were already handled (saved in state/processed.json, committed by the workflow)."""
import json
from datetime import datetime, timezone
from pathlib import Path

from .config import STATE_FILE

FINAL = {"done", "skipped", "gave_up"}
MAX_ERRORS = 3


class State:
    def __init__(self, path: Path = STATE_FILE):
        self.path = path
        self.items: dict[str, dict] = {}
        if path.exists():
            self.items = json.loads(path.read_text(encoding="utf-8")).get("items", {})

    def is_final(self, *keys: str) -> bool:
        return any(self.items.get(k, {}).get("status") in FINAL for k in keys if k)

    def get(self, key: str) -> dict:
        return self.items.get(key, {})

    def set(self, keys, status: str, **extra):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for k in [k for k in keys if k]:
            rec = self.items.setdefault(k, {})
            rec.update(extra, status=status, updated=now)

    def error(self, keys, message: str):
        main = next(k for k in keys if k)
        n = self.items.get(main, {}).get("errors", 0) + 1
        self.set(keys, "gave_up" if n >= MAX_ERRORS else "error", errors=n, note=message[:300])

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"items": dict(sorted(self.items.items()))}, indent=1, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(self.path)
