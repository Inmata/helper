"""发言：艾特回复和定时发言发出文字前的唯一出口，集中处理分档、拆分、条间隔和插话丢弃。

三档：

* 明确要求详细（``DETAIL``）：按字节预算分段，段数不限，每段都带艾特，不加条间隔、不因插话丢弃；
* 普通问答（``QA``）：单条发送，不拆；
* 闲聊/接梗（``CHAT``）：总长 ≤40 字且能在句末标点（只有一句时退而用逗号）处切成 2—3 条短句时才拆，最多 3 条；否则单条。

不做字数硬截断：非详细档的单条正文若超出字节预算，仍用 ``split_text`` 按边界分段发完，
这是兜底而不是常态。每一条都经调用方给的发送函数发出，调用方在发送函数里套 ``delivery_guard``，
所以守卫对每条单独检查；守卫抛出的异常（如快照过期）原样向上传播，已发出的段不重发。

条间隔：闲聊拆分的后续条，在“上一条发送函数返回之后、下一条发送函数被调用之前”等待 1—3 秒
（按下一条字数取目标值，用 ``time.monotonic`` 扣掉已过去的时间，只补足剩余部分）。
底层发送层（``TextSendDelay``，默认每 10 字 0.1—0.3 秒）自己的打字节奏另外叠加，这里不估计也不扣除。

插话检测有两处：后续每条在条间隔等待之后、调用发送函数之前检查一次；再通过 ``InterjectionGate.wrap``
把检查并入 ``delivery_guard``，在拿到守卫之后、原生发送之前再查一次，使判定贴近真实发送时刻。
被插话丢弃的段落计入 ``dropped``，不算 ``failed``。已知的残余窗口（可接受）：
底层发送层在守卫之前的打字节奏等待期间出现的新消息，要到下一条才能发现；
只有文字消息计为插话，非文本消息和控制指令不计。

本模块在 ``conversation`` 层，不依赖 ``plugin_engine`` 和 ``memory``：发送函数和群成员目录都按鸭子类型传入。
"""
from __future__ import annotations

import logging
import random
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from .summary import split_text

logger = logging.getLogger(__name__)

DETAIL, QA, CHAT = "detail", "qa", "chat"

# 普通问答的信号：命中任一个就按问答处理（单条、不拆）。宁可把闲聊当成问答，也不把问题拆开发。
QUESTION_SIGNALS = ("？", "?", "吗", "怎么", "怎样", "如何", "为什么", "为何", "什么", "啥", "咋",
                    "多少", "哪", "几", "谁", "是否", "能否", "有没有", "是不是", "能不能", "可不可以", "行不行",
                    # 没有疑问词的请求类说法，同样是等着被回答的提问。
                    "解释", "介绍", "讲讲", "说说", "讲一下", "说一下", "告诉我", "推荐", "帮我", "区别", "对比",
                    "比较", "科普", "分析", "总结", "教我", "翻译", "查一下", "给我")

CHAT_SPLIT_MAX_CHARS = 40  # 闲聊拆分的总长上限
CHAT_SPLIT_MAX_PARTS = 3
CHAT_SENTENCE_MAX_CHARS = 20  # 拆分后每句不超过这个长度
SENTENCE_END = "。！？!?…~～"
GAP_MIN_SECONDS, GAP_MAX_SECONDS = 1.0, 3.0
PROBE_ROWS = 30


def is_question(query):
    return any(signal in query for signal in QUESTION_SIGNALS)


def classify_tier(query, *, detail=False):
    """按提问判档：明确要求详细 → DETAIL；带疑问信号 → QA；其余 → CHAT。"""
    if detail:
        return DETAIL
    return QA if is_question(query or "") else CHAT


