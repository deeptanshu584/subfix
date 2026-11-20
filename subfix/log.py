"""Console, logging and progress-bar helpers (rich)."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager

from rich.console import Console
from rich.logging import RichHandler
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)


def _make_console() -> Console:
    # Windows consoles default to cp1252, which crashes on non-Latin transcripts.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    return Console(stderr=True, highlight=False)


console = _make_console()
log = logging.getLogger("subfix")

#: When True progress bars are suppressed (machine-readable / plugin mode).
_quiet = False
#: When True, ``SUBFIX-STATUS:`` lines are printed to stdout for the mpv plugin.
_status = False


def setup_logging(verbose: bool = False, quiet: bool = False, status: bool = False) -> None:
    """Configure the ``subfix`` logger once for CLI use."""
    global _quiet, _status
    _quiet = quiet
    _status = status
    level = logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO)
    handler = RichHandler(
        console=console, show_path=False, show_time=False, markup=True, rich_tracebacks=True
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.handlers[:] = [handler]
    log.setLevel(level)
    log.propagate = False
    # faster-whisper logs every VAD/segment step at INFO; keep it quiet unless verbose.
    logging.getLogger("faster_whisper").setLevel(logging.DEBUG if verbose else logging.WARNING)


class _NullProgress:
    def add_task(self, *args: object, **kwargs: object) -> int:
        return 0

    def update(self, *args: object, **kwargs: object) -> None:
        pass

    def advance(self, *args: object, **kwargs: object) -> None:
        pass


def set_progress_bars(enabled: bool) -> None:
    """Enable/disable rich progress bars without changing the log level (used by the web UI)."""
    global _quiet
    _quiet = not enabled


@contextmanager
def progress(transient: bool = True) -> Iterator[Progress | _NullProgress]:
    """A rich progress bar context; a no-op object in quiet mode or when not a TTY."""
    if _quiet:
        yield _NullProgress()
        return
    bar = Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TextColumn("eta"),
        TimeRemainingColumn(),
        console=console,
        transient=transient,
    )
    with bar:
        yield bar


def emit_status(message: str) -> None:
    """Machine-readable progress line on stdout (consumed by the mpv plugin).

    Format: ``SUBFIX-STATUS: <message>``. Only printed when enabled with ``--status``.
    """
    if not _status:
        return
    print(f"SUBFIX-STATUS: {message}", flush=True)
