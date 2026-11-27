"""在独立的 Python 3.12 进程中运行本地 GPT-SoVITS 音色。"""
from io import BytesIO
from pathlib import Path
from queue import Empty, Queue
import re
import struct
import subprocess
import sys
from threading import Event, Lock, Thread
from time import monotonic

_LENGTH = struct.Struct("<I")
_READY = b"READY\n"
_WARMUP_TIMEOUT = 180
_SYNTHESIS_TIMEOUT = 50
_RUNTIME = Path(__file__).resolve().parents[2] / ".runtime/voice-xuanshen"
_SPEAKERS = {"阿梓": "Azusa", "炫神": "XuanShen", "星瞳": "XingTong", "七海": "Nana7mi",
             "东雪莲": "Azuma", "永雏塔菲": "Taffy", "电棍": "otto", "孙笑川": "sun",
             "张顺飞": "ZhangShunFei"}
_PYTHON = _RUNTIME / "venv/Scripts/python.exe"


class LocalVoice:
    def __init__(self, voice):
        self._lock = Lock()
        self._responses = Queue()
        self._ready = Event()
        self._initialized = False
        self._started = monotonic()
        self._response_deadline = None
        self._process = subprocess.Popen(
            [str(_PYTHON), "-u", "-X", "utf8", __file__, voice],
            cwd=_RUNTIME, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        Thread(target=self._read_responses, name="LocalVoice", daemon=True).start()

    @staticmethod
    def available_voices():
        if not _PYTHON.is_file():
            return ()
        return tuple(name for name, folder in _SPEAKERS.items() if all(
            (_RUNTIME / "speakers" / folder / file).is_file()
            for file in ("config.json", "ref.wav", "gpt.ckpt", "sovits.pth")))

    def _read_responses(self):
        try:
            with self._process.stdout as wire:
                self._initialized = wire.read(len(_READY)) == _READY
                self._ready.set()
                if not self._initialized:
                    return
                while header := wire.read(_LENGTH.size):
                    size, = _LENGTH.unpack(header)
                    self._responses.put(wire.read(size))
        finally:
            self._ready.set()
            self._responses.put(None)

    @property
    def ready(self):
        return self._ready.is_set() and self._initialized

    @property
    def warming_up(self):
        return not self._ready.is_set() and monotonic() - self._started < _WARMUP_TIMEOUT

    def wait_ready(self):
        """等待启动预热完成；这段等待不占单句合成的 50 秒预算。"""
        from .reply_budget import remaining, ReplyExpired
        warmup_left = max(0, self._started + _WARMUP_TIMEOUT - monotonic())
        wait = remaining(warmup_left)
        if not self._ready.wait(timeout=wait):
            if wait < warmup_left:
                raise ReplyExpired("本轮等候预热的时间已用完")
            self.close()
            raise TimeoutError("本地音色预热超时")
        if not self._initialized:
            raise OSError("本地音色在预热完成前退出")

    def synthesize(self, voice, text):
        with self._lock:
            self.wait_ready()
            if self._response_deadline is not None:
                # 上轮已改发文字。管道按顺序返回，先取走上句的音频，才能交给它新句子。
                self._receive()
            self._process.stdin.write(f"{voice}\t{text}\n".encode("utf-8"))
            self._process.stdin.flush()
            self._response_deadline = monotonic() + _SYNTHESIS_TIMEOUT
            return self._receive()

    def _receive(self):
        from .reply_budget import remaining, ReplyExpired
        synthesis_left = max(0, self._response_deadline - monotonic())
        wait = remaining(synthesis_left)
        try:
            audio = self._responses.get(timeout=wait)
        except Empty:
            if wait < synthesis_left:
                raise ReplyExpired("本轮等候语音的时间已用完") from None
            self.close()
            raise TimeoutError("本地语音合成超时") from None
        self._response_deadline = None
        if audio is None:
            raise OSError("本地语音合成没有返回音频")
        return audio or None

    def close(self):
        if self._process.poll() is None:
            self._process.terminate()
        self._process.wait()
        self._process.stdin.close()


_KANA = re.compile(r"[ぁ-ゖァ-ヺーｦ-ﾟ]")
_SCRIPT = re.compile(r"(?P<english>[A-Za-z]+)|[㐀-鿿々ぁ-ヺーｦ-ﾟ]+")
_EN_PUNCTUATION = str.maketrans("，。！？；：、’", ",.!?;:,'")


def _xingtong_passages(text):
    """按标点判断汉字读音。带假名的短句里，汉字随假名按日语读。"""
    passages = []
    language = None
    offset = start = 0
    for clause in re.split(r"(?<=[，,。.!！?？；;：:、\n「」『』])", text):
        cjk = "ja" if _KANA.search(clause) else "zh"
        for index, match in enumerate(_SCRIPT.finditer(clause)):
            current = "en" if match.lastgroup == "english" else cjk
            if language and current != language:
                boundary = offset + (match.start() if index else 0)
                passages.append((language, text[start:boundary]))
                start = boundary
            language = current
        offset += len(clause)
    if text:
        passages.append((language or "zh", text[start:]))
    return passages


def _voice_samples(model, voice, text):
    import numpy as np
    import torch

    passages = _xingtong_passages(text) if voice == "星瞳" else [("zh", text)]
    max_seconds = 2 + len(text) / 4
    rate = model.sampling_rate
    for attempt in range(2):
        chunks = []
        for language, fragment in passages:
            fragment = fragment.strip()
            seeds = (7, 11)
            if language == "en":
                fragment = fragment.translate(_EN_PUNCTUATION)
                seeds = (11, 42)
            torch.manual_seed(seeds[attempt])
            # 星瞳起音前可有约 9.7 秒静音。解码器每秒 25 个 token，上限配置按 50 换算。
            decode_seconds = 2 + len(fragment) / 4 + 10
            model.t2s_max_sec = decode_seconds / 2
            samples = model.predict(fragment, text_language=language, top_k=5)
            if len(samples) > decode_seconds * rate:
                break  # 达到解码上限的片段可能没念完，整句重试。
            chunks.append(_trim_leading_silence(samples, rate))
        else:
            if chunks and sum(map(len, chunks)) <= max_seconds * rate:
                return chunks[0] if len(chunks) == 1 else np.concatenate(chunks)


def _trim_leading_silence(samples, rate):
    import numpy as np

    frame = rate // 50
    blocks = samples[:len(samples) // frame * frame].reshape(-1, frame)
    rms = np.sqrt(np.square(blocks).mean(axis=1))
    onset = np.flatnonzero(rms >= max(0.001, rms.max(initial=0) * 0.02))
    # 开头保留 80 毫秒，避免弱起音紧贴切口。
    return samples[max(0, int(onset[0]) - 4) * frame:] if len(onset) else samples


def _worker():
    import os
    import time

    root = Path.cwd()
    sys.path[:0] = [str(root / "deps-zh"), str(root / "parrots-src")]
    os.environ["NLTK_DATA"] = str(root / "nltk-data")
    os.environ["NUMBA_CACHE_DIR"] = str(root / "numba-cache")
    wire = sys.stdout.buffer
    sys.stdout = sys.stderr
    started = time.perf_counter()
    import soundfile
    from scipy.signal import resample_poly
    import torch
    from parrots import TextToSpeech, set_log_level

    set_log_level("WARNING")

    def load_voice(voice):
        return TextToSpeech(
            bert_model_path=str(root / "parrots-chinese-roberta-wwm-ext-large"),
            hubert_model_path=str(root / "parrots-chinese-hubert-base"),
            speaker_model_path=str(root / "speakers"), speaker_name=_SPEAKERS[voice],
            device="cuda", half=True, ensure_nltk=False,
        )
    current = sys.argv[1]
    model = load_voice(current)
    with torch.inference_mode():
        _voice_samples(model, current, "大家好。")
    print(f"本地音色已预热：{current}，{time.perf_counter() - started:.2f} 秒", file=sys.stderr)
    wire.write(_READY)
    wire.flush()
    for line in sys.stdin.buffer:
        voice, text = line.decode("utf-8").split("\t", 1)
        if voice != current:
            model = None
            model = load_voice(voice)
            current = voice
        with torch.inference_mode():
            samples = _voice_samples(model, voice, text.strip())
        if samples is None:
            wire.write(_LENGTH.pack(0))
            wire.flush()
            continue
        wav = BytesIO()
        samples = resample_poly(samples, 16000, model.sampling_rate)
        soundfile.write(wav, samples, 16000, format="WAV", subtype="PCM_16")
        data = wav.getbuffer()
        wire.write(_LENGTH.pack(len(data)))
        wire.write(data)
        wire.flush()


if __name__ == "__main__":
    _worker()