def split_sentences(text):
    """在句末标点（连续标点算一处）和换行处切句，标点留在句尾；只含标点的碎片并回前一句。"""
    pieces, current, index = [], [], 0
    while index < len(text):
        char = text[index]
        index += 1
        if char == "\n":
            if "".join(current).strip():
                pieces.append("".join(current).strip())
            current = []
            continue
        current.append(char)
        if char in SENTENCE_END:
            while index < len(text) and text[index] in SENTENCE_END:
                current.append(text[index])
                index += 1
            pieces.append("".join(current).strip())
            current = []
    if "".join(current).strip():
        pieces.append("".join(current).strip())
    merged = []
    for piece in pieces:
        if merged and not any(char.isalnum() for char in piece):
            merged[-1] += piece
        elif piece:
            merged.append(piece)
    return merged


def chat_parts(text):
    """闲聊拆分：总长 ≤40 且恰好 2—3 句、每句 ≤20 字时返回各句，否则返回 None。

    分句后去掉每句结尾的句号（群友不会在每条短消息后加句号），其他标点和字原样保留。
    模型常用逗号把两三句连成一句：只有一句时退而在逗号处切，切出的每段同样要满足上述条件。
    """
    text = text.strip()
    if not text or len(text) > CHAT_SPLIT_MAX_CHARS:
        return None
    sentences = split_sentences(text)
    if len(sentences) == 1:
        sentences = [piece for piece in re.split(r"(?<=[，,])", text) if piece.strip()]
    if not 2 <= len(sentences) <= CHAT_SPLIT_MAX_PARTS:
        return None
    parts = [sentence.rstrip("。，,").strip() or sentence for sentence in sentences]
    if any(len(part) > CHAT_SENTENCE_MAX_CHARS for part in parts):
        return None
    return parts


def gap_seconds(next_text, rng=None):
    """条间隔目标值：按下一条的字数取 1—3 秒。发送层自己的打字节奏另外叠加，不在这里扣除。"""
    rng = rng or random
    return min(GAP_MAX_SECONDS, max(GAP_MIN_SECONDS, 0.8 + 0.1 * len(next_text) + rng.uniform(0, 0.4)))


@dataclass(frozen=True)
class Plan:
    tier: str
    parts: tuple
    at_all: bool  # True：每条都带艾特；False：只有第一条带艾特
    paced: bool  # True：条间加间隔，并在发送期间检查插话


def plan_utterance(text, tier, *, byte_budget):
    """把一条回复规划成待发送的各条。``byte_budget`` 为第一条正文可用的 UTF-8 字节数（已扣艾特前缀）。"""
    text = text.strip()
    if tier == DETAIL:
        return Plan(tier, tuple(split_text(text, byte_limit=byte_budget)), True, False)
    if tier == CHAT:
        parts = chat_parts(text)
        if parts and all(len(part.encode("utf-8")) <= byte_budget for part in parts):
            return Plan(tier, tuple(parts), False, True)
    if len(text.encode("utf-8")) <= byte_budget:
        return Plan(tier, (text,), True, False)
    # 兜底：非详细档的单条超过字节预算时仍完整发出，不截断。
    return Plan(tier, tuple(split_text(text, byte_limit=byte_budget)), True, False)


@dataclass
class SpeakResult:
    sent: list = field(default_factory=list)  # 已发出的各条正文
    dropped: int = 0  # 因插话丢弃的剩余条数（含被守卫前的插话检查拒发的那一条）
    failed: bool = False  # 某条发送函数返回了假值（未确认发出）；其后的条目不再发送

    @property
    def interrupted(self):
        return self.dropped > 0


class Interjected(Exception):
    """后续条在守卫内检测到有人插话，拒绝发送；由 ``speak`` 接住并计为丢弃。"""


class InterjectionGate:
    """把插话检测并入 ``delivery_guard``：只在 ``speak`` 发完首条（``arm``）之后生效。

    ``wrap(guard)`` 返回新的守卫可调用对象：先进入原守卫（它抛出的异常原样向上传播），
    再检测插话，有人插话就抛 ``Interjected``，不会调用原生发送。
    """

    def __init__(self, probe):
        self._probe = probe
        self._armed = False

    def arm(self):
        self._armed = True

    def __call__(self):
        return self._armed and bool(self._probe())

    def wrap(self, guard):
        @contextmanager
        def guarded():
            with guard():
                if self():
                    raise Interjected
                yield
        return guarded


