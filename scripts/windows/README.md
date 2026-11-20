# subfix on Windows: desktop shortcut and right-click menu

These helpers make subfix easy to use without typing commands:

| File | What it does |
|---|---|
| `subfix-web.bat` | Starts the subfix web UI and opens it in your browser. |
| `create_desktop_shortcut.ps1` | Puts a **subfix** icon on your Desktop that starts the web UI (`-Remove` takes it away). |
| `install_context_menu.ps1` | Adds **Generate subtitles with subfix** to the right-click menu of video and audio files. |
| `uninstall_context_menu.ps1` | Removes that menu entry again. |
| `install_mpv_plugin.ps1` | Installs the [mpv plugin](../../plugins/mpv/README.md) into your mpv config folder and fills in the path to subfix (`-Remove` uninstalls). |
| `subfix_context.ps1` | What the menu entry runs (you never need to start it yourself). |
| `subfix_toast.ps1` | Shows the "Subtitles ready" / "subfix failed" notifications (Windows toasts). |
| `subfix.ico`, `subfix.png` | Icons. |

Nothing here needs administrator rights, and nothing is changed for other users of the PC.

## Before you start

subfix itself must be installed first (see the install steps in the main README). You can check: the
file `.venv\Scripts\subfix.exe` must exist inside the subfix folder. If it doesn't,
`subfix-web.bat` tells you so and waits for a key press.

## 1. Start the web UI

**Double-click `subfix-web.bat`** (in the folder `scripts\windows` inside the subfix folder).

* A black window titled **subfix web UI** opens, and your browser shows the subfix page
  (`http://127.0.0.1:8765`). The page only works on this PC; nothing is sent to the internet.
* **Keep the black window open** while you use subfix. Close it (or press `Ctrl+C` in it) to
  stop subfix.
* If something goes wrong, the window stays open and shows the error. Press a key to close it.

## 2. Put a shortcut on your Desktop (optional)

1. Open the folder `scripts\windows` in File Explorer.
2. Right-click **`create_desktop_shortcut.ps1`** and choose **Run with PowerShell**.
3. A blue window says `Created ...\Desktop\subfix.lnk`. Press Enter to close it.

You now have a **subfix** icon on your Desktop. Double-click it to start the web UI.
(Works with a OneDrive Desktop too.)

To remove the shortcut, just delete it, or run in PowerShell:
`powershell -ExecutionPolicy Bypass -File create_desktop_shortcut.ps1 -Remove`

## 3. Add "Generate subtitles with subfix" to the right-click menu (optional)

1. Open the folder `scripts\windows` in File Explorer.
2. Right-click **`install_context_menu.ps1`** and choose **Run with PowerShell**.
3. The window lists the file types it added (`.mkv`, `.mp4`, `.mp3`, ...) and ends with
   `Done`. Press Enter to close it.

Now right-click any video or audio file:

* **Windows 11**: click **Show more options** first (or hold **Shift** while you right-click),
  then **Generate subtitles with subfix**.
* **Windows 10**: the entry is directly in the menu.

What happens next:

* A notification in the corner of the screen says the job started. Making subtitles takes a
  few minutes (longer for long films). You can keep using your PC.
* When it is done, a notification says **Subtitles ready: _name_.srt**. The `.srt` file is
  saved **next to the video**, e.g. `Movie.mkv` -> `Movie.generated.srt` (or
  `Movie.synced.srt` if a subtitle file was already there and subfix fixed its timing). Use the
  **Open subtitles** or **Open folder** button (clicking the notification also opens the
  folder).
* If it fails, the notification says **subfix failed: _reason_**.
* If the **web UI is open**, the file is added to its queue instead, and you can watch the
  progress on the web page. Otherwise subfix works quietly in the background. If you
  right-click several files, they are done one after another.

Supported files: video `.mkv .mp4 .avi .mov .webm .m4v .wmv .flv .ts .m2ts`, audio
`.mp3 .wav .m4a .flac .aac .ogg .opus`.

### About the notifications

