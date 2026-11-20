<#
.SYNOPSIS
    Removes the "Generate subtitles with subfix" right-click menu entry.

.DESCRIPTION
    Looks at every HKEY_CURRENT_USER\Software\Classes\SystemFileAssociations\<ext>\shell\subfix
    key and deletes it if it was written by install_context_menu.ps1 (it has the
    SubfixCreatedKeys value or its command runs subfix_context.ps1). Parent keys are removed only
    if the installer created them and they are now empty. Other menu entries are never touched.
#>
[CmdletBinding()]
param([switch]$NoPause)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$Verb = 'subfix'
$Base = 'Software\Classes\SystemFileAssociations'

function Wait-IfDoubleClicked {
    if ($NoPause) { return }
    try {
        $parentId = (Get-CimInstance Win32_Process -Filter "ProcessId=$PID").ParentProcessId
        if ((Get-Process -Id $parentId -ErrorAction Stop).ProcessName -eq 'explorer') {
            $null = Read-Host 'Press Enter to close'
        }
    } catch { }
}

function Remove-KeyIfEmpty([string]$SubPath) {
    $hkcu = [Microsoft.Win32.Registry]::CurrentUser
    $k = $hkcu.OpenSubKey($SubPath)
    if (-not $k) { return }
    $empty = ($k.SubKeyCount -eq 0 -and $k.ValueCount -eq 0)
    $k.Close()
    if ($empty) { $hkcu.DeleteSubKey($SubPath, $false) }
}

function Update-ShellAssociations {
    try {
        if (-not ('SubfixShell.Notify' -as [type])) {
            Add-Type -Namespace SubfixShell -Name Notify -MemberDefinition `
                '[DllImport("shell32.dll")] public static extern void SHChangeNotify(int eventId, int flags, System.IntPtr item1, System.IntPtr item2);'
        }
        [SubfixShell.Notify]::SHChangeNotify(0x08000000, 0, [IntPtr]::Zero, [IntPtr]::Zero)
    } catch { }
}

try {
    $hkcu = [Microsoft.Win32.Registry]::CurrentUser
    $baseKey = $hkcu.OpenSubKey($Base)
    if (-not $baseKey) {
        Write-Host 'Nothing to remove.'
        Wait-IfDoubleClicked
        exit 0
    }
    $exts = $baseKey.GetSubKeyNames()
    $baseKey.Close()

    $removed = 0
    $removeBase = $false
    foreach ($ext in $exts) {
        $verbPath = "$Base\$ext\shell\$Verb"
        $k = $hkcu.OpenSubKey($verbPath)
        if (-not $k) { continue }
        $marker = $k.GetValue('SubfixCreatedKeys', $null)
        $command = ''
        $c = $k.OpenSubKey('command')
        if ($c) { $command = "$($c.GetValue('', ''))"; $c.Close() }
        $k.Close()
        if ($null -eq $marker -and $command -notmatch 'subfix_context\.ps1') {
            Write-Host "  skipped (not created by subfix): HKEY_CURRENT_USER\$verbPath"
            continue
        }
        $hkcu.DeleteSubKeyTree($verbPath)
        $created = @("$marker".Split(',') | Where-Object { $_ })
        if ($created -contains 'shell') { Remove-KeyIfEmpty "$Base\$ext\shell" }
        if ($created -contains 'ext') { Remove-KeyIfEmpty "$Base\$ext" }
        if ($created -contains 'base') { $removeBase = $true }
        Write-Host "  removed HKEY_CURRENT_USER\$verbPath"
        $removed++
    }
    if ($removeBase) { Remove-KeyIfEmpty $Base }
    Update-ShellAssociations
    Write-Host ''
    Write-Host "Done: removed the subfix menu entry from $removed file types."
    Wait-IfDoubleClicked
    exit 0
} catch {
    Write-Host "Uninstall failed: $($_.Exception.Message)" -ForegroundColor Red
    Wait-IfDoubleClicked
    exit 1
}
