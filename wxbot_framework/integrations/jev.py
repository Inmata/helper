"""TypeSafe Jev 判断接口：批量提交 noul 题目取概率，不记录密钥或题目正文。"""
from __future__ import annotations

import json
import math
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

DEFAULT_MODEL = "typesafe/jev-latest"
MAX_QUESTIONS = 64


class JevError(RuntimeError):
    pass


class JevAuthError(JevError):
    """密钥或模型配置问题：重试没有意义。"""


class JevFormatError(JevError):
    """响应不符合约定结构。"""


class JevClient:
    """独立可复用的 Jev 客户端；配置缺省时复用主模型的 base_url 与 api_key。"""

    def __init__(self, options, *, fallback=None):
        if not isinstance(options, dict):
            raise ValueError("Jev 配置必须是 JSON 对象")
        fallback = fallback if isinstance(fallback, dict) else {}
        base_url = options.get("base_url", fallback.get("base_url", ""))
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("Jev base_url 不能为空")
        self.base_url = base_url.rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Jev base_url 应为不含认证信息的 HTTP(S) 地址")
        if parsed.scheme == "http" and (parsed.hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("非本机地址的 Jev base_url 必须使用 HTTPS")
        self.model = options.get("model", DEFAULT_MODEL)
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("Jev model 不能为空")
        self._api_key = options.get("api_key", fallback.get("api_key", ""))
        if not isinstance(self._api_key, str) or not self._api_key.strip():
            raise ValueError("Jev 缺少 api_key")
        self.timeout = options.get("timeout_seconds", 30)
        if type(self.timeout) not in (int, float) or not math.isfinite(self.timeout) or not 1 <= self.timeout <= 120:
            raise ValueError("Jev timeout_seconds 应为 1–120 秒")

    @classmethod
    def from_file(cls, path):
        """从大模型配置文件读取：可选的 "jev" 段优先，缺省项取主模型配置。"""
        options = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(options, dict):
            raise ValueError("大模型配置必须是 JSON 对象")
        section = options.get("jev", {})
        return cls(section if isinstance(section, dict) else {}, fallback=options)

    def noul(self, state, questions):
        """questions 为 [{"instructions", "true", "false"}]；按顺序返回每题为真的概率。"""
        if not isinstance(state, dict):
            raise ValueError("Jev state 必须是对象")
        if not isinstance(questions, (list, tuple)) or not 1 <= len(questions) <= MAX_QUESTIONS:
            raise ValueError(f"Jev 题目数量应为 1–{MAX_QUESTIONS}")
        payload = {}
        for index, question in enumerate(questions):
            if not all(isinstance(question.get(key), str) and question[key].strip()
                       for key in ("instructions", "true", "false")):
                raise ValueError("Jev 题目需要 instructions 与 true/false 判据")
            payload[f"q{index}"] = {"type": "noul", "instructions": question["instructions"],
                                    "criteria": {"true": question["true"], "false": question["false"]}}
        answers = self._post({"model": self.model, "state": state, "questions": payload})
        result = []
        for index in range(len(questions)):
            try:
                value = answers[f"q{index}"]["noul"]
            except (KeyError, TypeError):
                raise JevFormatError("Jev 响应缺少题目结果") from None
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                raise JevFormatError("Jev 返回的概率无效")
            result.append(float(value))
        return result

    def _post(self, payload):
        request = Request(self.base_url + "/systemone",
                          data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          headers={"Authorization": "Bearer " + self._api_key,
                                   "Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(1024 * 1024 + 1)
        except HTTPError as error:
            # 不输出响应正文或请求头，避免服务器错误回显密钥。
            if error.code in (401, 403):
                raise JevAuthError(f"Jev 接口 HTTP {error.code}；请核对密钥与模型配置") from None
            raise JevError(f"Jev 接口 HTTP {error.code}") from None
        except (URLError, TimeoutError, OSError):
            raise JevError("Jev 连接失败或超时") from None
        if len(raw) > 1024 * 1024:
            raise JevError("Jev 响应过大")
        try:
            answers = json.loads(raw)["answers"]
        except (ValueError, KeyError, TypeError):
            raise JevFormatError("Jev 响应不是约定的 JSON") from None
        if not isinstance(answers, dict):
            raise JevFormatError("Jev 响应缺少 answers")
        return answers