* They are normal Windows notifications ("toasts"): they slide in at the bottom-right corner,
  disappear by themselves after a few seconds and never block anything. Missed ones are in the
  notification centre (click the clock). They appear under the name **Windows PowerShell**
  (subfix borrows PowerShell's notification identity so nothing has to be registered).
* **No notifications?** They are probably switched off: Settings > System > Notifications
  (turn on "Notifications" and make sure "Windows PowerShell" is allowed). With notifications
  off, subfix shows nothing; the result is still written next to the video and in the log
  file (see Troubleshooting).
* Only if the Windows notification system itself is broken does subfix fall back to a small
  message box for the final result (it closes itself after 5 minutes).
* **Turn notifications off for subfix only** (kill switch): set the user environment variable
  `SUBFIX_NOTIFY` to `off` (Start menu > "Edit environment variables for your account" > New:
  name `SUBFIX_NOTIFY`, value `off`). subfix then displays nothing and only writes
  `NOTIFY (not shown): ...` lines to the log. Delete the variable to get notifications back.

### Remove the menu entry

Right-click **`uninstall_context_menu.ps1`** and choose **Run with PowerShell**. Only the
subfix entry is removed; other menu entries are left alone.

If you move the subfix folder, run `install_context_menu.ps1` again (it simply updates the
entry), and re-create the Desktop shortcut.

## Troubleshooting

* **"Run with PowerShell" is missing / the script opens in Notepad**: open the Start menu,
  type `powershell`, open **Windows PowerShell**, and run (adjust the folder):
  ```
  cd "<folder where you cloned subfix>\scripts\windows"
  powershell -ExecutionPolicy Bypass -File install_context_menu.ps1
  ```
* **Nothing happens after clicking the menu entry**: look at the log file
  `.cache\web\context-menu.log` in the subfix folder (or `%SUBFIX_HOME%\web\context-menu.log`
  if you set `SUBFIX_HOME`). Every right-click writes what it did and any error there.
* **A window flashes briefly** when you click the menu entry: that is PowerShell starting
  hidden; it is normal.
* The menu entry is hidden when more than 15 files are selected (a Windows limit).

## Technical details

* The installer writes only to `HKEY_CURRENT_USER\Software\Classes\SystemFileAssociations\<ext>\shell\subfix`
  (values `(Default)` / `MUIVerb` = "Generate subtitles with subfix", `Icon`,
  `SubfixCreatedKeys`, and a `command` subkey) for each extension listed in
  `subfix/formats.py`. `SubfixCreatedKeys` records which parent keys did not exist before, so
  the uninstaller can restore the registry exactly. Re-running the installer is safe.
* The menu command is
  `powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "<folder>\subfix_context.ps1" "%1"`.
* `subfix_context.ps1` reads `<SUBFIX_HOME>\web\server.json`; if `GET <url>/api/health`
  answers it sends `POST /api/jobs` (header `X-Subfix-Token`) and polls `GET /api/jobs/<id>`
  every 2 s; otherwise it runs `subfix.exe auto "<file>" --quiet --status` (one at a time, via
  a per-user mutex) and reads the `SUBFIX-OUTPUT:` / `SUBFIX-ERROR:` line. Exit code 0 = OK.
* Notifications: `subfix_toast.ps1` builds a `ToastGeneric` toast (`duration="short"`, tag
  per file so "working" is replaced by "ready") and shows it with the WinRT
  `ToastNotificationManager` under Windows PowerShell's AppUserModelID
  `{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe`. Buttons use
  `activationType="protocol"` with `file:///` URIs of the `.srt` and its folder, so no COM
  activator is needed. Fallback (message box, auto-closing, final result only) only when the
  toast API throws. No tray (NotifyIcon) balloons.
* **Kill switch for automated tests:** `SUBFIX_NOTIFY=off` (also `log`) or `-NoNotify` on
  `subfix_context.ps1` / `subfix_toast.ps1`: nothing is displayed, each notification is
  recorded as `NOTIFY (not shown): <title> | <message>` in the log (and on stdout). All tests
  must set it. Real displays are logged as `NOTIFY (toast)`, `NOTIFY (toast-disabled)`
  (handed to Windows but notifications are switched off) or `NOTIFY (messagebox)`.
* Other overrides for testing: `-SubfixHome` / `SUBFIX_HOME`, `-SubfixExe` / `SUBFIX_EXE`,
  `-DryRun` / `SUBFIX_CONTEXT_DRYRUN=1` (only logs what it would do; implies no
  notifications). `subfix-web.bat` honours
  `SUBFIX_WEB_DRYRUN=1` (prints the command instead of running it) and passes extra arguments
  on to `subfix web` (e.g. `--port 8800`, `--no-browser`).
