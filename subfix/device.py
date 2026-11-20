"""GPU/CPU detection and robust Whisper model loading with automatic fallback.

On Windows, CTranslate2 needs ``cublas64_12.dll`` (from the ``nvidia-cublas-cu12`` pip package).
:func:`register_cuda_dlls` makes those DLLs discoverable *before* ctranslate2 is imported.

Loading is verified with a warm-up inference because on WDDM laptops many failures (out of
memory, cuBLAS errors) only surface at the first encode, not at model construction.
"""

from __future__ import annotations

import ctypes
import glob
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

from .config import ModelChoice, cache_dir, setup_environment
from .log import log

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

_dll_handles: list[object] = []


@lru_cache(maxsize=1)
def register_cuda_dlls() -> list[str]:
    """Make pip-installed NVIDIA DLLs (cuBLAS, cuDNN, ...) loadable. Returns directories added."""
    setup_environment()
    if sys.platform != "win32":
        return []
    added: list[str] = []
    roots = {p for p in sys.path if p.endswith("site-packages")}
    for root in roots:
        for bin_dir in glob.glob(os.path.join(root, "nvidia", "*", "bin")):
            try:
                _dll_handles.append(os.add_dll_directory(bin_dir))
            except OSError:
                continue
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
            added.append(bin_dir)
            # Preload: CTranslate2 loads cuBLAS lazily with a plain LoadLibrary call that does not
            # always honour add_dll_directory; an already-loaded module is always found.
            for name in ("cublasLt64_*.dll", "cublas64_*.dll", "cudnn64_*.dll"):
                for dll in glob.glob(os.path.join(bin_dir, name)):
                    try:
                        ctypes.WinDLL(dll)
                    except OSError as exc:  # pragma: no cover - depends on system
                        log.debug("could not preload %s: %s", dll, exc)
    return added


@dataclass
class GpuInfo:
    """Information reported by ``nvidia-smi`` (all fields optional)."""

    name: str = "unknown"
    vram_total_mb: int = 0
    vram_free_mb: int = 0
    driver: str = "unknown"


