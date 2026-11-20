-- subfix.lua: mpv front-end for the subfix CLI.
--
-- Ctrl+Shift+S  sync the subtitles next to the video (or the selected external
--               subtitle track); generate new ones with Whisper if there are none
-- Ctrl+Shift+G  always generate subtitles from the audio
-- Ctrl+Shift+T  generate English subtitles (Whisper translation)
-- Ctrl+Shift+X  cancel the running job
--
-- Rebind in input.conf, e.g.:   Alt+s script-binding subfix/auto
-- Binding names: subfix/auto, subfix/generate, subfix/translate, subfix/cancel
--
-- Options: script-opts/subfix.conf (see the example shipped next to this file).
-- subfix runs as a background subprocess, so playback continues meanwhile.

local mp = require "mp"
local msg = require "mp.msg"
local utils = require "mp.utils"
local options = require "mp.options"

local opts = {
    -- Path to subfix(.exe). Empty = $SUBFIX_EXE if set, otherwise `subfix` on PATH.
    -- scripts/windows/install_mpv_plugin.ps1 fills this in with your venv's subfix.exe.
    executable = "",
    -- Extra arguments appended to every call, e.g.  --no-cache --audio-track 1
    extra_args = "",
    -- Sync mode: auto | offset | drift | piecewise | smart
    mode = "auto",
    -- Whisper device: auto | cuda | cpu
    device = "auto",
    -- Whisper model (empty = subfix picks one for your hardware)
    model = "",
    -- Spoken language code, e.g. en (empty = auto-detect)
    lang = "",
    -- Seconds the final success/error OSD message stays on screen
    osd_duration = 6,
    -- Sync the currently selected external subtitle track instead of letting
    -- subfix look for one next to the video
    use_selected_sub = true,
    -- Show the "working..." OSD message with elapsed time while subfix runs
    show_progress = true,
}
options.read_options(opts, "subfix")

local IS_WINDOWS = package.config:sub(1, 1) == "\\"
local SUB_EXTENSIONS = { srt = true, vtt = true, ass = true }
local CANCEL_HINT = "Ctrl+Shift+X cancels"

local job = nil -- the running job, or nil

---------------------------------------------------------------------------
-- helpers
---------------------------------------------------------------------------

local function osd(text, seconds)
    mp.osd_message(text, seconds or opts.osd_duration)
end

local function trim(s)
    return (s:gsub("^%s+", ""):gsub("%s+$", ""))
end

local function basename(path)
    return path:match("([^/\\]+)$") or path
end

local function is_url(path)
    return path:match("^%a[%w+.-]+://") ~= nil
end

local function is_absolute(path)
    return path:match("^%a:[/\\]") ~= nil or path:match("^[/\\]") ~= nil
end

local function absolute(path)
    if is_absolute(path) then
        return path
    end
    local cwd = mp.get_property("working-directory")
    if not cwd or cwd == "" then
        cwd = utils.getcwd() or ""
    end
    return utils.join_path(cwd, path)
end

-- Comparable form of a path (on Windows: case-insensitive, either slash).
local function path_key(path)
    path = absolute(path)
    if IS_WINDOWS then
        path = path:gsub("/", "\\"):lower()
    end
    return path
end

local function is_file(path)
    local info = utils.file_info(path)
    return info ~= nil and info.is_file
end

