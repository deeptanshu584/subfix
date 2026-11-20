<#
.SYNOPSIS
  Installs (or removes) the subfix plugin for mpv. Writes only inside mpv's config folder.

.DESCRIPTION
  Copies plugins\mpv\subfix.lua and subfix.conf into <config>\scripts and <config>\script-opts,
  and fills "executable=" in subfix.conf with this checkout's .venv\Scripts\subfix.exe (or the
  first subfix.exe found on PATH). An existing subfix.conf is kept (your settings win) unless
  its executable= line is empty, in which case only that line is filled in.

  Config folder, first match wins: -ConfigDir, a portable_config folder next to mpv.exe
  (mpv found on PATH), %APPDATA%\mpv.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\windows\install_mpv_plugin.ps1
  powershell -ExecutionPolicy Bypass -File scripts\windows\install_mpv_plugin.ps1 -Remove
#>
param(
    [string]$ConfigDir = "",
    [switch]$Remove,
    [switch]$DryRun
)
$ErrorActionPreference = "Stop"

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$srcLua = Join-Path $repo "plugins\mpv\subfix.lua"
$srcConf = Join-Path $repo "plugins\mpv\subfix.conf"

function Find-ConfigDir {
    if ($ConfigDir) { return $ConfigDir }
    $mpv = Get-Command mpv.exe -ErrorAction SilentlyContinue
    if ($mpv) {
        $portable = Join-Path (Split-Path $mpv.Source) "portable_config"
        if (Test-Path $portable) { return $portable }
    }
    return (Join-Path $env:APPDATA "mpv")
}

function Find-Subfix {
    $venv = Join-Path $repo ".venv\Scripts\subfix.exe"
    if (Test-Path $venv) { return $venv }
    $onPath = Get-Command subfix.exe -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    return ""
}

$cfg = Find-ConfigDir
$lua = Join-Path $cfg "scripts\subfix.lua"
$conf = Join-Path $cfg "script-opts\subfix.conf"

if ($Remove) {
    foreach ($f in @($lua, $conf)) {
        if (Test-Path $f) {
            if ($DryRun) { Write-Host "would remove $f" } else { Remove-Item $f; Write-Host "removed $f" }
        }
    }
    return
}

$exe = Find-Subfix
if (-not $exe) {
    Write-Warning "subfix.exe not found (no .venv in the repo, nothing on PATH). Install subfix first, or edit executable= in $conf afterwards."
}

if ($DryRun) {
    Write-Host "would copy $srcLua -> $lua"
    Write-Host "would write $conf with executable=$exe"
    return
}

New-Item -ItemType Directory -Force (Split-Path $lua), (Split-Path $conf) | Out-Null
Copy-Item $srcLua $lua -Force

if (Test-Path $conf) {
    $text = Get-Content $conf -Raw
    if ($exe -and $text -match '(?m)^executable=\s*$') {
        $text = $text -replace '(?m)^executable=\s*$', "executable=$exe"
        [IO.File]::WriteAllText($conf, $text, (New-Object Text.UTF8Encoding($false)))
        Write-Host "kept your $conf and filled in executable="
    } else {
        Write-Host "kept your existing $conf"
    }
} else {
    $text = Get-Content $srcConf -Raw
    if ($exe) { $text = $text -replace '(?m)^executable=\s*$', "executable=$exe" }
    [IO.File]::WriteAllText($conf, $text, (New-Object Text.UTF8Encoding($false)))
    Write-Host "wrote $conf"
}
Write-Host "Installed subfix plugin into $cfg  (press Ctrl+Shift+S in mpv)"
