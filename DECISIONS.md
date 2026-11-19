# Decisions log

Each entry: what was decided, why, and what was tried. Newest phases at the bottom.

## Phase 1 — environment

**D1. Model + compute type: `large-v3-turbo` / `int8_float16` on CUDA by default.**
RTX 4050 Laptop has 6 GB VRAM. turbo fp16 fails on this machine because Windows' free commit
charge is ~3 GB (C: full → page file can't grow; WDDM charges GPU allocations against commit).
int8_float16 uses ~1 GB VRAM and works. Accuracy difference vs fp16 is negligible for subtitles.

**D2. `--translate` uses `large-v3` (fallback `medium`).** large-v3-turbo was fine-tuned without
translation data and tends to transcribe instead of translating.

**D3. Fallback chain instead of hard failure.** Requested config → int8_float16 → int8 (CUDA) →
CPU int8. Errors like "out of memory", "cublas", "mkl_malloc" trigger the next step.

**D4. cuBLAS pinned to 12.4.5.8** (matches CTranslate2 4.x build). 12.9 masked an allocation
failure as `CUBLAS_STATUS_NOT_SUPPORTED`.

**D5. DLL discovery on Windows:** add `site-packages/nvidia/*/bin` to the DLL search path *and*
`PATH`, and preload with `ctypes.WinDLL`. `add_dll_directory` alone was not enough for the lazily
loaded cuBLAS in larger models.

**D6. av pinned to 15.1.0** — faster-whisper 1.2.1 calls `av.open(..., metadata_errors=...)`,
which av 19 removed.

**D7. No PyTorch.** Silero VAD is used through the ONNX model bundled with faster-whisper
(onnxruntime), saving ~2.5 GB of install and RAM.

**D8. Cache location:** `$SUBFIX_HOME` → `<repo>/.cache` when running from a source checkout →
platform cache dir (`%LOCALAPPDATA%\subfix\Cache`). Setting a persistent user env var was not
done automatically (it's a system change); the source-checkout default keeps everything on D:.
Also sets `HF_HOME` inside the process (if unset) so Hugging Face's xet cache doesn't land on C:.

**D9. ffmpeg** installed with winget (`Gyan.FFmpeg.Essentials`) to `<ffmpeg folder>`.
subfix looks for ffmpeg in `$SUBFIX_FFMPEG`, `PATH`, then the WinGet Links folder, so it works
even in shells started before the PATH change.