-- Split [[a b 'c d' "e f"]] into {"a", "b", "c d", "e f"}.
local function split_args(s)
    local out, cur, quote, have = {}, {}, nil, false
    for ch in s:gmatch(".") do
        if quote then
            if ch == quote then quote = nil else cur[#cur + 1] = ch end
        elseif ch == '"' or ch == "'" then
            quote, have = ch, true
        elseif ch:match("%s") then
            if have or #cur > 0 then out[#out + 1] = table.concat(cur) end
            cur, have = {}, false
        else
            cur[#cur + 1] = ch
        end
    end
    if have or #cur > 0 then out[#out + 1] = table.concat(cur) end
    return out
end

local function format_elapsed(seconds)
    seconds = math.floor(seconds)
    return string.format("%d:%02d", math.floor(seconds / 60), seconds % 60)
end

local function resolve_executable()
    if opts.executable ~= "" then
        return mp.command_native({ "expand-path", opts.executable }) or opts.executable
    end
    local from_env = os.getenv("SUBFIX_EXE")
    if from_env and from_env ~= "" then
        return from_env
    end
    return "subfix" -- rely on PATH
end

-- Environment for the child: mpv's own plus UTF-8 stdout for Python, so
-- SUBFIX-OUTPUT paths with non-ASCII characters survive the pipe on Windows.
local function child_env()
    if not utils.get_env_list then
        return nil
    end
    local env = utils.get_env_list()
    for _, entry in ipairs(env) do
        if entry:match("^PYTHONIOENCODING=") then
            return env
        end
    end
    env[#env + 1] = "PYTHONIOENCODING=utf-8"
    return env
end

---------------------------------------------------------------------------
-- what to work on
---------------------------------------------------------------------------

-- Absolute path of the playing file, or nil plus a reason.
local function local_media_path()
    local path = mp.get_property("path")
    if not path or path == "" then
        return nil, "no file is playing"
    end
    if is_url(path) or mp.get_property_bool("demuxer-via-network", false) then
        return nil, "subfix only works on local files"
    end
    path = absolute(path)
    if not is_file(path) then
        return nil, "subfix only works on local files"
    end
    return path
end

-- The selected (primary) external subtitle file, if subfix can sync it.
local function selected_external_sub()
    local sid = mp.get_property_number("sid")
    if not sid then
        return nil
    end
    for _, track in ipairs(mp.get_property_native("track-list") or {}) do
        if track.type == "sub" and track.id == sid then
            local file = track.external and track["external-filename"]
            if not file or file == "" or is_url(file) then
                return nil
            end
            local name = basename(file):lower()
            local ext = name:match("%.([^.]+)$")
            if not ext or not SUB_EXTENSIONS[ext] then
                msg.info("selected subtitle " .. name .. " is not .srt/.vtt/.ass; not using it")
                return nil
            end
            if name:find(".synced.", 1, true) or name:find(".generated.", 1, true) then
                return nil -- subfix's own output: let subfix find the original
            end
            file = absolute(file)
            return is_file(file) and file or nil
        end
    end
    return nil
end

local function build_args(exe, kind, media, subs)
    local args = { exe, "auto", media, "--quiet", "--status" }
    local function add(...)
        for _, a in ipairs({ ... }) do args[#args + 1] = a end
    end
    if kind == "generate" then
        add("--generate")
    elseif kind == "translate" then
        add("--generate", "--translate")
    end
    if subs then add("--subs", subs) end
    if opts.mode ~= "" and opts.mode ~= "auto" then add("--mode", opts.mode) end
    if opts.device ~= "" and opts.device ~= "auto" then add("--device", opts.device) end
    if opts.model ~= "" then add("--model", opts.model) end
    if opts.lang ~= "" then add("--lang", opts.lang) end
    for _, a in ipairs(split_args(opts.extra_args)) do add(a) end
    return args
end

---------------------------------------------------------------------------
-- results
---------------------------------------------------------------------------

local function lines(text)
    local out = {}
    for line in ((text or "") .. "\n"):gmatch("([^\n]*)\n") do
        line = trim(line)
        if line ~= "" then out[#out + 1] = line end
    end
    return out
end

-- Load (or reload) an .srt as a subtitle track and select it.
local function load_subtitle(path)
    local key = path_key(path)
    for _, track in ipairs(mp.get_property_native("track-list") or {}) do
        local file = track.type == "sub" and track.external and track["external-filename"]
        if file and path_key(file) == key then
            mp.commandv("sub-remove", tostring(track.id))
        end
    end
    mp.commandv("sub-add", path, "select", "subfix")
end

-- Fallback when the SUBFIX-OUTPUT line is missing or unusable: the file
-- subfix writes by default, if it was (re)written during this job.
local function guess_output(j)
    local stem = j.media:gsub("%.[^./\\]*$", "")
    local names
    if j.kind ~= "auto" then
        names = { stem .. ".generated.srt" }
    elseif j.subs then
        names = { stem .. ".synced.srt" }
    else
        names = { stem .. ".synced.srt", stem .. ".generated.srt" }
    end
    for _, name in ipairs(names) do
        local info = utils.file_info(name)
        if info and info.is_file and info.mtime and info.mtime >= j.started_at - 2 then
            return name
        end
    end
    return nil
end

local function finish(j, success, res, err)
    if j.timer then j.timer:kill() end
    job = nil
    res = res or {}

    local stdout, stderr = lines(res.stdout), lines(res.stderr)
    msg.verbose("exit status: " .. tostring(res.status) .. ", error: " .. tostring(res.error_string))
    for _, l in ipairs(stdout) do msg.verbose("stdout: " .. l) end
    for _, l in ipairs(stderr) do msg.verbose("stderr: " .. l) end

    if j.cancelled or res.killed_by_us then
        osd("subfix: cancelled", 3)
        msg.info("cancelled")
        return
    end

    local output, error_line
    for _, l in ipairs(stdout) do
        local o = l:match("^SUBFIX%-OUTPUT:%s*(.-)$")
        local e = l:match("^SUBFIX%-ERROR:%s*(.-)$")
        if o and o ~= "" then output = o end
        if e and e ~= "" then error_line = e end
    end

    local not_started = not success or res.error_string == "init"
    if not_started or (res.status or 0) ~= 0 or error_line then
        local reason = error_line
        if not reason and not_started then
            reason = "could not start '" .. j.args[1] .. "' (" .. tostring(err or res.error_string)
                .. ") - set executable= in script-opts/subfix.conf"
        end
        if not reason then
            reason = stderr[#stderr] or ("exited with code " .. tostring(res.status))
        end
        osd("subfix: " .. reason)
        msg.error(reason)
        for i = math.max(1, #stderr - 5), #stderr do msg.warn(stderr[i]) end
        return
    end

    if output then output = absolute(output) end
    if not output or not is_file(output) then
        if output then msg.warn("reported output not found: " .. output) end
        output = guess_output(j)
    end
    if not output then
        osd("subfix: finished, but no output file was reported")
        msg.error("no SUBFIX-OUTPUT line in subfix's output")
        return
    end

    local now = mp.get_property("path")
    if not now or is_url(now) or path_key(now) ~= path_key(j.media) then
        osd("subfix: wrote " .. basename(output) .. " (not loaded: another file is playing)")
        msg.info("wrote " .. output)
        return
    end
    load_subtitle(output)
    osd("subfix: loaded " .. basename(output))
    msg.info("loaded " .. output)
end

---------------------------------------------------------------------------
-- running
---------------------------------------------------------------------------

local function show_progress(j)
    if opts.show_progress then
        mp.osd_message(string.format("subfix: working… %s  (%s; %s)",
            format_elapsed(mp.get_time() - j.clock), j.label, CANCEL_HINT), 2)
    end
end

local function launch(j, env)
    local cmd = {
        name = "subprocess",
        args = j.args,
        playback_only = false,
        capture_stdout = true,
        capture_stderr = true,
    }
    if env then cmd.env = env end
    j.id = mp.command_native_async(cmd, function(success, res, err)
        if job ~= j then return end
        if not success and env and not j.cancelled then
            -- Older mpv without the "env" argument: retry with the plain environment.
            msg.verbose("subprocess with env failed (" .. tostring(err) .. "), retrying without")
            launch(j, nil)
            return
        end
        finish(j, success, res, err)
    end)
end

local function run(kind)
    if job then
        osd("subfix: already running (" .. CANCEL_HINT .. ")", 3)
        return
    end
    local media, why = local_media_path()
    if not media then
        osd("subfix: " .. why, 4)
        return
    end

    local subs = nil
    if kind == "auto" and opts.use_selected_sub then
        subs = selected_external_sub()
    end

    local label
    if subs then
        label = "syncing " .. basename(subs)
    elseif kind == "generate" then
        label = "generating subtitles"
    elseif kind == "translate" then
        label = "generating English subtitles"
    else
        label = "syncing or generating"
    end

    local j = {
        kind = kind,
        media = media,
        subs = subs,
        label = label,
        args = build_args(resolve_executable(), kind, media, subs),
        clock = mp.get_time(),
        started_at = os.time(),
    }
    msg.info("running: " .. table.concat(j.args, " | "))
    job = j
    show_progress(j)
    j.timer = mp.add_periodic_timer(1, function() show_progress(j) end)
    launch(j, child_env())
end

local function cancel()
    if not job then
        osd("subfix: nothing to cancel", 2)
        return
    end
    job.cancelled = true
    if job.id then mp.abort_async_command(job.id) end
    osd("subfix: cancelling…", 2)
end

-- mpv reports Ctrl+Shift+<letter> as Ctrl+<UPPERCASE letter>.
mp.add_key_binding("Ctrl+S", "auto", function() run("auto") end)
mp.add_key_binding("Ctrl+G", "generate", function() run("generate") end)
mp.add_key_binding("Ctrl+T", "translate", function() run("translate") end)
mp.add_key_binding("Ctrl+X", "cancel", cancel)

mp.register_event("shutdown", function()
    if job and job.id then
        job.cancelled = true
        mp.abort_async_command(job.id)
    end
end)
