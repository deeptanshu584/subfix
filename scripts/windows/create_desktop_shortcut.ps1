<#
.SYNOPSIS
    Puts a "subfix" shortcut on your Desktop that starts the subfix web UI.

.DESCRIPTION
    Creates <Desktop>\subfix.lnk -> scripts\windows\subfix-web.bat (working folder = the subfix
    folder, subfix icon). The Desktop folder is looked up from Windows, so a Desktop that
    OneDrive moved (e.g. C:\Users\you\OneDrive\Desktop) works too.

    -Remove deletes the shortcut again.
#>
[CmdletBinding()]
param(
    [switch]$Remove,
    [switch]$NoPause
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Launcher = Join-Path $PSScriptRoot 'subfix-web.bat'
$IconFile = Join-Path $PSScriptRoot 'subfix.ico'

function Wait-IfDoubleClicked {
    if ($NoPause) { return }
    try {
        $parentId = (Get-CimInstance Win32_Process -Filter "ProcessId=$PID").ParentProcessId
        if ((Get-Process -Id $parentId -ErrorAction Stop).ProcessName -eq 'explorer') {
            $null = Read-Host 'Press Enter to close'
        }
    } catch { }
}

try {
    $desktop = [Environment]::GetFolderPath('Desktop')     # follows OneDrive redirection
    if (-not $desktop) { $desktop = Join-Path $env:USERPROFILE 'Desktop' }
    $link = Join-Path $desktop 'subfix.lnk'

    if ($Remove) {
        if (Test-Path -LiteralPath $link) {
            Remove-Item -LiteralPath $link -Force
            Write-Host "Removed $link"
        } else {
            Write-Host "No shortcut at $link"
        }
    } else {
        if (-not (Test-Path -LiteralPath $Launcher)) { throw "missing $Launcher" }
        $shell = New-Object -ComObject WScript.Shell
        $sc = $shell.CreateShortcut($link)
        $sc.TargetPath = $Launcher
        $sc.WorkingDirectory = $Repo
        $sc.Description = 'Start the subfix web UI (subtitles for your videos)'
        $sc.WindowStyle = 1     # normal window: it shows the server log; close it to stop
        if (Test-Path -LiteralPath $IconFile) {
            $sc.IconLocation = "$IconFile,0"
        } else {
            $sc.IconLocation = "$env:SystemRoot\System32\imageres.dll,18"
        }
        $sc.Save()
        Write-Host "Created $link"
        Write-Host "  -> $Launcher"
    }
    Wait-IfDoubleClicked
    exit 0
} catch {
    Write-Host "Failed: $($_.Exception.Message)" -ForegroundColor Red
    Wait-IfDoubleClicked
    exit 1
}
