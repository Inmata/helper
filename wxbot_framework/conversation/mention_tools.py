"""艾特回复里的看图 / 联网：找图、Jev 判断、调用 Magpie，整理成给模型的结果。

只在真正艾特机器人的提问里使用；普通聊天、主动发言不走这里。

* 找图：引用了图片就按引用找（原消息编号优先，其次 md5）；没有引用时只认同一群、提问前 2 分钟内发的、
  还没被看过的唯一一张（看成功过的图不再算，引用仍可再看）：先找提问人自己发的，他一张都没发时
  再找别人发的（“他发张图是啥意思”）。多张时请对方引用，不猜。
* 判断：提问里明确说看图 / 搜索，或引用了图片，就直接执行；没有引用任何消息时，只有艾特或很短、
  没有实质内容（如“现在呢”）、候选图又是提问人自己刚发的，也直接看图（隐式看图）；其余交给 Jev（``ToolTurn``），
  先判断看图，识图结果再参与搜索判断。Jev 不可用时含糊提问不调用工具。
* 预算：每轮最多识图一次、搜索一次；Jev 判断、等图和工具调用共用同一截止时间。
  客户端的超时只约束单次 socket 操作，所以每次判断 / 调用都在单独线程里执行、最多等到
  剩余时间，迟到的结果丢弃。艾特没有正文时，提问人 90 秒内的上一条发言当作问题来判断。工具失败如实交给模型说明。回复里不带网址（见 ``strip_links``）。
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..recent_images import FAILED, PENDING, READY, SKIPPED
from ..timeutil import parse_utc
from .tool_router import MAX_QUESTION_CHARS, ToolTurn, read_options

VISION = "vision"
SEARCH = "search"
# 明确要求看图 / 联网的说法；只收不会出现在普通闲聊里的写法，“查一下”这类泛称交给 Jev。
VISION_HINTS = ("看图", "看看图", "看下图", "看一下图", "看看这张图", "识图", "识别图", "识别一下图", "图里", "图上",
                "图中", "这张图", "截图里", "照片里")
SEARCH_HINTS = ("联网", "上网查", "网上查", "上网搜", "网上搜", "搜一下", "搜搜", "搜下", "帮我搜", "去搜",
                "百度一下", "谷歌一下")
IMAGE_WINDOW_SECONDS = 120
PREVIOUS_QUESTION_SECONDS = 90
IMPLICIT_QUESTION_CHARS = 4  # 艾特正文的字母和数字合计不超过这么多（标点、表情符号不计）当作没有实质问题
MENTION_MARK = chr(0x2005)  # 微信 @ 后面的分隔符：带它的上一条发言本身是一次艾特，已经回答过
PENDING_WAIT_SECONDS = 6
BUDGET_SECONDS = 60
MIN_CALL_SECONDS = 8  # 剩余时间不够一次工具调用时不再发起
JEV_MAX_SECONDS = 10  # 单次 Jev 判断的上限（实测 p50 约 0.3 秒）
MIN_JEV_SECONDS = 1
VISION_RESULT_CHARS = 800
SEARCH_RESULT_CHARS = 3000
SEARCH_VISION_CONTEXT_CHARS = 300

ASK_WHICH_IMAGE = "你刚发了好几张图，引用想让我看的那张再艾特我"
ASK_WHICH_OTHER_IMAGE = "群里刚有好几张图，引用想让我看的那张再艾特我"
ASK_FOR_IMAGE ="没找到你要我看的图，引用那张图再艾特我"
QUOTED_IMAGE_MISSING = "你引用的那张图我这边没收到，可能是我上线前发的，重新发一下再艾特我"
NO_IMAGE_SUPPORT = "我这边暂时收不到图片，看不了图"

_STATUS_NOTE = {FAILED: "图片没下载成功", SKIPPED: "图片没有下载", PENDING: "图片还在下载，没等到"}
_ERROR_NOTE = {"MagpieToolsUnavailableError": "工具服务连不上或超时", "MagpieToolsAuthError": "工具服务鉴权失败",
               "MagpieToolsFormatError": "工具返回的格式不对", "MagpieToolsToolError": "工具执行出错",
               "TimeoutError": "工具服务连不上或超时"}
_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\(\s*(?:https?://|www\.)[^)]*\)")
_DOMAIN_TEXT = re.compile(r"[\w-]+(?:\.[\w-]+)+")  # 链接文字本身就是域名时一并去掉
_BARE_URL = re.compile(r"(?:https?://|www\.)[^\s)\]>\"'，。、；）】]*")


@dataclass(frozen=True)
class ToolResult:
    tool: str
    ok: bool
    text: str = ""  # 工具返回的文本（成功时）
    note: str = ""  # 没成功的原因（给模型如实转述，不含路径和原始错误）

    def payload(self):
        return {"tool": self.tool, "ok": self.ok, "text": self.text, "note": self.note}


@dataclass
class ToolOutcome:
    results: list = field(default_factory=list)
    ask: str | None = None  # 非空时不生成回复，直接用这句请对方说明

    @property
    def used(self):
        return bool(self.results)

    def payload(self):
        return [result.payload() for result in self.results]


def read_tool_options(config):
    """``mention_chat.tools``：``{enabled, vision_threshold, search_threshold}``；关闭时返回 None。"""
    section = config.get("tools", {})
    if not isinstance(section, dict):
        raise ValueError("mention_chat.tools 应为 JSON 对象")
    enabled = section.get("enabled", True)
    if type(enabled) is not bool:
        raise ValueError("mention_chat.tools.enabled 应为布尔值")
    thresholds = read_options({key: section[key] for key in ("vision_threshold", "search_threshold")
                               if key in section})
    return thresholds if enabled else None


def wants(text, hints):
    return any(hint in text for hint in hints)


def vague_question(text):
    """艾特正文的字母和数字合计不超过 4 个（标点、表情符号不计；空、“现在呢”“咋样”）：没有实质问题。"""
    return sum(char.isalnum() for char in text or "") <= IMPLICIT_QUESTION_CHARS


def strip_links(reply):
    """去掉回复里的网址：Markdown 链接留文字（文字是域名时也去掉），裸网址整段删掉。

    带链接的消息容易触发微信风控，所以不管模型是否照抄了搜索结果里的网址，发出去的回复都不含链接。
    """
    text = _MARKDOWN_LINK.sub(lambda match: "" if _DOMAIN_TEXT.fullmatch(match.group(1).strip()) else match.group(1),
                             reply or "")
    text = _BARE_URL.sub("", text)
    text = re.sub(r"[（(]\s*[）)]", "", text)
    return re.sub(r"[ \t]+\n", "\n", text).strip()


def previous_question(rows, *, now=None, within=PREVIOUS_QUESTION_SECONDS, is_mention=None):
    """艾特没有正文时，提问人上一条发言（``within`` 秒内、本身不是艾特）当作这次的问题；没有就返回空串。

    ``now`` 应是这条艾特到达的时刻（不是处理它的时刻，插件串行处理时中间可能排过队）；
    ``is_mention(text)`` 识别手打的 @机器人（没有分隔符），它和带分隔符的艾特一样已经回答过。
    """
    if not rows:
        return ""
    row = rows[-1]
    text = str(row.get("text") or "").strip()
    if not text or MENTION_MARK in text or (is_mention is not None and is_mention(text)):
        return ""
    try:
        age = ((now or datetime.now(timezone.utc)) - parse_utc(row.get("received_at"))).total_seconds()
    except (TypeError, ValueError):
        return ""
    return text[:MAX_QUESTION_CHARS] if 0 <= age <= within else ""


def _vision_prompt(question):
    ask = f"群友的提问是：{question}\n" if question else ""
    return (ask + "请客观描述这张图里和提问相关的内容，图中有文字时照录关键文字；看不清的地方直接说看不清，"
            "不要猜测或编造，控制在 200 字以内。")


def _search_query(question, vision_text):
    if not vision_text:
        return question
    return f"{question}\n（提问附带的图片内容：{vision_text[:SEARCH_VISION_CONTEXT_CHARS]}）"


def _error_note(error, tool):
    if isinstance(error, ValueError):
        return "图片格式或大小不受支持" if tool == VISION else "搜索词无效"
    for base in type(error).__mro__:
        if base.__name__ in _ERROR_NOTE:
            return _ERROR_NOTE[base.__name__]
    return "工具调用失败"


def _within(work, timeout):
    """在单独线程里执行 ``work``，最多等 ``timeout`` 秒；超时抛 ``TimeoutError``，迟到的结果丢弃。"""
    box = {}

    def target():
        try:
            box["value"] = work()
        except BaseException as error:  # 交回调用线程按原类型处理
            box["error"] = error

    thread = threading.Thread(target=target, name="mention-tool", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError("超过本轮截止时间")
    if "error" in box:
        raise box["error"]
    return box["value"]


class MentionTools:
    """一次艾特提问的工具调用；``magpie`` 是延迟取得 MagpieTools 的可调用对象（取不到时抛异常）。"""

    def __init__(self, *, router, magpie, images, image_supported, logger, clock=time.monotonic,
                 budget=BUDGET_SECONDS):
        self.router = router
        self.magpie = magpie
        self.images = images
        self.image_supported = image_supported
        self.logger = logger
        self.clock = clock
        self.budget = budget

    def run(self, *, question, room, sender, received_at, quote=None, context_question=""):
        """``context_question``：艾特没有正文时提问人上一条发言（见 ``previous_question``），只用来判断和调用工具。"""
        started = self.clock()
        deadline = started + self.budget
        quote = quote or {}
        has_quote = any(quote.get(key) for key in ("name", "text", "ref_type"))  # 引用了任意一条消息
        outcome = ToolOutcome()
        asked = question  # 艾特自己的正文；回填上一句话后仍用它判断这次艾特有没有实质问题
        question = question or context_question
        explicit_vision = wants(question, VISION_HINTS)
        explicit_search = wants(question, SEARCH_HINTS)
        quoted_image = quote.get("ref_type") == "3"
        image, candidates, ask_which = None, (), ASK_WHICH_IMAGE
        own_candidates = False
        if quoted_image:
            image = self.images.quoted(room, svrid=quote.get("svrid") or "", md5=quote.get("image_md5") or "")
        else:
            candidates = self.images.recent(room, sender, before=received_at, within=IMAGE_WINDOW_SECONDS)
            own_candidates = bool(candidates)
            if not candidates:
                candidates = self.images.recent(room, sender, before=received_at, within=IMAGE_WINDOW_SECONDS,
                                                from_others=True)
                ask_which = ASK_WHICH_OTHER_IMAGE
            if len(candidates) == 1:
                image = candidates[0]
        has_image = quoted_image or bool(candidates)
        turn = ToolTurn(self.router, question=(question or "（只艾特了机器人，没有正文）")[:MAX_QUESTION_CHARS],
                        has_image=has_image)

        vision_reason = "explicit" if (quoted_image or explicit_vision) else None
        if vision_reason is None and has_image:
            if not has_quote and own_candidates and vague_question(asked):
                # 没有引用、提问人自己刚发了一张没看过的图，这次艾特又没问出实质内容（只有艾特、“现在呢”）：
                # 默认就是要看那张图，不必再让 Jev 判断（实测这类提问概率偏低，容易被拒）。
                # 引用了别的消息（如引用一句文本回“谢谢”）时不算，仍按原有 Jev 判断。
                self.logger.info("艾特工具：隐式看图，跳过看图判断")
                vision_reason = "implicit"
            else:
                decision = self._judge(turn.consider_vision, deadline, "看图")
                vision_reason = "jev" if decision is not None and decision.use else None
        vision_text = None
        if vision_reason:
            if len(candidates) > 1:
                outcome.ask = ask_which
                return outcome
            if image is None:
                # 引用的图对不上记录（多半是上线前发的）也不让模型猜，直接请对方重发。
                outcome.ask = (NO_IMAGE_SUPPORT if not self.image_supported else
                               QUOTED_IMAGE_MISSING if quoted_image else ASK_FOR_IMAGE)
                return outcome
            result = self._vision(image, question, deadline)
            outcome.results.append(result)
            if result.ok:
                self.images.mark_used(image)
                vision_text = result.text
                turn.record_vision(result.text)

        if question:
            search_reason = "explicit" if explicit_search else None
            if search_reason is None:
                decision = self._judge(turn.consider_search, deadline, "搜索")
                search_reason = "jev" if decision is not None and decision.use else None
            if search_reason:
                result = self._search(_search_query(question, vision_text), deadline)
                outcome.results.append(result)
                if result.ok:
                    turn.record_search(result.text)
        self.logger.info("艾特工具：%s 耗时 %.1fs", "、".join(
            f"{item.tool}={'ok' if item.ok else 'failed'}" for item in outcome.results) or "未使用",
            self.clock() - started)
        return outcome

    def _judge(self, consider, deadline, label):
        """让 Jev 判断一次；剩余时间不够判断加一次调用时不判断，按不需要处理。"""
        remaining = deadline - self.clock()
        if remaining < MIN_CALL_SECONDS + MIN_JEV_SECONDS:
            self.logger.info("艾特工具：%s判断跳过，本轮时间不够", label)
            return None
        timeout = min(JEV_MAX_SECONDS, remaining - MIN_CALL_SECONDS)
        try:
            decision = _within(lambda: consider(timeout=timeout), timeout)
        except TimeoutError:
            self.logger.info("艾特工具：%s判断超时，按不需要处理", label)
            return None
        self.logger.info("艾特工具：%s判断 status=%s p=%s", label, decision.status, decision.probability)
        return decision

    def _client(self):
        try:
            return self.magpie()
        except Exception as error:  # 配置读不出来也只当工具不可用
            self.logger.warning("Magpie 工具不可用：%s", type(error).__name__)
            return None

    def _vision(self, image, question, deadline):
        if image.status == PENDING:
            image = self.images.wait(image, min(PENDING_WAIT_SECONDS, max(0, deadline - self.clock())))
        if image.status != READY:
            note = _STATUS_NOTE.get(image.status, "图片不可用")
            return ToolResult(VISION, False, note=f"{note}：{image.error}" if image.error else note)
        return self._call(VISION, deadline, lambda client, timeout: client.analyze_image(
            image.path, _vision_prompt(question), max_chars=min(VISION_RESULT_CHARS, client.max_result_chars),
            timeout=timeout))

    def _search(self, query, deadline):
        return self._call(SEARCH, deadline, lambda client, timeout: client.search(
            query[:1000], max_chars=min(SEARCH_RESULT_CHARS, client.max_result_chars), timeout=timeout))

    def _call(self, tool, deadline, invoke):
        if deadline - self.clock() < MIN_CALL_SECONDS:
            return ToolResult(tool, False, note="这轮时间不够，没来得及调用")
        client = self._client()
        if client is None:
            return ToolResult(tool, False, note="工具服务不可用")
        try:
            remaining = max(0.1, deadline - self.clock())
            text = _within(lambda: invoke(client, remaining), remaining).text.strip()
        except Exception as error:  # 原始错误可能带路径或服务端细节，只转述类别
            self.logger.warning("艾特工具 %s 失败：%s", tool, type(error).__name__)
            return ToolResult(tool, False, note=_error_note(error, tool))
        if not text:
            return ToolResult(tool, False, note="工具没有返回内容")
        return ToolResult(tool, True, text=text)
