"""短时接话：已入库的消息、还没发出的回复、刚成功发出的上一轮。"""
from collections import deque
from dataclasses import dataclass, field
import re
from threading import Condition
from time import monotonic

from ..commands import is_control
from ..integrations.jev import JevError
from ..messaging.mentions import strip_at_labels
from ..timeutil import parse_utc

COLLECT_SECONDS = 5
FOLLOW_SECONDS = 30
FOLLOW_LIMIT = 2
CLARIFICATION_SECONDS = 90
_QUESTION = re.compile(r"[?？]|你(?:问|指|说)的是|哪个|哪(?:一)?[场款天年]")
_REFERS_BACK = re.compile(r"不是|刚才|你(?:说|讲)|我说的是|到底|不对吧|(?:^|[，。])那|这(?:个|事|说法)|他们")
_SELF_REFERENCE = re.compile(r"你|刚才|刚刚|之前|上次|前面|前几天|以前|不对|说错|搞错|念|朗读|唱|语音|重说|记错")
_SING_REQUEST = re.compile(r"唱|来(?:一|半)?首|\bsing\b", re.IGNORECASE)

CORRECTION_INTENT = {
    "instructions": "当前发言是否在核对、质疑或纠正机器人自己先前的发言或实际动作？考虑引用和最近一轮。",
    "true": "说机器人以前讲过某句话、指出它答错或改口，追问它为何那样回答，"
            "或者质疑它刚发的语音其实是念的。‘你之前说结婚就选小林’即使没有问号，仍然在核对旧话。"
            "引用机器人称某分数是高手，反问‘前十万也叫高手？’也算。",
    "false": "普通新问题、群友纠正自己的错字、单纯补充新条件、评价其他人、单纯请求唱歌或朗读。",
}
SING_INTENT = {
    "instructions": "当前发言是否直接要求机器人实际唱歌或用旋律演唱？",
    "true": "‘唱一个雨爱’、‘给我用语音唱一下’、‘来首歌’。",
    "false": "要求念歌词、评论之前的语音、指出上次光念没唱、问会不会唱，或在讨论别人唱歌。",
}
READ_CONSENT = {
    "instructions": "机器人刚提出把原请求内容改为文字朗读。当前发言是否接受这项朗读提议？",
    "true": "清楚同意用朗读替代演唱，比如‘可以，那就念吧’或紧接具体朗读提议说‘行’。",
    "false": "否定、想要唱而非念、另起话题、对旧语音的评论，或要求朗读不同内容。",
}


def turn_intents(jev, question, *, quote=None, previous=None):
    if jev is None or not question:
        return set()
    checks = {}
    if quote or previous or _SELF_REFERENCE.search(question):
        checks["correction"] = CORRECTION_INTENT
    if _SING_REQUEST.search(question):
        checks["sing"] = SING_INTENT
    if previous and previous.reading_offer:
        checks["read"] = READ_CONSENT
    if not checks:
        return set()
    state = {"当前发言": question, "引用": quote, "机器人上一轮答复": previous.text if previous else ""}
    if "read" in checks:
        state["待确认的原请求"] = previous.reading_offer
    try:
        scores = jev.noul(state, list(checks.values()), timeout=2)
        return {key for key, score in zip(checks, scores, strict=True) if score >= 0.7}
    except (JevError, OSError, ValueError):
        return set()


def turn_key(message):
    return message.session_id, message.chat_id, message.sender_id


def likely_incomplete(question):
    # 短闲聊和问清楚了的内容直接答；只有像在铺素材的长句才问模型要不要等。
    return len(question) >= 20 and not any(word in question for word in (
        "?", "？", "吗", "咋", "怎么", "如何", "为什么", "哪个", "多少", "是不是",
        "帮我", "请", "说一下", "说一句", "说一遍", "用语音", "语音说", "念", "回复", "搜一下", "查一下"))


def judge_turn(jev, intent, question, *, previous="", new=""):
    criteria = {
        "collect": ("这条艾特只是贴素材、列对象、或话没说完，等同一人补问题才知道要答什么。",
                    "这已经是可直接回应的问句、请求或自然闲聊，包括能直接接的一句观点。"),
        "merge": ("新消息在补完刚刚的请求、添加或改正会影响答复的条件；应把两条合成一次答复。",
                  "只是感叹、催促、重复同一句、在和其他人说话，或提出独立的新问题。"),
        "follow": ("同一人明确在对机器人追问上一轮，指向机器人的答复或同一件事里的未解问题。",
                   "纯感叹或评价、和其他人说话、另起话题，或不清楚是不是在问机器人。"),
        "status": ("发言正在询问机器人上一轮为什么没有正常回答、是否被拦截或处理结果如何。"
                   "紧接着问‘为什么会这样’也算。",
                   "纠正或质疑机器人已经发出的某句话、另起话题、询问原问题的具体做法、询问生图任务、要求重试、调侃或与别人说话。"),
        "clarify": ("新话正好补齐机器人刚问的条件，例如地区、年份、型号、哪场比赛。允许另一位群友补充。",
                    "是道谢、单独的新问题、在和别人说话，或没有补充这轮缺的条件。"),
        "refer": ("新话确实在追问或纠正机器人刚才对原问题的答复。",
                  "只是用了相似的字眼，但在谈另一件事、跟别人说话，或只是道谢和感叹。"),
        "hook": ("这些原话在同一个尚未结束的话题里构成重复的梗，新话再次指向这个梗，有合适的接法。",
                 "只是词语巧合、例行重复、换了话题、严肃沟通，或接梗会打断别人。"),
    }
    yes, no = criteria[intent]
    try:
        context_key = "最近群聊" if intent == "hook" else "机器人刚才的答复"
        p, = jev.noul({"前一句或锚点": question, context_key: previous, "新的发言": new},
                      [{"instructions": "依据消息含义和说话对象判断，拿不准判否。", "true": yes, "false": no}],
                      timeout=2)
    except (JevError, OSError):
        return False
    return p >= 0.70


