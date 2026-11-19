# Synthesize one WAV per line of an input text file with Windows SAPI (System.Speech).
# Usage: powershell -NoProfile -File tts.ps1 -InFile sentences.txt -OutDir wavs -Seed 1
param(
    [Parameter(Mandatory = $true)][string]$InFile,
    [Parameter(Mandatory = $true)][string]$OutDir,
    [int]$Seed = 1
)
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voices = $synth.GetInstalledVoices() | Where-Object { $_.Enabled } | ForEach-Object { $_.VoiceInfo.Name }
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$rng = New-Object System.Random($Seed)
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$lines = Get-Content -LiteralPath $InFile -Encoding UTF8
$i = 0
$meta = New-Object System.Collections.Generic.List[string]
foreach ($line in $lines) {
    $out = Join-Path $OutDir ("{0:D5}.wav" -f $i)
    if (-not (Test-Path $out)) {
        $voice = $voices[$rng.Next($voices.Count)]
        $synth.SelectVoice($voice)
        $synth.Rate = $rng.Next(-2, 3)
        $synth.SetOutputToWaveFile($out, $fmt)
        $synth.Speak($line)
        $synth.SetOutputToNull()
    }
    $i++
}
$synth.Dispose()
Write-Output "synthesized $i lines"
