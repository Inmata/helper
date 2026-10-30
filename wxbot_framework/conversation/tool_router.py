"""Jev 的看图 / 联网搜索判断：只做决定，不调用 Magpie。

两项判断是两次独立的 ``noul`` 调用，一项失败不会影响另一项；真正的识图和搜索由调用方
执行（见 ``wxbot_framework.integrations.magpie_tools``）。``ToolTurn`` 负责一轮提问内的
预算：每项工具最多判断一次、最多记录一次实际结果；识图结果会作为搜索判断的输入。

默认阈值来自 ``tools/evaluate_tool_router.py`` 在真实艾特提问上的离线评测（样本小，仍需真机复核）：
搜索 0.7——dev 误搜 1/116、召回 7/10，未参与调参的 holdout 误搜 0/50、召回 3/4；
看图 0.6——每条样本都假定附了图，真实误判 7/176、召回 4/4，合成样本误判 1/14、召回 10/10。
看图只在已匹配到图片时才判断，实际误判面比评测假定更小；不复用主动发言的 0.25。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

VISION = "vision"
SEARCH = "search"

ACCEPTED = "accepted"
REJECTED = "rejected"
UNAVAILABLE = "unavailable"
NOT_APPLICABLE = "not_applicable"
BUDGET_EXHAUSTED = "budget_exhausted"

DEFAULT_VISION_THRESHOLD = 0.6
DEFAULT_SEARCH_THRESHOLD = 0.7
MAX_QUESTION_CHARS = 2000
MAX_SUMMARY_CHARS = 600

VISION_QUESTION = {
    "instructions": "判断这条群聊提问是否需要先看随消息附带的图片才能可靠回答。",
    "true": "提问针对图片本身（截图、照片、图表、界面、图中的文字），不看图就无法可靠回答。",
    "false": "看图对回答没有帮助，或提问与图片无关；只是提到图片、发表情或闲聊也算 false。",
}
# 首版问法把顺带提到股票、新闻的闲聊也判成要搜（dev 阈值 0.5 误搜 13.9%），
# 这一版明确排除闲聊调侃和群内人事。
SEARCH_QUESTION = {
    "instructions": "判断这条群聊提问是否需要联网搜索才能可靠回答。群友常在闲聊、吐槽、玩梗或发表看法时顺带提到股票、"
                    "公司、新闻和人物，这类陈述和调侃不需要搜索；群友的绰号、群里的人和事网上查不到，也不需要搜索。"
                    "只有对方在问一个依赖最新数据或可查证具体事实的问题，或明确要求联网、搜索时才需要。"
                    "已有看图结果时，只用它判断是否还需要补充最新信息。",
    "true": "对方在问一个具体事实，答案依赖近期信息或实时数据（行情、价格、版本、新闻、赛果、天气）或特定网页内容，"
            "不联网很可能答错或过时；问网上流行梗、网络人物或作品的含义来历（不是群友）；或明确要求联网、搜索、上网查。",
    "false": "闲聊、吐槽、玩梗、陈述观点或调侃（即使提到股票、公司、新闻）；问群里的人和事、群友绰号；"
             "常识、观点、建议、创作、算术；不联网也能答好。",
}


@dataclass(frozen=True)
class ToolDecision:
    """一次工具判断。

    ``status`` 取值：``accepted`` / ``rejected``（Jev 已给出概率）、``unavailable``
    （未配置、调用失败或响应无效，一律不调用工具）、``not_applicable``（前提不成立，
    例如没有图片）、``budget_exhausted``（本轮该项工具已经判断过）。
    """

    tool: str
    use: bool
    status: str
    probability: float | None = None
    threshold: float | None = None
    error: str | None = None


def _threshold(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"{name} 应为 0–1 的数值")
    return float(value)


def _question(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("判断前需要有提问正文")
    text = text.strip()
    if len(text) > MAX_QUESTION_CHARS:
        raise ValueError(f"提问正文不能超过 {MAX_QUESTION_CHARS} 字")
    return text


def _summary(text):
    if not isinstance(text, str):
        raise ValueError("工具结果必须是字符串")
    return text.strip()[:MAX_SUMMARY_CHARS]


def read_options(options):
    """读取 ``{vision_threshold, search_threshold}``，缺省用评测得出的默认阈值。"""
    if options is None:
        options = {}
    if not isinstance(options, dict):
        raise ValueError("Jev 工具判断配置必须是 JSON 对象")
    return {
        "vision_threshold": _threshold(options.get("vision_threshold", DEFAULT_VISION_THRESHOLD),
                                       "vision_threshold"),
        "search_threshold": _threshold(options.get("search_threshold", DEFAULT_SEARCH_THRESHOLD),
                                       "search_threshold"),
    }


class JevToolRouter:
    """独立判断“要不要看图”“要不要搜索”；``jev`` 为 None 时统一返回不可用。"""

    def __init__(self, jev, *, vision_threshold=DEFAULT_VISION_THRESHOLD,
                 search_threshold=DEFAULT_SEARCH_THRESHOLD):
        self.jev = jev
        self.vision_threshold = _threshold(vision_threshold, "vision_threshold")
        self.search_threshold = _threshold(search_threshold, "search_threshold")

    def decide_vision(self, *, question, has_image, timeout=None):
        if type(has_image) is not bool:
            raise ValueError("has_image 必须是布尔值")
        question = _question(question)
        if not has_image:
            return ToolDecision(VISION, False, NOT_APPLICABLE)
        return self._decide(VISION, {"提问": question}, VISION_QUESTION, self.vision_threshold, timeout)

    def decide_search(self, *, question, vision_text=None, timeout=None):
        question = _question(question)
        state = {"提问": question}
        if vision_text is not None:
            state["看图结果"] = _summary(vision_text)
        return self._decide(SEARCH, state, SEARCH_QUESTION, self.search_threshold, timeout)

    def _decide(self, tool, state, question, threshold, timeout=None):
        """``timeout`` 为本轮剩余时间；只在给出时传给 Jev，兼容不认识该参数的客户端。"""
        if self.jev is None:
            return ToolDecision(tool, False, UNAVAILABLE, threshold=threshold, error="not-configured")
        try:
            values = self.jev.noul(state, [dict(question)], **({} if timeout is None else {"timeout": timeout}))
        except Exception as error:  # Jev 超时、鉴权、格式错误一律不调用工具，不重试。
            return ToolDecision(tool, False, UNAVAILABLE, threshold=threshold, error=type(error).__name__)
        probability = _probability(values)
        if probability is None:
            return ToolDecision(tool, False, UNAVAILABLE, threshold=threshold, error="invalid-probability")
        use = probability >= threshold
        return ToolDecision(tool, use, ACCEPTED if use else REJECTED,
                            probability=round(probability, 3), threshold=threshold)


def _probability(values):
    if not isinstance(values, (list, tuple)) or len(values) != 1:
        return None
    value = values[0]
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        return None
    return float(value)


class ToolTurn:
    """一轮提问内的工具判断与使用预算。

    调用方先 ``consider_vision``，得到 ``use`` 后自己调用 Magpie 识图，再用
    ``record_vision`` 记下结果；随后 ``consider_search`` 会把看图结果带给 Jev。
    每项工具的判断和记录各至多一次，超过时返回 ``budget_exhausted`` 或抛
    ``ValueError``，避免同一轮重复请求。
    """

    def __init__(self, router, *, question, has_image=False):
        self.router = router
        self.question = _question(question)
        if type(has_image) is not bool:
            raise ValueError("has_image 必须是布尔值")
        self.has_image = has_image
        self._vision_decision = None
        self._search_decision = None
        self._vision_summary = None
        self._search_summary = None

    @property
    def vision_used(self):
        return self._vision_summary is not None

    @property
    def search_used(self):
        return self._search_summary is not None

    def consider_vision(self, *, timeout=None):
        if self._vision_decision is not None:
            return ToolDecision(VISION, False, BUDGET_EXHAUSTED, threshold=self.router.vision_threshold)
        self._vision_decision = self.router.decide_vision(question=self.question, has_image=self.has_image,
                                                          timeout=timeout)
        return self._vision_decision

    def record_vision(self, text):
        """记录调用方实际执行的识图结果；只保留有界摘要供搜索判断使用。"""
        if self._vision_summary is not None:
            raise ValueError("本轮已记录过识图结果")
        self._vision_summary = _summary(text)

    def consider_search(self, *, timeout=None):
        if self._search_decision is not None:
            return ToolDecision(SEARCH, False, BUDGET_EXHAUSTED, threshold=self.router.search_threshold)
        self._search_decision = self.router.decide_search(question=self.question,
                                                          vision_text=self._vision_summary, timeout=timeout)
        return self._search_decision

    def record_search(self, text):
        if self._search_summary is not None:
            raise ValueError("本轮已记录过搜索结果")
        self._search_summary = _summary(text)
