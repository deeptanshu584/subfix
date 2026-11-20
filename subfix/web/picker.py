"""Native "Open file" dialog, shown by a separate Python process (tkinter).

Tk must not run inside the server's worker/request threads, so each pick starts
``sys.executable -c <script>``; the child prints ``{"path": ...}`` as JSON and exits. Only one
dialog can be open at a time (``409 picker_busy``). The folder of the last picked file is
remembered in ``<SUBFIX_HOME>/web/picker.json``.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
from pathlib import Path

from .. import formats
from .common import ApiError

EXIT_NO_TK = 3

# Runs in the child process. argv: title, initial dir ("" = default), filetypes JSON.
DIALOG_SCRIPT = r"""
import json, sys
try:
    import tkinter as tk
    from tkinter import filedialog
except Exception:
    sys.exit(3)
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
title, initial, types = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
try:
    root = tk.Tk()
except Exception:
    sys.exit(3)
root.title("subfix")
root.overrideredirect(True)
root.geometry("1x1+200+200")
root.attributes("-alpha", 0.0)
root.attributes("-topmost", True)
root.deiconify()
root.lift()
root.focus_force()
root.update()
kwargs = {"parent": root, "title": title, "filetypes": [tuple(t) for t in types]}
if initial:
    kwargs["initialdir"] = initial
path = filedialog.askopenfilename(**kwargs)
root.destroy()
sys.stdout.write(json.dumps({"path": path or None}))
"""


def filetypes(kind: str) -> list[tuple[str, str]]:
    """tkinter ``filetypes`` for ``kind`` ("media" | "subtitle"), from :mod:`subfix.formats`."""

    def pattern(exts: tuple[str, ...]) -> str:
        return " ".join(f"*{e}" for e in exts)

    if kind == "subtitle":
        return [
            ("Subtitles", pattern(formats.SUBTITLE_EXTENSIONS)),
            ("All files", "*.*"),
        ]
    return [
        ("Video and audio", pattern(formats.MEDIA_EXTENSIONS)),
        ("Video", pattern(formats.VIDEO_EXTENSIONS)),
        ("Audio", pattern(formats.AUDIO_EXTENSIONS)),
        ("All files", "*.*"),
    ]


def tk_available() -> bool:
    return importlib.util.find_spec("tkinter") is not None


class Picker:
    def __init__(self, state_file: Path) -> None:
        self.state_file = Path(state_file)
        self._busy = threading.Lock()

    # -------------------------------------------------------------- remembered folder
    def last_dir(self) -> str:
        try:
            d = json.loads(self.state_file.read_text(encoding="utf-8")).get("last_dir") or ""
        except (OSError, ValueError, AttributeError):
            return ""
        return d if d and Path(d).is_dir() else ""

    def _remember(self, path: str) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(
                json.dumps({"last_dir": str(Path(path).parent)}), encoding="utf-8"
            )
        except OSError:
            pass

    # -------------------------------------------------------------- dialog
    def run_dialog(self, kind: str, initial_dir: str) -> str | None:
        """Show the dialog in a child process; return the chosen path or None.

        Raises ApiError ``picker_unavailable`` if tkinter cannot be used.
        """
        title = "Choose a subtitle file" if kind == "subtitle" else "Choose a video or audio file"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(
            [sys.executable, "-c", DIALOG_SCRIPT, title, initial_dir, json.dumps(filetypes(kind))],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
        )
        if proc.returncode == EXIT_NO_TK:
            raise ApiError(501, "picker_unavailable", "No native file dialog (tkinter missing).")
        if proc.returncode != 0:
            raise ApiError(
                500, "internal", f"File dialog failed: {proc.stderr.strip()[-300:] or 'error'}"
            )
        try:
            path = json.loads(proc.stdout.strip() or "{}").get("path")
        except ValueError:
            path = None
        return str(Path(path)) if path else None

    def pick(self, kind: str) -> str | None:
        if not tk_available():
            raise ApiError(
                501,
                "picker_unavailable",
                "No native file dialog is available (tkinter is not installed). Paste the path "
                "into the box instead.",
            )
        if not self._busy.acquire(blocking=False):
            raise ApiError(409, "picker_busy", "A file dialog is already open.")
        try:
            path = self.run_dialog(kind, self.last_dir())
        finally:
            self._busy.release()
        if path:
            self._remember(path)
        return path