@dataclass(frozen=True)
class TurnFocus:
    question: str
    anchor: str = ""
    reference: str = ""
    clarifying: bool = False

    def context(self, previous_search=""):
        return "\n".join(text for text in (
            f"所接的原问题：{self.anchor}" if self.anchor else "",
            f"引用或前一轮答复（待核对）：{self.reference}" if self.reference else "",
            f"此前的搜索对象（仅在相关时参考）：{previous_search[:220]}" if previous_search else "",
        ) if text)


def recent_exchange(history, now):
    """找最近一次回复和它前面的原话，允许其后夹着几句其他群聊。"""
    rows = history[-8:]
    bot = lambda row: row.get("bot_generated") or row.get("is_self")
    for i in range(len(rows) - 1, 0, -1):
        if not bot(rows[i]):
            continue
        if not 0 <= (now - parse_utc(rows[i]["received_at"])).total_seconds() <= CLARIFICATION_SECONDS:
            break
        end = i + 1
        while i > 0 and bot(rows[i - 1]):
            i -= 1
        if i == 0:
            break
        reply = " ".join(strip_at_labels(row["text"]).strip() for row in rows[i:end])[:220]
        return strip_at_labels(rows[i - 1]["text"]).strip()[:180], reply
    return "", ""


def exchange_intent(question, reply):
    if 0 < len(question) <= 24 and _QUESTION.search(reply):
        return "clarify"
    if _REFERS_BACK.search(question):
        return "refer"
    return ""


@dataclass
class Turn:
    message: object
    question: str
    depth: int
    cursor: int
    sent: list = field(default_factory=list)
    replanned: bool = False


@dataclass(frozen=True)
class LastReply:
    question: str
    text: str
    sent_at: float
    depth: int
    reading_offer: str = ""
    voice_action: str = ""


class ConversationTurns:
    def __init__(self):
        self._condition = Condition()
        self._seen = deque(maxlen=128)
        self._claimed = deque(maxlen=128)
        self._merged = deque(maxlen=128)
        self._active = {}
        self._replied = {}

    def observe(self, message):
        if (not message.dispatch_sequence or not message.data.get("dispatch_to_plugins", True)
                or is_control(message.text)):
            return
        with self._condition:
            self._seen.append(message)
            self._condition.notify_all()

    def claim(self, message):
        with self._condition:
            self._claimed.append((message.dispatch_epoch, message.dispatch_sequence))

    def claimed(self, message):
        with self._condition:
            return (message.dispatch_epoch, message.dispatch_sequence) in self._claimed

    def merge(self, message):
        with self._condition:
            key = (message.dispatch_epoch, message.dispatch_sequence)
            self._merged.append(key)
            self._claimed.append(key)

    def merged(self, message):
        with self._condition:
            return (message.dispatch_epoch, message.dispatch_sequence) in self._merged

    def open(self, message, question, depth):
        turn = Turn(message, question, depth, message.dispatch_sequence)
        with self._condition:
            self._active[turn_key(message)] = turn
        return turn

    def active(self, message):
        with self._condition:
            turn = self._active.get(turn_key(message))
            return turn if turn is not None and not turn.sent else None

    def _after(self, turn):
        return (m for m in self._seen if m.dispatch_sequence > turn.cursor and turn_key(m) == turn_key(turn.message))

    def has_addition(self, turn):
        with self._condition:
            return not turn.sent and next(self._after(turn), None) is not None

    def additions(self, turn, *, deadline=None):
        """只返回同一发言者这轮尚未看的消息；等待期间收到第一条就唤醒。"""
        with self._condition:
            while self._active.get(turn_key(turn.message)) is turn:
                pending = list(self._after(turn))
                if pending:
                    turn.cursor = pending[-1].dispatch_sequence
                    return pending
                left = deadline - monotonic() if deadline is not None else 0
                if left <= 0:
                    return ()
                self._condition.wait(left)
            return ()

    def sent(self, turn, text, *, reading_offer="", voice_action=""):
        with self._condition:
            key = turn_key(turn.message)
            if self._active.get(key) is not turn:
                return
            if text:
                turn.sent.append(text)
            now = monotonic()
            self._replied = {k: value for k, value in self._replied.items()
                             if now - value.sent_at <= FOLLOW_SECONDS}
            self._replied[key] = LastReply(turn.question, "\n".join(turn.sent), now, turn.depth, reading_offer, voice_action)

    def continuation(self, message):
        with self._condition:
            previous = self._replied.get(turn_key(message))
            if (previous is not None and (previous.depth < FOLLOW_LIMIT or previous.reading_offer)
                    and 0 <= message.received_at - previous.sent_at <= FOLLOW_SECONDS):
                return previous

    def close(self, turn):
        with self._condition:
            key = turn_key(turn.message)
            if self._active.get(key) is turn:
                self._active.pop(key)

    def clear(self, room):
        with self._condition:
            self._active = {k: v for k, v in self._active.items() if k[1] != room}
            self._replied = {k: v for k, v in self._replied.items() if k[1] != room}
            self._condition.notify_all()
