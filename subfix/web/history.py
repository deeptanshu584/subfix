"""Job history stored as JSON lines in ``<SUBFIX_HOME>/web/history.jsonl``."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

MAX_ITEMS = 500


class History:
    """Append-only JSONL file capped at :data:`MAX_ITEMS` entries (thread-safe)."""

    def __init__(self, path: Path, cap: int = MAX_ITEMS) -> None:
        self.path = Path(path)
        self.cap = cap
        self._lock = threading.Lock()
        self._count: int | None = None

    def _read(self) -> list[dict[str, Any]]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        items = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn line (crash mid-write) must not break the list
            if isinstance(obj, dict):
                items.append(obj)
        return items

    def _rewrite(self, items: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".jsonl.tmp")
        tmp.write_text(
            "".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items), encoding="utf-8"
        )
        tmp.replace(self.path)

    def append(self, item: dict[str, Any]) -> None:
        with self._lock:
            if self._count is None:
                self._count = len(self._read())
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
            self._count += 1
            if self._count > self.cap:
                items = self._read()[-self.cap :]
                self._rewrite(items)
                self._count = len(items)

    def items(self, limit: int = 100) -> list[dict[str, Any]]:
        """Newest first."""
        with self._lock:
            items = self._read()
        items.reverse()
        return items[: max(0, limit)]

    def clear(self) -> None:
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self._count = 0
