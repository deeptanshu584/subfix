# subfix for mpv

A small mpv script that runs `subfix` on the file you are watching and loads the result as a subtitle track. Playback continues while subfix works in the background.

| Key | Binding name | What it does |
|---|---|---|
| `Ctrl+Shift+S` | `subfix/auto` | Syncs the selected external subtitle track. If none is selected, subfix uses the subtitle file next to the video, or generates new subtitles when there is none. |
| `Ctrl+Shift+G` | `subfix/generate` | Always generates subtitles from the audio (`<video>.generated.srt`). |
| `Ctrl+Shift+T` | `subfix/translate` | Generates English subtitles, whatever language is spoken. |
| `Ctrl+Shift+X` | `subfix/cancel` | Stops the running job. |

While subfix runs, the OSD shows `subfix: working… 0:42`. When it finishes, the new `.srt` is added and selected (track title `subfix`), and the OSD shows `subfix: loaded Movie.synced.srt`. If subfix fails, the error stays on screen for a few seconds and is also written to the mpv console.

Only local files are supported. Network streams show `subfix only works on local files`.

## 1. Install mpv (Windows)

```powershell
winget install --id shinchiro.mpv -e
```

(`--location <folder>` installs it elsewhere, for example on a drive with more space. If winget
ignores it, download a build from <https://github.com/shinchiro/mpv-winbuild-cmake/releases>, an
`mpv-x86_64-*.7z` file, and extract it anywhere. mpv is portable, so nothing else needs installing.)

## 2. Install the plugin

**Easiest:** from the repository folder run

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\install_mpv_plugin.ps1
```

It finds your mpv config folder, copies `subfix.lua` and `subfix.conf` there, and writes the
path of this checkout's `.venv\Scripts\subfix.exe` into `subfix.conf` (`-Remove` takes the plugin
away again; `-ConfigDir <folder>` overrides the config folder it picks).

**By hand.** mpv uses the first config folder that exists:

1. **Portable:** a `portable_config` folder next to `mpv.exe`. Create it to switch mpv to portable mode.
2. **Per user:** `%APPDATA%\mpv\`.

and the files go to

```
<config>\scripts\subfix.lua
<config>\script-opts\subfix.conf
```

For example, in PowerShell with a portable setup:

```powershell
$cfg = "<mpv folder>\portable_config"
New-Item -ItemType Directory -Force "$cfg\scripts", "$cfg\script-opts" | Out-Null
Copy-Item plugins\mpv\subfix.lua  "$cfg\scripts\"
Copy-Item plugins\mpv\subfix.conf "$cfg\script-opts\"
```

Keep the file name `subfix.lua`. mpv takes the script name from it, and the binding names (`subfix/auto` and so on) and the `subfix.conf` options depend on that name.

Then set `executable=` in `subfix.conf` (next section) unless `subfix` is on your `PATH` or the `SUBFIX_EXE` environment variable points at it.

## 3. Configure

Edit `script-opts\subfix.conf`. Write `key=value`, with no quotes:

| Option | Default | Meaning |
|---|---|---|
| `executable` | empty → `$SUBFIX_EXE`, then `subfix` on PATH | Full path to `subfix.exe`, e.g. `C:\path\to\subfix\.venv\Scripts\subfix.exe` |
| `mode` | `auto` | Sync mode: `auto`, `offset`, `drift`, `piecewise`, `smart` |
| `device` | `auto` | `auto`, `cuda` or `cpu` |
| `model` | (empty) | Whisper model. Leave empty to let subfix choose. |
| `lang` | (empty) | Spoken language code (`en`, `fr`, …). Leave empty to auto-detect. |
| `extra_args` | (empty) | Extra CLI flags, e.g. `--no-cache --audio-track 1` |
| `osd_duration` | `6` | Seconds that the final message stays on screen |
| `use_selected_sub` | `yes` | Sync the selected external subtitle track, not the file subfix finds next to the video |
| `show_progress` | `yes` | Show the `working…` message with the elapsed time |

You can also set options for a single session on the command line:
`mpv --script-opts=subfix-device=cpu,subfix-mode=offset movie.mkv`

## Rebinding keys

Add lines to `<config>\input.conf`:

```
Alt+s  script-binding subfix/auto
Alt+g  script-binding subfix/generate
Alt+t  script-binding subfix/translate
Alt+x  script-binding subfix/cancel
```

The default keys are registered as `Ctrl+S`, `Ctrl+G`, `Ctrl+T` and `Ctrl+X`, which is how mpv names Ctrl+Shift+letter. To turn off a default key, bind it to `ignore` in input.conf, e.g. `Ctrl+S ignore`.

## How it works

The script runs

```
subfix auto <video> --quiet --status [--generate] [--translate] [--subs <file>] [--mode …] [--device …] …
```

through mpv's asynchronous `subprocess` command, passing the arguments as a list. Paths with spaces or non-ASCII characters need no quoting. When subfix exits, the script reads the `SUBFIX-OUTPUT:` line from its stdout, or the `SUBFIX-ERROR:` line if it failed. The output is written next to the video as `<video>.synced.srt` or `<video>.generated.srt`, so it is still there the next time you play the file.

mpv's subprocess command returns output only when the process exits, so the OSD shows the elapsed time rather than live progress.

## Troubleshooting

- **Check the mpv console first.** Press the backtick key `` ` `` in mpv. Lines tagged `[subfix]` show the exact command that ran and subfix's error. For full stdout and stderr, start mpv with `--msg-level=subfix=trace`.
- **`could not start 'subfix'`**: mpv could not find the executable. Set `executable=` in `script-opts\subfix.conf` to the full path of `subfix.exe`.
- **The first run is slow.** Whisper downloads its model first, about 1.6 GB for `large-v3-turbo`. Later runs reuse it, and subfix caches the audio and transcript of each file.
- **It is slow or runs out of GPU memory.** Set `device=cpu`, or choose a smaller `model=` such as `small`.
- **The wrong subtitle file got synced.** Select the subtitle track you want (`j` cycles tracks) before pressing Ctrl+Shift+S, or set `use_selected_sub=no`.
- **`subfix: already running`**: only one job runs at a time. Press Ctrl+Shift+X to cancel it.
- **The key does nothing.** Another script or input.conf entry may use the same key. Rebind it as shown above.

