<#
.SYNOPSIS
    What the "Generate subtitles with subfix" right-click menu runs for one media file.

.DESCRIPTION
    * If the subfix web UI is running (<SUBFIX_HOME>\web\server.json exists and
      GET <url>/api/health answers), the file is added to its queue (POST /api/jobs, mode "auto")
      and this script polls GET /api/jobs/<id> until the job is finished. One GPU queue for
      everything.
    * Otherwise it runs  subfix.exe auto "<file>" --quiet --status  itself (one at a time: a
      second right-click waits for the first to finish) and reads the SUBFIX-OUTPUT /
      SUBFIX-ERROR line.
    Either way it shows a Windows notification with the result, and logs to
    <SUBFIX_HOME>\web\context-menu.log.

    Exit code: 0 = subtitles written, 1 = failed.

.PARAMETER Path
    The video or audio file (Explorer passes "%1").
.PARAMETER SubfixHome
    Overrides SUBFIX_HOME (default: $env:SUBFIX_HOME, else <repo>\.cache for a source checkout,
    else %LOCALAPPDATA%\subfix\Cache - the same rules as subfix itself).
.PARAMETER SubfixExe
    Overrides the subfix executable (default: $env:SUBFIX_EXE, else <repo>\.venv\Scripts\subfix.exe,
    else subfix.exe on PATH).
.PARAMETER DryRun
    Only log/print what would be done (also enabled by $env:SUBFIX_CONTEXT_DRYRUN = 1).
.PARAMETER NoNotify
    Kill switch: display nothing, only log the notifications that would have been shown
    (also $env:SUBFIX_NOTIFY = 'off'). Used by all automated tests.

    Notifications are non-blocking Windows toasts (see subfix_toast.ps1). If Windows
    notifications are switched off in Settings, nothing is displayed (the result is still logged).
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)][string]$Path,
    [string]$SubfixHome,
    [string]$SubfixExe,
    [switch]$DryRun,
    [switch]$NoNotify,
    [ValidateRange(1, 60)][int]$PollSeconds = 2
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
# Read the switches before dot-sourcing the toast helper (its param block reuses some names).
$script:NotifyOff = [bool]$NoNotify
$script:IsDryRun = [bool]$DryRun -or ($env:SUBFIX_CONTEXT_DRYRUN -eq '1')
. (Join-Path $PSScriptRoot 'subfix_toast.ps1')
if (Test-SubfixNotifyOff) { $script:NotifyOff = $true }

# --- locations -------------------------------------------------------------------------------
$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)    # scripts\windows -> repo
$HomeExplicit = $false
if ($SubfixHome) {
    $HomeExplicit = $true
} elseif ($env:SUBFIX_HOME) {
    $SubfixHome = $env:SUBFIX_HOME
} elseif ((Test-Path -LiteralPath (Join-Path $Repo 'pyproject.toml')) -and
          (Test-Path -LiteralPath (Join-Path $Repo 'subfix') -PathType Container)) {
    $SubfixHome = Join-Path $Repo '.cache'
} else {
    $base = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { Join-Path $HOME 'AppData\Local' }
    $SubfixHome = Join-Path $base 'subfix\Cache'
}
if (-not $SubfixExe) { $SubfixExe = $env:SUBFIX_EXE }
if (-not $SubfixExe) {
    $candidate = Join-Path $Repo '.venv\Scripts\subfix.exe'
    if (Test-Path -LiteralPath $candidate) {
        $SubfixExe = $candidate
    } else {
        $cmd = Get-Command subfix.exe -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($cmd) { $SubfixExe = $cmd.Source }
    }
}
$WebDir = Join-Path $SubfixHome 'web'
$LogFile = Join-Path $WebDir 'context-menu.log'
$Utf8 = New-Object System.Text.UTF8Encoding($false)

# --- helpers ---------------------------------------------------------------------------------
function Write-Log([string]$Text) {
    $line = '{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $PID, $Text
    Write-Host $line
    try {
        if (-not (Test-Path -LiteralPath $WebDir)) { $null = New-Item -ItemType Directory -Path $WebDir -Force }
        if ((Test-Path -LiteralPath $LogFile) -and (Get-Item -LiteralPath $LogFile).Length -gt 1MB) {
            Move-Item -LiteralPath $LogFile -Destination "$LogFile.old" -Force
        }
        [System.IO.File]::AppendAllText($LogFile, $line + [Environment]::NewLine, $Utf8)
    } catch { }   # logging must never break the job
}

