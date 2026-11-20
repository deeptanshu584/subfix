# Windows + NVIDIA GPU notes

Findings from getting faster-whisper (CTranslate2) running reliably on a Windows 11 laptop with
a 6 GB RTX 4050. Useful if `subfix info` says CUDA is not available, or a model fails to load.

## CUDA libraries

CTranslate2 4.x needs `cublas64_12.dll` (CUDA 12). The `gpu` extra installs it from PyPI:

```powershell
pip install -e ".[gpu]"        # nvidia-cublas-cu12==12.4.5.8; cuDNN 9 ships inside the ctranslate2 wheel
```

Windows does not search pip's `site-packages\nvidia\*\bin` folders, so `subfix/device.py`
adds them to the DLL search path, to `PATH`, **and preloads them with `ctypes`**. `add_dll_directory`
alone was not enough: CTranslate2 loads cuBLAS lazily with a plain `LoadLibrary`, which worked for
`tiny` but failed for larger models.

cuBLAS is pinned to 12.4.5.8 (the version CTranslate2 4.x is built against). Version 12.9 turned an
allocation failure into a misleading `CUBLAS_STATUS_NOT_SUPPORTED`.

You do **not** need the CUDA Toolkit, only a recent NVIDIA driver.

## "Out of memory" although `nvidia-smi` shows free VRAM

On Windows (WDDM) GPU allocations are also charged against the system **commit limit**. When the
system drive is almost full the page file cannot grow, and a model that fits in VRAM can still fail
with `CUDA failed with error out of memory` or `mkl_malloc: failed to allocate memory`
(`float16` and `large-v3` were the first to fail).

That is why the default compute type is `int8_float16` (about half the weight footprint, same
accuracy for subtitles), and why subfix has an automatic fallback chain: requested configuration
→ `int8_float16` → `int8` on CUDA → smaller model → CPU `int8`. Every fallback is logged with
the reason.

If you hit this: free space on the system drive, move the page file to another drive
(*System → Advanced system settings → Performance → Advanced → Virtual memory*), close browsers,
or pass `--compute-type int8_float16` / `--device cpu`.

## Where subfix keeps its data

Models, extracted audio, VAD signals, transcripts and web UI state live in `SUBFIX_HOME`:

1. the `SUBFIX_HOME` environment variable, if set;
2. `<repo>/.cache` when running from a source checkout;
3. otherwise `%LOCALAPPDATA%\subfix\Cache`.

Point it at a roomy drive if your system drive is small: `setx SUBFIX_HOME D:\subfix-data`.
The first run downloads the model (about 1.6 GB for `large-v3-turbo`, 3 GB for `large-v3`).
Drag-and-drop uploads in the web UI go to `<SUBFIX_HOME>\uploads` (override with `--upload-dir`)
and are refused on the system drive unless you set `SUBFIX_ALLOW_SYSTEM_DRIVE_UPLOADS=1`.

## ffmpeg

```powershell
winget install --id Gyan.FFmpeg.Essentials -e
```

subfix looks for ffmpeg in `SUBFIX_FFMPEG`, then `PATH`, then the WinGet links folder, so it also
works in a terminal opened before `PATH` was updated.

## Other findings

* `av` must be `<16`: faster-whisper 1.2.1 calls `av.open(..., metadata_errors=...)`, which newer
  PyAV releases removed.
* The Windows console defaults to cp1252 and crashes on non-Latin transcripts; the CLI switches
  stdout/stderr to UTF-8.
* Whisper's first word of a segment starts 100–400 ms early; see "How it works" in the README.
