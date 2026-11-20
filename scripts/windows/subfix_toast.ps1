<#
.SYNOPSIS
    Show a Windows toast notification for subfix (non-blocking, disappears by itself).

.DESCRIPTION
    Uses the built-in WinRT ToastNotificationManager of Windows PowerShell 5.1 (no extra
    modules, nothing written to the registry). The toast is shown under Windows PowerShell's own
    AppUserModelID, so Windows labels it "Windows PowerShell".

    * -OpenFile / -Folder add "Open subtitles" / "Open folder" buttons (protocol activation with
      file:/// URIs, so they work after this script has exited); clicking the toast itself opens
      the folder.
    * If Windows notifications are switched off in Settings, the toast is handed to Windows
      anyway (Windows then drops it); nothing else is shown.
    * Only if the toast API itself fails (e.g. very old Windows) is a message box shown instead,
      and only for -Important messages; it closes itself after 5 minutes. -NoFallback disables
      that too.

    Kill switch: with $env:SUBFIX_NOTIFY = 'off' (or 'log'), or -NoNotify, nothing is displayed;
    the notification is written to the output as "NOTIFY (not shown): <title> | <message>".
    All automated tests use it.

    Dot-source it to get Show-SubfixToast, or run it directly to show one notification:

        powershell -NoProfile -ExecutionPolicy Bypass -File subfix_toast.ps1 -Title "Hello" -Message "World"

    Show-SubfixToast returns how the notification was handled: "toast", "toast-disabled"
    (handed to Windows, but notifications are switched off), "messagebox", "off" or "none".
#>
[CmdletBinding()]
param(
    [string]$Title,
    [string]$Message,
    # Optional .srt (or any file): adds an "Open subtitles" button.
    [string]$OpenFile,
    # Optional folder: adds an "Open folder" button; clicking the toast opens it.
    [string]$Folder,
    # Notifications with the same tag replace each other (e.g. "working..." -> "ready").
    [string]$Tag,
    [switch]$Important,
    [switch]$IsError,
    [switch]$NoNotify,
    [switch]$NoFallback
)

# Windows PowerShell's own AppUserModelID: always registered (Start menu shortcut).
$script:SubfixToastAppId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'

function ConvertTo-SubfixXmlText([string]$Text) {
    if ($null -eq $Text) { return '' }
    return [System.Security.SecurityElement]::Escape($Text)
}

function Test-SubfixNotifyOff {
    return ("$env:SUBFIX_NOTIFY".Trim().ToLowerInvariant() -in @('off', 'log', '0', 'no', 'false'))
}

function Show-SubfixToast {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Title,
        [string]$Message = '',
        [string]$OpenFile,
        [string]$Folder,
        [string]$Tag,
        [switch]$IsError,
        [switch]$Important,
        [switch]$NoNotify,
        [switch]$NoFallback
    )
    if ($NoNotify -or (Test-SubfixNotifyOff)) {
        Write-Host ("NOTIFY (not shown): {0} | {1}" -f $Title, ($Message -replace "`r?`n", ' / '))
        return 'off'
    }
    try {
        $null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
        $null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]

        $launch = ''
        $buttons = ''
        if ($OpenFile -and (Test-Path -LiteralPath $OpenFile -PathType Leaf)) {
            $fileUri = ConvertTo-SubfixXmlText ([System.Uri]::new($OpenFile)).AbsoluteUri
            $buttons += "<action content=`"Open subtitles`" activationType=`"protocol`" arguments=`"$fileUri`"/>"
        }
        if ($Folder -and (Test-Path -LiteralPath $Folder -PathType Container)) {
            $dirUri = ConvertTo-SubfixXmlText ([System.Uri]::new($Folder)).AbsoluteUri
            $launch = " activationType=`"protocol`" launch=`"$dirUri`""
            $buttons += "<action content=`"Open folder`" activationType=`"protocol`" arguments=`"$dirUri`"/>"
        }
        $actions = if ($buttons) { "<actions>$buttons</actions>" } else { '' }
        $image = ''
        $icon = Join-Path $PSScriptRoot 'subfix.png'
        if (Test-Path -LiteralPath $icon) {
            $iconUri = ConvertTo-SubfixXmlText ([System.Uri]::new($icon)).AbsoluteUri
            $image = "<image placement=`"appLogoOverride`" src=`"$iconUri`"/>"
        }
        $body = ''
        foreach ($line in ($Message -split "`r?`n")) {
            if ($line) { $body += "<text>$(ConvertTo-SubfixXmlText $line)</text>" }
        }
        # duration="short": the toast slides away by itself after a few seconds.
        $xml = "<toast duration=`"short`"$launch><visual><binding template=`"ToastGeneric`">" +
               "<text>$(ConvertTo-SubfixXmlText $Title)</text>$body$image</binding></visual>$actions</toast>"

        $doc = New-Object Windows.Data.Xml.Dom.XmlDocument
        $doc.LoadXml($xml)
        $toast = New-Object Windows.UI.Notifications.ToastNotification $doc
        if ($Tag) {
            $toast.Tag = $Tag.Substring(0, [Math]::Min(64, $Tag.Length))
            $toast.Group = 'subfix'
        }
        $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($script:SubfixToastAppId)
        $notifier.Show($toast)
        $setting = "$($notifier.Setting)"
        if ($setting -and $setting -ne 'Enabled') {
            Write-Verbose "Windows notifications are switched off ($setting); the toast is not displayed"
            return 'toast-disabled'
        }
        return 'toast'
    } catch {
        Write-Verbose "WinRT toast failed: $($_.Exception.Message)"
    }
    # Last resort, only when the toast API itself failed.
    if (-not $Important -or $NoFallback) { return 'none' }
    try {
        $flags = 0x40000 + $(if ($IsError) { 0x10 } else { 0x40 })    # topmost + icon
        $text = if ($Message) { $Message } else { $Title }
        $null = (New-Object -ComObject WScript.Shell).Popup($text, 300, $Title, $flags)
        return 'messagebox'
    } catch {
        return 'none'
    }
}

if ($MyInvocation.InvocationName -ne '.' -and $Title) {
    Show-SubfixToast -Title $Title -Message $Message -OpenFile $OpenFile -Folder $Folder -Tag $Tag `
        -IsError:$IsError -Important:$Important -NoNotify:$NoNotify -NoFallback:$NoFallback
}
