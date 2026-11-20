"""Drag-and-drop uploads streamed to disk (never buffered whole in memory).

Layout: ``<upload_dir>/<upload_id>/<sanitised name>``. Uploads are deleted when the last job
using them ends, on ``DELETE /api/uploads/{id}``, when unused for an hour, and at server start.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from starlette.concurrency import run_in_threadpool

from .common import ApiError, classify

CHUNK_SIZE = 1 << 20  # 1 MiB
FREE_SPACE_MARGIN = 1 << 30  # keep 1 GB free on the upload drive
STALE_SECONDS = 3600.0
ALLOW_SYSTEM_DRIVE_ENV = "SUBFIX_ALLOW_SYSTEM_DRIVE_UPLOADS"

_ID_RE = re.compile(r"^u-[0-9a-f]{8}$")
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(10)}


def sanitise_name(name: str) -> str:
    """A safe single file name: no folders, no reserved characters/names, <= 150 chars."""
    base = re.split(r"[\\/]", name or "")[-1]
    base = _BAD_CHARS.sub("_", base).strip().rstrip(". ")
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    stem = stem.strip() or "upload"
    if stem.split(".")[0].upper() in _RESERVED:
        stem = "_" + stem
    ext = ext[:16]
    stem = stem[: 150 - len(ext) - 1]
    return f"{stem}.{ext}" if ext else stem


@dataclass
class Upload:
    id: str
    name: str
    kind: str
    size_bytes: int
    path: Path
    last_used: float = field(default_factory=time.monotonic)
    jobs: set[str] = field(default_factory=set)
    delete_requested: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "upload_id": self.id,
            "name": self.name,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "path": str(self.path),
        }


def on_system_drive(path: Path) -> bool:
    """True if ``path`` is on the Windows system drive (``%SystemDrive%``, normally C:)."""
    system = os.environ.get("SYSTEMDRIVE", "")
    if not system:
        return False
    drive = Path(os.path.abspath(path)).drive
    return drive.rstrip("\\/").lower() == system.rstrip("\\/").lower()


class UploadManager:
    def __init__(self, upload_dir: Path, stale_seconds: float = STALE_SECONDS) -> None:
        self.upload_dir = Path(upload_dir)
        self.stale_seconds = stale_seconds
        self._uploads: dict[str, Upload] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ checks
    def check_dir(self) -> None:
        if on_system_drive(self.upload_dir) and os.environ.get(ALLOW_SYSTEM_DRIVE_ENV) != "1":
            raise ApiError(
                500,
                "upload_dir_on_system_drive",
                f"The upload folder {self.upload_dir} is on the system drive. Start subfix web "
                f"with --upload-dir on another drive (or set {ALLOW_SYSTEM_DRIVE_ENV}=1), or "
                "choose the file with the Browse button instead of dragging it in.",
            )

    def _check_space(self, size: int) -> None:
        free = shutil.disk_usage(self.upload_dir).free
        if size > free - FREE_SPACE_MARGIN:
            raise ApiError(
                413,
                "upload_too_large",
                f"Not enough free space in {self.upload_dir}: the file needs "
                f"{size / 1e9:.1f} GB, {max(0, free - FREE_SPACE_MARGIN) / 1e9:.1f} GB available "
                "(1 GB is kept free). Use the Browse button to process the file in place.",
            )

    # ------------------------------------------------------------------ receive
    def _write_chunk(self, f: Any, data: bytes) -> None:
        f.write(data)

    async def receive(
        self, filename: str, size: int | None, stream: AsyncIterator[bytes]
    ) -> Upload:
        """Stream ``stream`` to disk. Extension, folder and space are checked before reading."""
        name = sanitise_name(filename)
        kind = classify(name)
        self.check_dir()
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        if size is not None:
            self._check_space(size)
        with self._lock:
            uid = f"u-{secrets.token_hex(4)}"
            while uid in self._uploads or (self.upload_dir / uid).exists():
                uid = f"u-{secrets.token_hex(4)}"
            folder = self.upload_dir / uid
            folder.mkdir()
        path = folder / name
        received = 0
        try:
            f = await run_in_threadpool(path.open, "wb")
            try:
                buf = bytearray()
                async for chunk in stream:
                    if not chunk:
                        continue
                    received += len(chunk)
                    if size is not None and received > size:
                        raise ApiError(
                            400, "invalid_request", "The upload is larger than its declared size."
                        )
                    buf += chunk
                    while len(buf) >= CHUNK_SIZE:
                        piece = bytes(buf[:CHUNK_SIZE])
                        del buf[:CHUNK_SIZE]
                        await run_in_threadpool(self._write_chunk, f, piece)
                if buf:
                    await run_in_threadpool(self._write_chunk, f, bytes(buf))
            finally:
                await run_in_threadpool(f.close)
            if size is not None and received != size:
                raise ApiError(
                    400,
                    "invalid_request",
                    f"Upload incomplete: received {received} of {size} bytes.",
                )
        except BaseException:
            # Failed, aborted or disconnected: never leave a partial file behind.
            shutil.rmtree(folder, ignore_errors=True)
            raise
        up = Upload(uid, name, kind, received, path)
        with self._lock:
            self._uploads[uid] = up
        return up

    # ------------------------------------------------------------------ lookup / lifetime
    def get(self, upload_id: str) -> Upload:
        with self._lock:
            up = self._uploads.get(upload_id)
            if up is None or up.delete_requested:
                raise ApiError(404, "upload_not_found", f"Unknown upload '{upload_id}'.")
            up.last_used = time.monotonic()
            return up

    def attach(self, upload_id: str, job_id: str) -> Upload:
        with self._lock:
            up = self._uploads.get(upload_id)
            if up is None or up.delete_requested:
                raise ApiError(404, "upload_not_found", f"Unknown upload '{upload_id}'.")
            up.jobs.add(job_id)
            up.last_used = time.monotonic()
            return up

    def release(self, upload_id: str, job_id: str) -> None:
        """A job using the upload reached a final state; delete it if no other job needs it."""
        with self._lock:
            up = self._uploads.get(upload_id)
            if up is None:
                return
            up.jobs.discard(job_id)
            if up.jobs:
                return
            del self._uploads[upload_id]
        self._remove(up)

    def delete(self, upload_id: str) -> None:
        """``DELETE /api/uploads/{id}``; deferred until its jobs end if it is in use."""
        with self._lock:
            up = self._uploads.get(upload_id)
            if up is None or up.delete_requested:
                raise ApiError(404, "upload_not_found", f"Unknown upload '{upload_id}'.")
            if up.jobs:
                up.delete_requested = True
                return
            del self._uploads[upload_id]
        self._remove(up)

    def _remove(self, up: Upload) -> None:
        shutil.rmtree(up.path.parent, ignore_errors=True)

    def cleanup_stale(self) -> int:
        """Delete uploads not attached to a job and unused for ``stale_seconds``."""
        now = time.monotonic()
        with self._lock:
            stale = [
                u
                for u in self._uploads.values()
                if not u.jobs and now - u.last_used >= self.stale_seconds
            ]
            for u in stale:
                del self._uploads[u.id]
        for u in stale:
            self._remove(u)
        return len(stale)

    def cleanup_all(self) -> int:
        """Delete every upload folder (server start/stop).

        Only ``u-xxxxxxxx`` folders are touched, so an --upload-dir that also holds other files
        is safe.
        """
        with self._lock:
            self._uploads.clear()
        n = 0
        try:
            entries = list(self.upload_dir.iterdir())
        except OSError:
            return 0
        for p in entries:
            if p.is_dir() and _ID_RE.match(p.name):
                shutil.rmtree(p, ignore_errors=True)
                n += 1
        return n
