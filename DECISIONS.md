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

## Phase 2 — generation

**D10. Batched pipeline, ~10-min chunks cut at the quietest 0.5 s window near the boundary.**
faster-whisper's `BatchedInferencePipeline` (batch 8) on silence-aligned chunks: 12 min of audio
transcribed in 22.6 s incl. model load; 100 min end-to-end (extract + VAD + ASR) in 95 s with
peak RSS 2.3 GB (vs 1.9 GB for 12 min → memory doesn't grow with film length).
`condition_on_previous_text=False` + hallucination filter + repetition-loop collapse.

**D11. Cues are built from word timestamps** (≤2×42 chars, ≤6 s, break after sentence
punctuation once the cue has substance, split on ≥0.7 s pauses), not from Whisper segments —
batched-mode segments can be minutes long on real films (Tears of Steel: one 188 s segment).

## Phase 3/4 — sync algorithms (what was tried)

**D12. Common score = Σ over cue frames of (p_speech − θ)** on a 10 ms grid → offset search is
one FFT cross-correlation.
* v1 used θ = 0.5 (i.e. `2p − 1`). Perfect on synthetic speech (median 20 ms) but **failed on
  Tears of Steel**: Silero outputs p≈0.1–0.4 for dialogue under the score (mean 0.36 under cues
  vs 0.02 elsewhere), so every cue looked like "silence", and FFT zero-padding (score 0) beat
  real placement → offset −528 s. Fix: θ = film's mean speech probability (clamped 0.05–0.5)
  and out-of-range frames score −0.5. ToS now: +0.12 s on the in-sync original.
* The piecewise split penalty is expressed in "seconds of well-aligned speech" by scaling with
  the mean positive reference value of the film, so weak-VAD films aren't under-segmented.

**D13. Drift: fps-ratio candidates + Theil–Sen local-offset estimate + golden-section refine.**
Candidates alone cover real-world fps conversions; the data-driven estimate covers odd scales.

**D14. Piecewise = Viterbi over cues × offset candidates** (candidates = top-3 peaks of 90 s
sliding windows, ±0.6 s neighbourhoods at 10 ms), constant split penalty, hard no-reorder
constraint via prefix-max (O(N·K)). `--mode piecewise` runs it with and without the drift scale
and keeps the better fit — running it only after drift removal gave a spurious 0.9993 scale on
pure-cut inputs (0.5 s error by minute 12).

**D15. Smart sync iterations** (12-min synthetic, median error / % within 200 ms):
1. anchors + per-line offset from first matched word: 207 ms / 43 % — Whisper's first word of
   a segment starts 100–400 ms early (DTW stretches it into the preceding silence).
2. + snap matched word starts to the first VAD speech frame inside the word: 23 ms / 100 % on
   offset, but drift 91 % (running median biased at the ends of a ramp).
3. + global Theil–Sen detrend: drift 100 %, but piecewise with monotonically increasing steps
   regressed to 80 % (a line fitted through steps).
4. per-line choice between two smoothers: compromise, 93–98 %.
5. **final:** slope from *nearby* line pairs only (≤20 lines apart → cuts can't bias it), detrend,
   running median on the now piecewise-flat residual: offset/drift 100 %, piecewise 97–100 %.
Also: unmatched lines interpolate when neighbours agree *with the trend* (not just with each
other), and token canonicalisation (good-bye→goodbye, Mr→mister) on both sides.

**D16. Benchmark corruption realism.** A negative jump in the piecewise corruption originally
made sections overlap and interleave, which never happens in a real file; lines that would
overlap are now dropped (= footage present in the video but absent from the subtitles).

**D17. Centre-channel extraction for surround audio.** For layouts with FC (5.1, 7.1, …) audio
is extracted as `0.8·FC + 0.1·FL + 0.1·FR`. Test: speech on FC, loud pink noise on the other 5
channels → correlation with the clean speech 0.95 vs 0.31 for a plain downmix.

**D18. auto mode = smart on GPU, audio-only (drift + piecewise) on CPU.** Smart falls back to
alignment against Whisper's English translation when the subtitles look English but the audio
isn't, then to audio-only sync.

## Phase 6 — benchmark-driven iteration

**D19. Real film exposed two smart-sync failure modes** (first full benchmark, Tears of Steel,
piecewise corruption: 42 % of lines within 200 ms, drift: 82 %):
1. With few matched lines (47) the local-pair drift slope was biased by pairs straddling cuts
   and bent whole sections by seconds. Fix: choose the slope among {0, fps-ratio slopes, local
   estimate} by **total variation** of the smoothed detrended offsets (a ramp costs its full
   rise, steps only their size). A "flatness vs. own running median" criterion was tried first
   and failed — a running median follows a linear ramp, so drift looked flat too.
2. Sections whose dialogue Whisper never transcribed (ToS climax: screaming over the score)
   inherited the last matched offset (23 s wrong). Fix: **fuse** text and audio — the final
   assignment is the piecewise Viterbi with an extra emission term pulling matched cues toward
   their Whisper offsets (capped at 3 s so one bad match can't dominate), and candidates from
   both text offsets and audio correlation peaks.
Before: 23.7 % / 13.8 % / 35.5 % of lines within 200 ms on ToS piecewise seeds 1/3/4 (first
benchmark run). Lines off by > 0.3 s on those seeds: 13/13/18 after the slope fix → 2/3/12
after fusion (out of 76; the remaining ones are the untranscribed climax section).
Side effect: smart output keeps the human-authored relative timing within each section, which
matches how real out-of-sync files are broken (they were correct for another release).

**D20. `--translate` verified** on a German LibriVox recording (Grimm, public domain): large-v3
int8_float16, 91 s of audio in 20.7 s, peak RSS 3.3 GB; fluent English output.

**D21. Piecewise candidates: 6 correlation peaks per window (was 3).** On Tears of Steel the
film *repeats its opening dialogue* in a later scene with the same rhythm, so for a 41-cue
section the audio correlation 5 minutes away (0.141) beats the true offset (0.062) and the
true offset never made the top-3 list. The no-reorder constraint already rejects the
impossible placement; it just needs the right candidate to exist. ToS piecewise (audio-only):
76.2 % → 84.8 % within 200 ms; synthetic unchanged. 10 peaks: no further gain, 2.6× slower on
the 100-min film. Lowering the split penalty (1.5–6 s) made no difference — it was a
candidate problem, not a penalty problem.