function Send-Notification {
    param([string]$Title, [string]$Message, [string]$OpenFile, [string]$Folder, [switch]$IsError, [switch]$Important)
    $flat = $Message -replace "`r?`n", ' / '
    if ($script:IsDryRun -or $script:NotifyOff) {
        Write-Log "NOTIFY (not shown): $Title | $flat"
        return
    }
    $how = Show-SubfixToast -Title $Title -Message $Message -OpenFile $OpenFile -Folder $Folder `
        -Tag $script:ToastTag -IsError:$IsError -Important:$Important
    Write-Log "NOTIFY ($how): $Title | $flat"
}

# Small JSON-over-HTTP client. Uses HttpWebRequest instead of Invoke-RestMethod because
# Windows PowerShell 5.1 mangles non-ASCII text (it sends/reads Latin-1 unless told otherwise).
# Returns @{Status = <HTTP code, 0 = no connection>; Data = <parsed JSON>; Error = <message>}.
function Invoke-SubfixApi {
    param([string]$Method, [string]$Url, [string]$Token, $Body, [int]$TimeoutSec = 15)
    $result = [pscustomobject]@{ Status = 0; Data = $null; Error = '' }
    $text = ''
    try {
        $req = [System.Net.HttpWebRequest]::Create($Url)
        $req.Method = $Method
        $req.Proxy = $null
        $req.Timeout = $TimeoutSec * 1000
        $req.ReadWriteTimeout = $TimeoutSec * 1000
        $req.Accept = 'application/json'
        if ($Token) { $req.Headers.Add('X-Subfix-Token', $Token) }
        if ($null -ne $Body) {
            $bytes = $Utf8.GetBytes(($Body | ConvertTo-Json -Depth 6 -Compress))
            $req.ContentType = 'application/json; charset=utf-8'
            $req.ContentLength = $bytes.Length
            $stream = $req.GetRequestStream()
            try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Close() }
        }
        $resp = $null
        try {
            $resp = $req.GetResponse()
        } catch {
            $ex = $_.Exception
            while ($ex -and -not ($ex -is [System.Net.WebException])) { $ex = $ex.InnerException }
            if ($ex -and $ex.Response) { $resp = $ex.Response } else { throw }
        }
        try {
            $result.Status = [int]$resp.StatusCode
            $reader = New-Object System.IO.StreamReader($resp.GetResponseStream(), $Utf8)
            $text = $reader.ReadToEnd()
            $reader.Close()
        } finally { $resp.Close() }
        if ($text) {
            try { $result.Data = $text | ConvertFrom-Json } catch { $result.Data = $null }
        }
        if ($result.Status -ge 400) {
            $msg = "HTTP $($result.Status)"
            if ($result.Data -and ($result.Data.PSObject.Properties.Name -contains 'error') -and $result.Data.error) {
                $msg = "$($result.Data.error.message)"
            }
            $result.Error = $msg
        }
    } catch {
        $result.Status = 0
        $result.Error = $_.Exception.Message
    }
    return $result
}

function Get-JsonProperty($Object, [string]$Name) {
    if ($null -eq $Object) { return $null }
    $p = $Object.PSObject.Properties[$Name]
    if ($p) { return $p.Value } else { return $null }
}

# Returns @{Url; Token} when a subfix web server is running and healthy, else $null.
function Get-RunningServer {
    $file = Join-Path $WebDir 'server.json'
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        Write-Log "no web server session file ($file)"
        return $null
    }
    try {
        $info = [System.IO.File]::ReadAllText($file, $Utf8) | ConvertFrom-Json
    } catch {
        Write-Log "unreadable server.json: $($_.Exception.Message)"
        return $null
    }
    $url = "$(Get-JsonProperty $info 'url')".TrimEnd('/')
    $token = "$(Get-JsonProperty $info 'token')"
    if (-not $url -or -not $token) {
        Write-Log 'server.json has no url/token'
        return $null
    }
    $health = Invoke-SubfixApi -Method GET -Url "$url/api/health" -TimeoutSec 3
    if ($health.Status -eq 200 -and (Get-JsonProperty $health.Data 'app') -eq 'subfix') {
        Write-Log "web server is running at $url"
        return [pscustomobject]@{ Url = $url; Token = $token }
    }
    Write-Log "server.json found but $url/api/health did not answer ($($health.Status) $($health.Error)); not using it"
    return $null
}

function New-Outcome([bool]$Ok, [string]$Output, [string]$Summary, [string]$Reason) {
    return [pscustomobject]@{ Ok = $Ok; Output = $Output; Summary = $Summary; Reason = $Reason }
}

function Invoke-ViaServer($Server, [string]$File) {
    $body = [ordered]@{
        media     = [ordered]@{ path = $File }
        subs      = $null
        mode      = 'auto'
        sync_mode = 'auto'
        translate = $false
        language  = $null
    }
    if ($script:IsDryRun) {
        Write-Log "DRY RUN: would POST $($Server.Url)/api/jobs $($body | ConvertTo-Json -Depth 6 -Compress) (X-Subfix-Token set) and poll until finished"
        return $null
    }
    $r = Invoke-SubfixApi -Method POST -Url "$($Server.Url)/api/jobs" -Token $Server.Token -Body $body
    if ($r.Status -ne 201 -and $r.Status -ne 200) {
        Write-Log "POST /api/jobs failed: $($r.Status) $($r.Error)"
        $reason = if ($r.Status -eq 0) { "could not reach the subfix web UI ($($r.Error))" } else { $r.Error }
        return New-Outcome $false '' '' $reason
    }
    $job = $r.Data
    $id = "$(Get-JsonProperty $job 'id')"
    Write-Log "submitted job $id (state $(Get-JsonProperty $job 'state'), queue position $(Get-JsonProperty $job 'queue_position'))"
    $pos = Get-JsonProperty $job 'queue_position'
    $where = if ($pos -and [int]$pos -gt 1) { "Position $pos in the queue. " } else { '' }
    Send-Notification -Title "Added to the subfix queue: $(Split-Path -Leaf $File)" `
        -Message "${where}Progress is shown in the subfix web UI ($($Server.Url))."

    $jobUrl = "$($Server.Url)/api/jobs/$([System.Uri]::EscapeDataString($id))"
    $lastState = ''
    $lostSince = $null
    while ($true) {
        $state = "$(Get-JsonProperty $job 'state')"
        if ($state -ne $lastState) { Write-Log "job $id state: $state"; $lastState = $state }
        switch ($state) {
            'done' {
                $res = Get-JsonProperty $job 'result'
                $notes = @(Get-JsonProperty $res 'notes') | Where-Object { $_ }
                foreach ($n in $notes) { Write-Log "note: $n" }
                return New-Outcome $true "$(Get-JsonProperty $res 'output_path')" "$(Get-JsonProperty $res 'summary')" ''
            }
            'failed' {
                $err = Get-JsonProperty $job 'error'
                $msg = "$(Get-JsonProperty $err 'message')"
                if (-not $msg) { $msg = 'the job failed (no message)' }
                Write-Log "job failed: $(Get-JsonProperty $err 'code'): $msg"
                return New-Outcome $false '' '' $msg
            }
            'cancelled' { return New-Outcome $false '' '' 'the job was cancelled' }
        }
        Start-Sleep -Seconds $PollSeconds
        $r = Invoke-SubfixApi -Method GET -Url $jobUrl -Token $Server.Token
        if ($r.Status -eq 200 -and $r.Data) {
            $job = $r.Data
            $lostSince = $null
        } elseif ($r.Status -eq 0) {
            if (-not $lostSince) { $lostSince = Get-Date; Write-Log "lost contact with the web UI: $($r.Error)" }
            if (((Get-Date) - $lostSince).TotalSeconds -ge 60) {
                return New-Outcome $false '' '' 'the subfix web UI was closed before the job finished'
            }
        } else {
            Write-Log "GET $jobUrl failed: $($r.Status) $($r.Error)"
            return New-Outcome $false '' '' $r.Error
        }
    }
}

