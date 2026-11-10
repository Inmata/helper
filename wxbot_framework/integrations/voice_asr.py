"""用本机 SenseVoice 把微信 SILK 语音转成群聊文字。"""
from array import array
from io import BytesIO
import xml.etree.ElementTree as ET

SILK_HEADERS = (b"\x02#!SILK_V3", b"#!SILK_V3")
SAMPLE_RATE = 16000
SOUND_EVENTS = {
    "<|BGM|>": "音乐声",
    "<|Applause|>": "掌声",
    "<|Laughter|>": "笑声",
    "<|Cry|>": "哭声",
    "<|Sneeze|>": "喷嚏声",
    "<|Breath|>": "呼吸声",
    "<|Cough|>": "咳嗽声",
}


class VoiceTranscriber:
    def __init__(self, directory):
        import pysilk
        import sherpa_onnx

        self._decode = pysilk.decode
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            str(directory / "model.int8.onnx"), str(directory / "tokens.txt"),
            num_threads=2, language="auto", use_itn=True)

    def transcribe(self, audio, metadata):
        if not audio.startswith(SILK_HEADERS):
            return ""
        root = ET.fromstring(metadata)
        voice = root if root.tag == "voicemsg" else root.find("voicemsg")
        if voice is None or voice.get("endflag", "1") != "1" or voice.get("cancelflag", "0") != "0":
            return ""
        if voice.get("length", str(len(audio))) != str(len(audio)):
            return ""
        pcm = BytesIO()
        self._decode(BytesIO(audio), pcm, SAMPLE_RATE)
        samples = array("h", pcm.getvalue())
        if not samples:
            return ""
        stream = self._recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, [sample / 32768.0 for sample in samples])
        self._recognizer.decode_stream(stream)
        result = stream.result
        text = result.text.strip()
        if any(character.isalnum() for character in text):
            return text
        event = SOUND_EVENTS.get(result.event)
        return f"（听起来像{event}）" if event else ""