@lru_cache(maxsize=1)
def gpu_info() -> GpuInfo | None:
    """Query ``nvidia-smi``; ``None`` if no NVIDIA GPU/driver is present."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [
                exe,
                "--query-gpu=name,memory.total,memory.free,driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    parts = [p.strip() for p in out.splitlines()[0].split(",")]
    try:
        return GpuInfo(parts[0], int(float(parts[1])), int(float(parts[2])), parts[3])
    except (IndexError, ValueError):
        return GpuInfo(name=parts[0] if parts else "unknown")


@lru_cache(maxsize=1)
def cuda_available() -> bool:
    """True if CTranslate2 can see at least one CUDA device."""
    register_cuda_dlls()
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception as exc:  # pragma: no cover - depends on system
        log.debug("CUDA check failed: %s", exc)
        return False


def default_choice(task: str = "transcribe", device: str = "auto") -> ModelChoice:
    """Pick a model/compute type for this machine.

    * CUDA with >= 5 GB VRAM: ``large-v3-turbo`` (transcribe) / ``large-v3`` (translate), both
      ``int8_float16`` (see DECISIONS.md D1/D2).
    * CUDA with less VRAM: ``medium`` / ``small``.
    * CPU: ``small`` int8 (``medium`` is ~3x slower for a modest gain).
    """
    use_cuda = device == "cuda" or (device == "auto" and cuda_available())
    if use_cuda:
        info = gpu_info()
        vram = info.vram_total_mb if info else 0
        if vram == 0 or vram >= 5000:
            model = "large-v3" if task == "translate" else "large-v3-turbo"
        elif vram >= 3500:
            model = "medium"
        else:
            model = "small"
        return ModelChoice(model, "cuda", "int8_float16")
    return ModelChoice("small", "cpu", "int8")


def fallback_chain(choice: ModelChoice, task: str = "transcribe") -> list[ModelChoice]:
    """Ordered list of configurations to try, starting with ``choice``."""
    chain = [choice]
    if choice.device == "cuda":
        for ct in ("int8_float16", "int8"):
            chain.append(ModelChoice(choice.model, "cuda", ct))
        if choice.model in ("large-v3", "large-v2", "large"):
            chain.append(ModelChoice("medium", "cuda", "int8_float16"))
        chain.append(ModelChoice("small", "cuda", "int8_float16"))
        cpu_model = "small" if choice.model not in ("tiny", "base") else choice.model
        chain.append(ModelChoice(cpu_model, "cpu", "int8"))
    elif choice.compute_type != "int8":
        chain.append(ModelChoice(choice.model, "cpu", "int8"))
    seen: set[ModelChoice] = set()
    return [c for c in chain if not (c in seen or seen.add(c))]


def _warm_up(model: WhisperModel) -> None:
    """Run one tiny inference so CUDA/cuBLAS/OOM errors surface now, not mid-movie."""
    import numpy as np

    segments, _ = model.transcribe(
        np.zeros(16_000, dtype=np.float32),
        language="en",
        beam_size=1,
        vad_filter=False,
        without_timestamps=True,
    )
    list(segments)


#: When True (long-running processes such as the web UI) loaded models are kept in memory and
#: reused by later jobs; the CLI leaves it False so each run frees its GPU memory on exit.
keep_models_loaded = False
_model_cache: dict[tuple[ModelChoice, str], tuple[WhisperModel, ModelChoice]] = {}


def release_models() -> None:
    """Drop cached models (frees VRAM once the last reference is gone)."""
    import gc

    _model_cache.clear()
    gc.collect()


def load_model(
    choice: ModelChoice, task: str = "transcribe", allow_fallback: bool = True
) -> tuple[WhisperModel, ModelChoice]:
    """Load (and warm up) a Whisper model, walking the fallback chain on failure.

    Returns the model and the configuration that actually worked. With
    :data:`keep_models_loaded` set, the last loaded model is reused (only one is kept, since a
    6 GB GPU cannot hold turbo and large-v3 side by side comfortably).
    """
    key = (choice, task)
    if keep_models_loaded and key in _model_cache:
        log.info("Reusing loaded Whisper model [bold]%s[/]", _model_cache[key][1])
        return _model_cache[key]
    if keep_models_loaded:
        release_models()
    result = _load_model_uncached(choice, task, allow_fallback)
    if keep_models_loaded:
        _model_cache[key] = result
    return result


def _load_model_uncached(
    choice: ModelChoice, task: str, allow_fallback: bool
) -> tuple[WhisperModel, ModelChoice]:
    register_cuda_dlls()
    from faster_whisper import WhisperModel

    chain = fallback_chain(choice, task) if allow_fallback else [choice]
    errors: list[str] = []
    for cand in chain:
        try:
            log.info("Loading Whisper [bold]%s[/] ...", cand)
            model = WhisperModel(
                cand.model,
                device=cand.device,
                compute_type=cand.compute_type,
                download_root=str(cache_dir("models")),
                cpu_threads=min(8, os.cpu_count() or 4) if cand.device == "cpu" else 0,
            )
            _warm_up(model)
            if cand != choice:
                log.warning("Fell back to [bold]%s[/] (requested %s)", cand, choice)
            return model, cand
        except Exception as exc:
            msg = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            errors.append(f"{cand}: {msg}")
            log.warning("Could not use %s: %s", cand, msg)
            if "No such file" in msg or "Repository Not Found" in msg:
                break  # bad model name; trying other compute types won't help
    raise RuntimeError(
        "Could not load any Whisper model. Tried:\n  " + "\n  ".join(errors) + "\n"
        "Hints: close memory-hungry apps (Windows commit limit), check your internet connection "
        "for the first model download, or pass --device cpu."
    )