function Invoke-ViaCli([string]$File) {
    if (-not $SubfixExe -or -not (Test-Path -LiteralPath $SubfixExe -PathType Leaf)) {
        return New-Outcome $false '' '' "subfix.exe not found (expected $(Join-Path $Repo '.venv\Scripts\subfix.exe')). Is subfix installed?"
    }
    $arguments = 'auto "' + $File + '" --quiet --status'
    if ($script:IsDryRun) {
        Write-Log "DRY RUN: would run `"$SubfixExe`" $arguments"
        return $null
    }

    # Only one CLI job at a time (GPU memory); later right-clicks wait here. While waiting,
    # switch to the web UI's queue if it gets started.
    $mutex = New-Object System.Threading.Mutex($false, 'Local\subfix-context-menu-cli')
    $owned = $false
    $notifiedWait = $false
    try {
        $wait = 0
        while (-not $owned) {
            try { $owned = $mutex.WaitOne($wait) } catch [System.Threading.AbandonedMutexException] { $owned = $true }
            $wait = 5000
            if ($owned) { break }
            if (-not $notifiedWait) {
                Write-Log 'another subfix job started from the menu is running; waiting'
                Send-Notification -Title "Waiting: $(Split-Path -Leaf $File)" `
                    -Message 'Another subfix job is running. This one starts when it is finished.'
                $notifiedWait = $true
            }
            $server = Get-RunningServer
            if ($server) { return Invoke-ViaServer $server $File }
        }

        Write-Log "running: `"$SubfixExe`" $arguments"
        Send-Notification -Title "Making subtitles: $(Split-Path -Leaf $File)" `
            -Message 'This can take a few minutes. You will get another notification when it is done.'

        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $SubfixExe
        $psi.Arguments = $arguments
        $psi.WorkingDirectory = Split-Path -Parent $File
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.StandardOutputEncoding = $Utf8
        $psi.StandardErrorEncoding = $Utf8
        $psi.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
        $psi.EnvironmentVariables['PYTHONUTF8'] = '1'
        if ($HomeExplicit) { $psi.EnvironmentVariables['SUBFIX_HOME'] = $SubfixHome }

        $proc = [System.Diagnostics.Process]::Start($psi)
        $stderrTask = $proc.StandardError.ReadToEndAsync()
        $output = ''
        $errorLine = ''
        $lastLine = ''
        while ($null -ne ($line = $proc.StandardOutput.ReadLine())) {
            if (-not $line.Trim()) { continue }
            Write-Log "subfix: $line"
            if ($line -match '^SUBFIX-OUTPUT:\s*(.+?)\s*$') { $output = $Matches[1] }
            elseif ($line -match '^SUBFIX-ERROR:\s*(.+?)\s*$') { $errorLine = $Matches[1] }
            elseif ($line -notmatch '^SUBFIX-') { $lastLine = $line.Trim() }
        }
        $proc.WaitForExit()
        $stderr = $stderrTask.Result
        $code = $proc.ExitCode
        foreach ($l in ($stderr -split "`r?`n")) { if ($l.Trim()) { Write-Log "subfix (stderr): $l"; $lastLine = $l.Trim() } }
        Write-Log "subfix exited with code $code"

        if ($code -eq 0 -and $output) { return New-Outcome $true $output '' '' }
        if ($errorLine) { return New-Outcome $false '' '' $errorLine }
        if ($code -eq 0) { return New-Outcome $false '' '' 'subfix finished without reporting an output file' }
        $reason = "subfix exited with code $code"
        if ($lastLine) { $reason += ": $lastLine" }
        return New-Outcome $false '' '' $reason
    } finally {
        if ($owned) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}