def interjection_probe(members, room, *, baseline=None):
    """返回“发送开始后群里是否出现了别人的新消息”的检测函数。

    建立时记下群内当前最大消息序号（含机器人自己的行）；``baseline`` 为整数时改用它作水位
    （如触发这一轮的那条消息自己的序号：已落库但还没分发的更新消息也算新消息）。
    之后每次调用，读最近的非机器人行，
    只要有序号更大、且不是机器人账号自己回显的行，就算有人插话。机器人自己刚发的段落
    （bot_generated=1，或 is_self=1 的原生回显）不算插话。读不到数据时一律视为没有插话：
    宁可把后面几条发完，也不因为统计问题丢掉回复。
    """
    def read_baseline():
        try:
            value = members.latest_id(room)
        except Exception as error:
            logger.debug("读取群消息序号失败：%s", type(error).__name__)
            return None
        return value if type(value) is int else None

    if type(baseline) is not int:
        baseline = read_baseline()

    def probe():
        if baseline is None:
            return False
        try:
            rows = members.recent_rows(room, limit=PROBE_ROWS, include_bot=False)
        except Exception as error:
            logger.debug("读取群消息失败：%s", type(error).__name__)
            return False
        if not isinstance(rows, (list, tuple)):
            return False
        for row in rows:
            if not isinstance(row, dict) or row.get("bot_generated") or row.get("is_self"):
                continue
            row_id = row.get("id")
            if type(row_id) is int and row_id > baseline:
                return True
        return False

    return probe


def speak(text, *, tier, byte_budget, send_at, send_plain=None, interjected=None, sleep=None, rng=None,
          clock=None, result=None):
    """按档位发出一条回复，返回 ``SpeakResult``。

    * ``send_at(part)`` / ``send_plain(part)``：发送一条并返回真值表示已发出；``send_plain`` 缺省时都用 ``send_at``。
      发送函数里应套 ``delivery_guard``，它抛出的异常原样向上传播（已发出的段不重发），
      只有 ``Interjected`` 会被接住并计为丢弃。
    * ``interjected()``：仅闲聊拆分时在后续每条的发送函数被调用前检查，返回真则丢弃剩余条目；
      若它带 ``arm()``（``InterjectionGate``），发完首条后会调用，使并入守卫的检查开始生效。
    * ``sleep(seconds)`` / ``clock()``：条间隔的等待和计时，默认 ``time.sleep`` / ``time.monotonic``；
      只在闲聊拆分的条与条之间使用，等待值 = 目标值（1—3 秒）减去上一条返回后已过去的时间。
    * ``result``：可传入预先创建的 ``SpeakResult``，发送中途抛出异常时调用方仍能看到已发出的部分。
    某条返回假值时标记 ``failed`` 并停止，由调用方决定是否抛错。
    """
    plan = plan_utterance(text, tier, byte_budget=byte_budget)
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    plain = send_plain or send_at
    result = SpeakResult() if result is None else result
    arm = getattr(interjected, "arm", None)
    returned_at = None
    for index, part in enumerate(plan.parts):
        if index > 0 and plan.paced:
            remaining = gap_seconds(part, rng) - (clock() - returned_at)
            if remaining > 0:
                sleep(remaining)
            if interjected is not None and interjected():
                result.dropped = len(plan.parts) - index
                break
        send = send_at if plan.at_all or index == 0 else plain
        try:
            sent = send(part)
        except Interjected:
            result.dropped = len(plan.parts) - index
            break
        returned_at = clock()
        if not sent:
            result.failed = True
            break
        result.sent.append(part)
        if index == 0 and arm is not None:
            arm()
    return result
