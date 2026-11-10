"""用 Windows 已安装的中文音色生成内存 WAV，不播放、不联网。"""
from io import BytesIO
import subprocess
import wave

_SYNTHESIZE = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$reader = [IO.StreamReader]::new([Console]::OpenStandardInput(), [Text.Encoding]::UTF8)
$text = $reader.ReadToEnd()
$speech = [System.Speech.Synthesis.SpeechSynthesizer]::new()
$pcm = [IO.MemoryStream]::new()
try {
    $voice = $speech.GetInstalledVoices() | Where-Object {
        $_.Enabled -and $_.VoiceInfo.Culture.TwoLetterISOLanguageName -eq 'zh'
    } | Select-Object -First 1
    if (!$voice) { throw 'No installed Chinese voice' }
    $speech.SelectVoice($voice.VoiceInfo.Name)
    $format = [System.Speech.AudioFormat.SpeechAudioFormatInfo]::new(16000, 'Sixteen', 'Mono')
    $speech.SetOutputToAudioStream($pcm, $format)
    $speech.Speak($text)
    $speech.SetOutputToNull()
    $bytes = $pcm.ToArray()
    [Console]::OpenStandardOutput().Write($bytes, 0, $bytes.Length)
} finally {
    $speech.Dispose()
    $pcm.Dispose()
}
"""


def synthesize_voice(text):
    result = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _SYNTHESIZE],
        input=text.encode("utf-8"), capture_output=True, timeout=8,
        creationflags=subprocess.CREATE_NO_WINDOW, check=True)
    if not result.stdout:
        raise ValueError("本机语音合成没有音频")
    output = BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(result.stdout)
    return output.getvalue()
