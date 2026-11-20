"""Shared helpers for the web backend: API errors, timestamps, path handling."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import formats


class ApiError(Exception):
    """An error returned to the client as ``{"error": {"code", "message"}}``."""

    def __init__(
        self, status: int, code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details

    def body(self) -> dict[str, Any]:
        err: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            err["details"] = self.details
        return {"error": err}


def now_iso() -> str:
    """Local time with UTC offset, e.g. ``2026-11-20T04:12:05+05:30``."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def clean_path_text(text: str) -> str:
    """Strip whitespace and the quotes Explorer's "Copy as path" adds."""
    text = (text or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def existing_file(text: str) -> Path:
    """Resolve a user-supplied absolute path to an existing file or raise an ApiError."""
    raw = clean_path_text(text)
    if not raw:
        raise ApiError(400, "invalid_request", "No file path was given.")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ApiError(400, "invalid_request", f"Please give a full path (got '{raw}').")
    try:
        is_file = path.is_file()
    except OSError:
        is_file = False
    if not is_file:
        if path.is_dir():
            raise ApiError(404, "file_not_found", f"{path} is a folder, not a file.")
        raise ApiError(404, "file_not_found", f"File not found: {path}")
    # abspath, not resolve(): keep mapped drive letters instead of UNC paths
    return Path(os.path.abspath(path))


def classify(path: Path | str) -> str:
    """:func:`subfix.formats.classify` mapped to ``400 unsupported_format``."""
    try:
        return formats.classify(path)
    except formats.UnsupportedFormat as exc:
        raise ApiError(400, "unsupported_format", str(exc)) from None


def duration_of(path: Path) -> float | None:
    """Media duration via ffprobe; ``None`` if unknown (never raises)."""
    try:
        from ..audio import media_duration

        d = media_duration(path)
    except Exception:
        return None
    return round(d, 3) if d and d > 0 else None


def formats_dict() -> dict[str, list[str]]:
    return {
        "video": list(formats.VIDEO_EXTENSIONS),
        "audio": list(formats.AUDIO_EXTENSIONS),
        "subtitle": list(formats.SUBTITLE_EXTENSIONS),
    }