# --- main ------------------------------------------------------------------------------------
$script:ToastTag = ''
$exitCode = 1
try {
    Write-Log "---- right-click: $Path"
    Write-Log "SUBFIX_HOME=$SubfixHome; subfix.exe=$SubfixExe$(if ($script:IsDryRun) { '; DRY RUN' })"
    if (-not $Path) { throw 'no file was given' }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "file not found: $Path" }
    $file = (Get-Item -LiteralPath $Path).FullName
    $name = Split-Path -Leaf $file
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $script:ToastTag = 'subfix-' + (($sha.ComputeHash($Utf8.GetBytes($file.ToLowerInvariant())) |
        Select-Object -First 8 | ForEach-Object { $_.ToString('x2') }) -join '')

    $server = Get-RunningServer
    $outcome = if ($server) { Invoke-ViaServer $server $file } else { Invoke-ViaCli $file }

    if ($null -eq $outcome) {
        $exitCode = 0   # dry run
    } elseif ($outcome.Ok) {
        $outName = if ($outcome.Output) { Split-Path -Leaf $outcome.Output } else { $name }
        $folder = if ($outcome.Output) { Split-Path -Parent $outcome.Output } else { Split-Path -Parent $file }
        Write-Log "OK: $($outcome.Output) $($outcome.Summary)"
        $msg = if ($outcome.Summary) { "$name - $($outcome.Summary)" } else { $name }
        Send-Notification -Title "Subtitles ready: $outName" -Message "$msg`nSaved in $folder" `
            -OpenFile $outcome.Output -Folder $folder -Important
        $exitCode = 0
    } else {
        Write-Log "FAILED: $($outcome.Reason)"
        Send-Notification -Title "subfix failed: $($outcome.Reason)" -Message "$name`nDetails: $LogFile" `
            -IsError -Important
    }
} catch {
    $reason = $_.Exception.Message
    Write-Log "FAILED: $reason"
    Write-Log "$($_.ScriptStackTrace)"
    Send-Notification -Title "subfix failed: $reason" -Message "Details: $LogFile" -IsError -Important
}
exit $exitCode
