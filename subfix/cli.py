"""Command-line interface: ``subfix [auto|generate|sync|info] ...``.

``subfix movie.mkv`` is shorthand for ``subfix auto movie.mkv``.
"""

from __future__ import annotations

import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .log import console, log, setup_logging

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="Generate subtitles with Whisper, or fix out-of-sync ones. "
    "[dim]Run `subfix MOVIE` for automatic mode.[/]",
)

COMMANDS = {"auto", "generate", "sync", "info", "web", "--help", "-h", "--version"}


class Mode(StrEnum):
    auto = "auto"
    offset = "offset"
    drift = "drift"
    piecewise = "piecewise"
    smart = "smart"


VerboseOpt = Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")]
StatusOpt = Annotated[
    bool,
    typer.Option(
        "--status", help="Print SUBFIX-STATUS lines (used by the mpv plugin).", hidden=True
    ),
]
QuietOpt = Annotated[
    bool, typer.Option("--quiet", "-q", help="Only warnings/errors; no progress bars.")
]
TrackOpt = Annotated[int, typer.Option("--audio-track", help="Audio track index (0 = first).")]
NoCacheOpt = Annotated[
    bool, typer.Option("--no-cache", help="Recompute audio/VAD/transcript instead of reusing.")
]
ModelOpt = Annotated[
    str | None,
    typer.Option(
        "--model",
        "-m",
        help="Whisper model (tiny … large-v3, large-v3-turbo). Default depends on GPU.",
    ),
]
DeviceOpt = Annotated[str, typer.Option("--device", help="auto | cuda | cpu")]
ComputeOpt = Annotated[
    str | None, typer.Option("--compute-type", help="float16 | int8_float16 | int8 | float32")
]
LangOpt = Annotated[
    str | None,
    typer.Option(
        "--lang", "-l", help="Spoken language code (e.g. en, fr). Auto-detected if omitted."
    ),
]
OutOpt = Annotated[Path | None, typer.Option("--output", "-o", help="Output .srt path.")]


def _choice(model: str | None, device: str, compute_type: str | None, task: str):
    from .config import ModelChoice
    from .device import cuda_available, default_choice

    base = default_choice(task, device)
    if device == "cuda" and not cuda_available():
        log.warning("CUDA requested but not available; using CPU.")
    return ModelChoice(model or base.model, base.device, compute_type or base.compute_type)


def _check_media(path: Path) -> None:
    if not path.is_file():
        console.print(f"[red]error:[/] file not found: {path}")
        raise typer.Exit(2)


@app.command()
def generate(
    video: Annotated[Path, typer.Argument(help="Video or audio file.")],
    lang: LangOpt = None,
    translate: Annotated[
        bool, typer.Option("--translate", "-t", help="Translate speech to English subtitles.")
    ] = False,
    model: ModelOpt = None,
    device: DeviceOpt = "auto",
    compute_type: ComputeOpt = None,
    output: OutOpt = None,
    batch_size: Annotated[int, typer.Option(help="GPU batch size for Whisper.")] = 8,
    audio_track: TrackOpt = 0,
    no_cache: NoCacheOpt = False,
    verbose: VerboseOpt = False,
    quiet: QuietOpt = False,
    status: StatusOpt = False,
) -> None:
    """Generate subtitles from the audio → [bold]VIDEO.generated.srt[/]."""
    setup_logging(verbose, quiet, status)
    _check_media(video)
    from .pipeline import Context
    from .pipeline import generate as do_generate

    task = "translate" if translate else "transcribe"
    ctx = Context(video, audio_track, use_cache=not no_cache)
    path, _, _ = do_generate(
        ctx, task, lang, _choice(model, device, compute_type, task), output, batch_size
    )
    print(f"SUBFIX-OUTPUT: {path}", flush=True)


@app.command()
def sync(
    video: Annotated[Path, typer.Argument(help="Video or audio file (the timing reference).")],
    subs: Annotated[
        Path | None,
        typer.Argument(help="Out-of-sync subtitle file (.srt/.vtt/.ass). Default: next to video."),
    ] = None,
    mode: Annotated[
        Mode,
        typer.Option("--mode", help="auto = smart on GPU, else audio (drift + piecewise)."),
    ] = Mode.auto,
    lang: LangOpt = None,
    model: ModelOpt = None,
    device: DeviceOpt = "auto",
    compute_type: ComputeOpt = None,
    output: OutOpt = None,
    audio_track: TrackOpt = 0,
    no_cache: NoCacheOpt = False,
    verbose: VerboseOpt = False,
    quiet: QuietOpt = False,
    status: StatusOpt = False,
) -> None:
    """Fix the timing of existing subtitles → [bold]VIDEO.synced.srt[/]."""
    setup_logging(verbose, quiet, status)
    _check_media(video)
    from .pipeline import Context
    from .pipeline import sync as do_sync
    from .srt_io import find_sidecar

    subs = subs or find_sidecar(video)
    if subs is None:
        console.print(f"[red]error:[/] no subtitle file given and none found next to {video.name}")
        raise typer.Exit(2)
    ctx = Context(video, audio_track, use_cache=not no_cache)
    path, _, _ = do_sync(
        ctx, subs, mode.value, _choice(model, device, compute_type, "transcribe"), lang, output
    )
    print(f"SUBFIX-OUTPUT: {path}", flush=True)