## VLC (not implemented): how a VLC Lua extension could work

VLC supports Lua extensions (`%APPDATA%\vlc\lua\extensions\subfix.lua`). They appear under *View → subfix*. An extension could:

1. Get the current file with `vlc.input.item():uri()` and convert the `file:///` URI to a path with `vlc.strings.decode_uri`.
2. Run subfix with `io.popen('"…\\subfix.exe" auto "' .. path .. '" --quiet --status')` and read lines until it gets `SUBFIX-OUTPUT:`.
3. Load the result with `vlc.input.add_subtitle(path)`.

The problem is that VLC extensions have no asynchronous subprocess API. `io.popen` blocks the extension, and often the VLC interface, until subfix exits, which can take minutes. It also needs a shell command string, so quoting paths with special characters is fragile, and on Windows it briefly opens a console window.

A workable alternative is a small **watcher**. A separate process, such as a Python script or `subfix watch` if that command is ever added, does the long-running work. The VLC extension then only starts that process without waiting for it (for example `start "" /b …` through `os.execute`) and polls for `<video>.synced.srt` or `<video>.generated.srt` from a VLC dialog button or timer. When the file appears, it calls `vlc.input.add_subtitle`. VLC's own UI stays responsive because the extension never waits on the subprocess.

## Testing the plugin without a screen

`tests/headless_test.lua` loads a file, sends a real `Ctrl+S` keypress to the plugin and exits
with code 0 once a subtitle track titled `subfix` has been loaded (3 on a 5-minute timeout):

```powershell
mpv.com --no-config --vo=null --ao=null --pause `
  --script=plugins\mpv\subfix.lua --script=plugins\mpv\tests\headless_test.lua `
  path\to\movie.mkv
```

This was run against mpv 0.42 on Windows 11 with a synthetic film and with Tears of Steel
(corrupted sidecar `.srt` → `*.synced.srt` loaded and selected).
