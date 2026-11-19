"""Paths, defaults and environment setup shared by all subfix modules."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

#: Bump when the on-disk format of cached artefacts changes (forces recomputation).
CACHE_VERSION = 1

#: Whisper / VAD audio sample rate.
SAMPLE_RATE = 16_000


def _source_checkout_root() -> Path | None:
    """Return the repository root if subfix runs from a source checkout (editable install)."""
    root = Path(__file__).resolve().parent.parent
    if (root / "pyproject.toml").is_file() and (root / "subfix").is_dir():
        return root
    return None


def _platform_cache_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "subfix" / "Cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "subfix"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "subfix"


def subfix_home() -> Path:
    """Directory holding models, extracted audio, VAD signals and transcripts.

    Resolution order: ``$SUBFIX_HOME`` → ``<repo>/.cache`` for source checkouts → platform cache
    directory. Created on first use.
    """
    env = os.environ.get("SUBFIX_HOME")
    if env:
        home = Path(env).expanduser()
    else:
        root = _source_checkout_root()
        home = root / ".cache" if root else _platform_cache_dir()
    home.mkdir(parents=True, exist_ok=True)
    return home


def cache_dir(kind: str) -> Path:
    """Return (and create) a sub-directory of :func:`subfix_home`, e.g. ``"audio"``."""
    path = subfix_home() / kind
    path.mkdir(parents=True, exist_ok=True)
    return path


def setup_environment() -> None:
    """Point third-party caches at subfix_home and silence noisy library warnings.

    Hugging Face would otherwise write its xet/hub caches to ``~/.cache/huggingface``.
    """
    home = subfix_home()
    os.environ.setdefault("HF_HOME", str(home / "hf"))
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    # OpenMP runtimes from MKL (ctranslate2) and onnxruntime can clash on Windows.
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")


@dataclass(frozen=True)
class ModelChoice:
    """A concrete Whisper model configuration."""

    model: str
    device: str  # "cuda" | "cpu"
    compute_type: str

    def __str__(self) -> str:
        return f"{self.model} on {self.device} ({self.compute_type})"


#: Subtitle formatting defaults used when generating subtitles.
MAX_LINE_CHARS = 42
MAX_LINES = 2
MAX_CUE_SECONDS = 6.0
MIN_CUE_SECONDS = 0.8
MIN_GAP_SECONDS = 0.08
