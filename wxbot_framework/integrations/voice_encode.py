"""将本地 WAV 或 SILK 准备为原生发送所需的音频和时长。"""
from io import BytesIO
from pathlib import Path
import struct
import wave

TENCENT_HEADER = b"\x02#!SILK_V3"
STANDARD_HEADER = b"#!SILK_V3"
MAX_BYTES = 20 * 1024 * 1024
MAX_DURATION_MS = 60_000


def _silk(data):
    tencent = data.startswith(TENCENT_HEADER)
    header = TENCENT_HEADER if tencent else STANDARD_HEADER
    end = len(data)
    if not tencent:
        if not data.endswith(b"\xff\xff"):
            raise ValueError("标准 SILK 缺少结束标记")
        end -= 2
    position, packets = len(header), 0
    while position < end:
        if end - position < 2:
            raise ValueError("SILK 包长度被截断")
        size, = struct.unpack_from("<h", data, position)
        position += 2
        if size <= 0 or position + size > end:
            raise ValueError("SILK 音频包不完整")
        position += size
        packets += 1
    duration = packets * 20
    if not 0 < duration <= MAX_DURATION_MS:
        raise ValueError("语音必须非空，且不超过 60 秒")
    return TENCENT_HEADER + data[len(header):end], duration


def prepare_voice(path):
    if isinstance(path, bytes):
        data = path
    else:
        with Path(path).open("rb") as source:
            data = source.read(MAX_BYTES + 1)
    if not 0 < len(data) <= MAX_BYTES:
        raise ValueError("语音文件必须非空，且不超过 20 MiB")
    if data.startswith((TENCENT_HEADER, STANDARD_HEADER)):
        return _silk(data)
    try:
        with wave.open(BytesIO(data), "rb") as source:
            rate, count = source.getframerate(), source.getnframes()
            if source.getnchannels() != 1 or source.getsampwidth() != 2 or rate not in (8000, 12000, 16000, 24000):
                raise ValueError("WAV 仅支持单声道 16 位 PCM，采样率为 8、12、16 或 24 kHz")
            if not 0 < count <= rate * 60:
                raise ValueError("语音必须非空，且不超过 60 秒")
            pcm = source.readframes(count)
    except (wave.Error, EOFError) as error:
        raise ValueError("语音文件必须是 PCM WAV 或 SILK v3") from error
    if len(pcm) != count * 2:
        raise ValueError("WAV 音频数据不完整")
    try:
        import pysilk
    except ImportError as error:
        raise RuntimeError("发送 WAV 需先安装 requirements-voice.txt 中的 silk-python") from error
    frame_bytes = rate // 50 * 2
    pcm += b"\0" * (-len(pcm) % frame_bytes)
    output = BytesIO()
    pysilk.encode(BytesIO(pcm), output, rate, 24000, tencent=True)
    encoded = output.getvalue()
    if not encoded.startswith(TENCENT_HEADER):
        raise ValueError("编码结果缺少微信 SILK 文件头")
    return _silk(encoded)
