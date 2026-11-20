<#
.SYNOPSIS
    Adds "Generate subtitles with subfix" to the right-click menu of video and audio files.

.DESCRIPTION
    Current user only (no administrator rights needed). For every video/audio extension subfix
    supports (read from subfix\formats.py, with a built-in fallback list) it writes:

        HKEY_CURRENT_USER\Software\Classes\SystemFileAssociations\<ext>\shell\subfix
            (Default)          = Generate subtitles with subfix
            MUIVerb            = Generate subtitles with subfix
            Icon               = <this folder>\subfix.ico
            SubfixCreatedKeys  = which parent keys this script had to create (so the uninstaller
                                 can remove exactly those again)
        ...\shell\subfix\command
            (Default)          = "<powershell.exe>" -NoProfile -ExecutionPolicy Bypass
                                 -WindowStyle Hidden -File "<this folder>\subfix_context.ps1" "%1"

    Running it again just refreshes the values (e.g. after moving the subfix folder).
    Undo with uninstall_context_menu.ps1. On Windows 11 the entry is under "Show more options".
#>
[CmdletBinding()]
param([switch]$NoPause)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$Verb = 'subfix'
$Label = 'Generate subtitles with subfix'
$Base = 'Software\Classes\SystemFileAssociations'
$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$ContextScript = Join-Path $PSScriptRoot 'subfix_context.ps1'
$IconFile = Join-Path $PSScriptRoot 'subfix.ico'
$PowerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

# Same as subfix.formats (docs/WEB_API.md section 4); used if formats.py can't be read.
$FallbackExtensions = @(
    '.mkv', '.mp4', '.avi', '.mov', '.webm', '.m4v', '.wmv', '.flv', '.ts', '.m2ts',
    '.mp3', '.wav', '.m4a', '.flac', '.aac', '.ogg', '.opus'
)

function Wait-IfDoubleClicked {
    # Keep the window open when started with "Run with PowerShell" from Explorer.
    if ($NoPause) { return }
    try {
        $parentId = (Get-CimInstance Win32_Process -Filter "ProcessId=$PID").ParentProcessId
        if ((Get-Process -Id $parentId -ErrorAction Stop).ProcessName -eq 'explorer') {
            $null = Read-Host 'Press Enter to close'
        }
    } catch { }
}

function Get-MediaExtensions {
    $formats = Join-Path $Repo 'subfix\formats.py'
    try {
        $text = [System.IO.File]::ReadAllText($formats)
        $found = @()
        foreach ($name in 'VIDEO_EXTENSIONS', 'AUDIO_EXTENSIONS') {
            $m = [regex]::Match($text, "(?s)\b$name\s*=\s*\((.*?)\)")
            if (-not $m.Success) { throw "$name not found" }
            $found += [regex]::Matches($m.Groups[1].Value, '"(\.[A-Za-z0-9]+)"') | ForEach-Object { $_.Groups[1].Value.ToLowerInvariant() }
        }
        if ($found.Count -lt 5) { throw 'too few extensions' }
        Write-Host "Extensions from $formats"
        return $found | Select-Object -Unique
    } catch {
        Write-Host "Could not read $formats ($($_.Exception.Message)); using the built-in list."
        return $FallbackExtensions
    }
}

function Test-Key([string]$SubPath) {
    $k = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($SubPath)
    if ($k) { $k.Close(); return $true }
    return $false
}

function Update-ShellAssociations {
    # Tell Explorer that file associations changed (SHCNE_ASSOCCHANGED).
    try {
        if (-not ('SubfixShell.Notify' -as [type])) {
            Add-Type -Namespace SubfixShell -Name Notify -MemberDefinition `
                '[DllImport("shell32.dll")] public static extern void SHChangeNotify(int eventId, int flags, System.IntPtr item1, System.IntPtr item2);'
        }
        [SubfixShell.Notify]::SHChangeNotify(0x08000000, 0, [IntPtr]::Zero, [IntPtr]::Zero)
    } catch { }
}

try {
    if (-not (Test-Path -LiteralPath $ContextScript)) { throw "missing $ContextScript" }
    if (-not (Test-Path -LiteralPath $PowerShellExe)) { $PowerShellExe = 'powershell.exe' }
    $command = "`"$PowerShellExe`" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ContextScript`" `"%1`""
    $exe = Join-Path $Repo '.venv\Scripts\subfix.exe'
    if (-not (Test-Path -LiteralPath $exe)) {
        Write-Warning "$exe not found. The menu will be installed, but it can only work once subfix is installed (see SETUP.md)."
    }

    $hkcu = [Microsoft.Win32.Registry]::CurrentUser
    $baseExisted = Test-Key $Base
    $count = 0
    foreach ($ext in (Get-MediaExtensions)) {
        $extPath = "$Base\$ext"
        $shellPath = "$extPath\shell"
        $verbPath = "$shellPath\$Verb"

        $created = @()
        if (-not $baseExisted -and $count -eq 0) { $created += 'base' }
        if (-not (Test-Key $extPath)) { $created += 'ext' }
        if (-not (Test-Key $shellPath)) { $created += 'shell' }
        $old = $hkcu.OpenSubKey($verbPath)
        if ($old) {
            # Re-install: keep what the first install recorded.
            $prev = $old.GetValue('SubfixCreatedKeys', $null)
            $old.Close()
            if ($null -ne $prev) { $created = @("$prev".Split(',') | Where-Object { $_ }) }
        }

        $key = $hkcu.CreateSubKey($verbPath)
        try {
            $key.SetValue('', $Label)
            $key.SetValue('MUIVerb', $Label)
            if (Test-Path -LiteralPath $IconFile) { $key.SetValue('Icon', $IconFile) }
            elseif ($key.GetValue('Icon', $null)) { $key.DeleteValue('Icon') }
            $key.SetValue('SubfixCreatedKeys', ($created -join ','))
            $cmdKey = $key.CreateSubKey('command')
            try { $cmdKey.SetValue('', $command) } finally { $cmdKey.Close() }
        } finally { $key.Close() }
        Write-Host ("  HKEY_CURRENT_USER\{0}" -f $verbPath)
        $count++
    }
    Update-ShellAssociations
    Write-Host ''
    Write-Host "Done: '$Label' added for $count file types."
    Write-Host 'Right-click a video or audio file (on Windows 11: "Show more options").'
    Write-Host "Command: $command"
    Wait-IfDoubleClicked
    exit 0
} catch {
    Write-Host "Install failed: $($_.Exception.Message)" -ForegroundColor Red
    Wait-IfDoubleClicked
    exit 1
}