@app.command()
def auto(
    video: Annotated[Path, typer.Argument(help="Video or audio file.")],
    subs: Annotated[
        Path | None, typer.Option("--subs", "-s", help="Use this subtitle file.")
    ] = None,
    mode: Annotated[Mode, typer.Option("--mode", help="Sync mode if subtitles exist.")] = Mode.auto,
    generate_: Annotated[
        bool, typer.Option("--generate", "-g", help="Always generate, even if subtitles exist.")
    ] = False,
    lang: LangOpt = None,
    translate: Annotated[
        bool, typer.Option("--translate", "-t", help="When generating: English subtitles.")
    ] = False,
    model: ModelOpt = None,
    device: DeviceOpt = "auto",
    compute_type: ComputeOpt = None,
    output: OutOpt = None,
    audio_track: TrackOpt = 0,
    no_cache: NoCacheOpt = False,
    verbose: VerboseOpt = False,
    quiet: QuietOpt = False,
    status: StatusOpt = False,
) -> None:
    """[default] Sync subtitles next to VIDEO if present, otherwise generate them."""
    setup_logging(verbose, quiet, status)
    _check_media(video)
    from .pipeline import Context
    from .pipeline import auto as do_auto
    from .pipeline import generate as do_generate

    ctx = Context(video, audio_track, use_cache=not no_cache)
    task = "translate" if translate else "transcribe"
    choice = _choice(model, device, compute_type, task)
    if generate_:
        path = do_generate(ctx, task, lang, choice, output)[0]
    else:
        path = do_auto(ctx, subs, mode.value, choice, lang, translate, output)
    print(f"SUBFIX-OUTPUT: {path}", flush=True)


@app.command()
def web(
    port: Annotated[int, typer.Option(help="Port on 127.0.0.1.")] = 8765,
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Don't open the browser automatically.")
    ] = False,
    upload_dir: Annotated[
        Path | None,
        typer.Option(help="Temp folder for drag-and-drop uploads (default: SUBFIX_HOME/uploads)."),
    ] = None,
    verbose: VerboseOpt = False,
) -> None:
    """Start the local web UI (http://127.0.0.1:PORT) and open it in the browser."""
    setup_logging(verbose)
    from .web.server import run

    run(port=port, open_browser=not no_browser, upload_dir=upload_dir)


@app.command()
def info(verbose: VerboseOpt = False) -> None:
    """Show detected GPU/CPU, default models and cache location."""
    setup_logging(verbose)
    import platform

    from .audio import AudioError, find_tool
    from .config import subfix_home
    from .device import cuda_available, default_choice, gpu_info

    console.print(
        f"[bold]subfix[/] {__version__}  (Python {platform.python_version()}, "
        f"{platform.system()} {platform.release()})"
    )
    g = gpu_info()
    if g:
        console.print(
            f"GPU:     {g.name}, {g.vram_total_mb} MiB VRAM "
            f"({g.vram_free_mb} MiB free), driver {g.driver}"
        )
    else:
        console.print("GPU:     none detected (nvidia-smi not found)")
    console.print(f"CUDA:    {'available' if cuda_available() else 'NOT available'} to CTranslate2")
    console.print(f"Default: transcribe → {default_choice('transcribe')}")
    console.print(f"         translate  → {default_choice('translate')}")
    for tool in ("ffmpeg", "ffprobe"):
        try:
            console.print(f"{tool + ':':9}{find_tool(tool)}")
        except AudioError:
            console.print(f"{tool + ':':9}[red]NOT FOUND[/]")
    console.print(f"Cache:   {subfix_home()}")


def main() -> None:
    """Entry point; inserts the ``auto`` command when the first argument is a file."""
    argv = sys.argv[1:]
    if argv and argv[0] not in COMMANDS and not argv[0].startswith("-"):
        sys.argv.insert(1, "auto")
    from .audio import AudioError
    from .srt_io import SubtitleError

    try:
        app()
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted.[/]")
        sys.exit(130)
    except (
        AudioError,
        SubtitleError,
        RuntimeError,
        ValueError,
        PermissionError,
        FileNotFoundError,
    ) as exc:
        if "-v" in argv or "--verbose" in argv:
            raise
        console.print(f"[red]error:[/] {exc}")
        console.print("[dim]Run again with -v for details.[/]")
        print(
            f"SUBFIX-ERROR: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}",
            flush=True,
        )
        sys.exit(1)
    except Exception as exc:  # unexpected: keep the traceback, but tell the plugin too
        print(f"SUBFIX-ERROR: internal error: {type(exc).__name__}: {exc}", flush=True)
        raise


if __name__ == "__main__":
    main()
