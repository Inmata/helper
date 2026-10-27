"""上下文工具：把艾特回复前的预取收成一组带名字、参数和说明的函数。

现阶段仍由 ``prefetch_mention_context`` 按固定顺序一次性调用；以后接入工具调用时，
同一份注册表的 ``schemas()`` 可直接转成模型的 tools 描述，``call()`` 负责按名字执行。

本模块位于 ``conversation`` 层，不依赖 ``memory``：知识库和成员目录都按鸭子类型传入，
只用到下面这些方法：

* knowledge：``member_entity(room, wxid)``、``speaker_profile(room, wxid)``、
  ``search(room, query, *, entity_id=None, limit, char_limit)``、``lookup_people(room, query)``、
  ``style(room, wxid)``
* members：``recent_rows(room, *, limit, include_bot)``（取群友近期发言做示例，可省略）
"""
from __future__ import annotations

import copy
import logging
import random
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

# 提问很短且含这些说法时，视为“问自己是谁”，不再按提问检索背景资料，只用本人档案和记忆。
SELF_QUERY_HINTS = ("我是谁", "知道我是谁", "认识我吗", "我叫什么", "我的身份", "我是哪位")
SELF_QUERY_MAX_CHARS = 12
OWN_MEMORY_LIMIT, OWN_MEMORY_CHAR_LIMIT = 5, 1800
RELEVANT_LIMIT, RELEVANT_CHAR_LIMIT = 4, 2000

# 群友说话示例：长度、条数和候选池大小。
EXAMPLE_MIN_CHARS, EXAMPLE_MAX_CHARS = 4, 20
EXAMPLE_COUNT = 10
EXAMPLE_POOL = 200

# 通用的冒犯性词表：死亡、家人、辱骂。命中任一词的群友发言不会作为示例交给模型。
# 宁可多排除：示例只用来学语气和长度，少几条不影响效果。
OFFENSIVE_WORDS = (
    "死", "去世", "遗体", "遗照", "骨灰", "葬", "尸", "自杀", "殉", "地狱", "投胎", "阴间",
    "爸", "妈", "爹", "娘", "爷", "奶", "全家", "家人", "祖宗", "祖坟", "老母", "亲戚",
    "傻逼", "傻b", "sb", "煞笔", "沙比", "草泥马", "操你", "艹", "我草", "他妈", "你妈", "日你",
    "滚", "废物", "垃圾", "智障", "脑残", "贱", "狗", "屌", "逼", "尼玛", "畜生", "混蛋",
)


class ContextToolError(LookupError):
    """工具名不存在，或调用参数与声明不符。"""


@dataclass(frozen=True)
class ContextTool:
    """一个上下文工具：名字、说明、参数声明（JSON Schema 风格）和实现。"""

    name: str
    description: str
    parameters: dict
    func: Callable[..., Any]

    def schema(self):
        return {"name": self.name, "description": self.description,
                "parameters": copy.deepcopy(self.parameters)}


class ContextTools:
    """按名字登记和调用上下文工具。"""

    def __init__(self, tools=()):
        self._tools = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool):
        if tool.name in self._tools:
            raise ValueError(f"上下文工具重名：{tool.name}")
        self._tools[tool.name] = tool

    def names(self):
        return tuple(self._tools)

    def get(self, name):
        try:
            return self._tools[name]
        except KeyError:
            raise ContextToolError(f"没有这个上下文工具：{name}") from None

    def schemas(self):
        """可直接转成模型 tools 描述的列表，顺序即登记顺序。"""
        return [tool.schema() for tool in self._tools.values()]

    def call(self, name, **arguments):
        tool = self.get(name)
        declared = tool.parameters.get("properties", {})
        unknown = sorted(set(arguments) - set(declared))
        missing = sorted(set(tool.parameters.get("required", ())) - set(arguments))
        if unknown or missing:
            raise ContextToolError(f"工具 {name} 参数不符：多余 {unknown}，缺少 {missing}")
        return tool.func(**arguments)


def _object(properties, required):
    return {"type": "object", "properties": properties, "required": list(required)}


_ROOM = {"type": "string", "description": "群聊 ID（以 @chatroom 结尾），由调用方绑定，不由模型指定"}
_WXID = {"type": "string", "description": "成员的微信 ID"}
_QUERY = {"type": "string", "description": "当前提问的正文（已去掉艾特标签）"}


def is_offensive(text):
    lowered = text.casefold()
    return any(word in lowered for word in OFFENSIVE_WORDS)


def usable_example(text):
    """4—20 字、单行、不含艾特/链接/命令/微信 ID，且不含冒犯性词的群友发言才可作示例。"""
    if not isinstance(text, str):
        return False
    text = text.strip()
    if not EXAMPLE_MIN_CHARS <= len(text) <= EXAMPLE_MAX_CHARS or "\n" in text or "\r" in text:
        return False
    if "@" in text or "＠" in text or text.startswith("/") or "http" in text.casefold() or "wxid_" in text:
        return False
    return not is_offensive(text)


def pick_speech_examples(members, room, *, count=EXAMPLE_COUNT, pool=EXAMPLE_POOL, rng=None):
    """从本群最近 ``pool`` 条群友发言里挑 ``count`` 条短句，按原发言顺序返回。

    机器人自己的发言和机器人账号的回显都不算群友发言；读不出数据时返回空列表。
    """
    if members is None:
        return []
    try:
        rows = members.recent_rows(room, limit=pool, include_bot=False)
    except Exception as error:
        logger.debug("读取群友示例失败：%s", type(error).__name__)
        return []
    if not isinstance(rows, (list, tuple)):
        return []
    seen, candidates = set(), []
    for row in rows:
        if not isinstance(row, dict) or row.get("bot_generated") or row.get("is_self"):
            continue
        text = row.get("text")
        if usable_example(text) and text.strip() not in seen:
            seen.add(text.strip())
            candidates.append(text.strip())
    if len(candidates) <= count:
        return candidates
    chosen = sorted((rng or random).sample(range(len(candidates)), count))
    return [candidates[index] for index in chosen]


def build_default_tools(knowledge, members=None, *, rng=None):
    """登记艾特回复用到的全部上下文工具；limit/char_limit 沿用既有取值。"""
    tools = ContextTools()
    tools.register(ContextTool(
        "member_entity",
        "取得某个群成员在本群知识库里的实体记录（含 id），其他按成员检索的工具以它为入口；"
        "成员从未在本群留过记录时抛出错误。",
        _object({"room": _ROOM, "wxid": _WXID}, ("room", "wxid")),
        lambda room, wxid: knowledge.member_entity(room, wxid)))
    tools.register(ContextTool(
        "speaker_profile",
        "取得发言者的身份档案：是否已绑定真实群内人物、name、aliases、wechat_name；"
        "未绑定时只给安全的微信昵称，不能据此猜身份。",
        _object({"room": _ROOM, "wxid": _WXID}, ("room", "wxid")),
        lambda room, wxid: knowledge.speaker_profile(room, wxid)))
    tools.register(ContextTool(
        "member_memory",
        f"读取某个成员实体名下的长期记忆（最多 {OWN_MEMORY_LIMIT} 条、{OWN_MEMORY_CHAR_LIMIT} 字），"
        "不按提问筛选；entity_id 来自 member_entity。",
        _object({"room": _ROOM, "entity_id": {"type": "integer", "description": "member_entity 返回的实体 id"}},
                ("room", "entity_id")),
        lambda room, entity_id: knowledge.search(room, "", entity_id=entity_id, limit=OWN_MEMORY_LIMIT,
                                                 char_limit=OWN_MEMORY_CHAR_LIMIT)))
    tools.register(ContextTool(
        "search_memory",
        f"按提问内容在本群长期记忆里检索相关背景资料（最多 {RELEVANT_LIMIT} 条、{RELEVANT_CHAR_LIMIT} 字）；"
        "条目是过去的记录，不代表今天发生的事。",
        _object({"room": _ROOM, "query": _QUERY}, ("room", "query")),
        lambda room, query: knowledge.search(room, query, limit=RELEVANT_LIMIT, char_limit=RELEVANT_CHAR_LIMIT)))
    tools.register(ContextTool(
        "lookup_people",
        "按提问里出现的名字检索已确认的人物档案；同名可能对应多人，返回空列表表示没有匹配。",
        _object({"room": _ROOM, "query": _QUERY}, ("room", "query")),
        lambda room, query: knowledge.lookup_people(room, query)))
    tools.register(ContextTool(
        "speaker_style",
        "取得发言者的表达习惯描述，只用于理解他的反话和省略，不用来模仿或冒充他；没有时为空。",
        _object({"room": _ROOM, "wxid": _WXID}, ("room", "wxid")),
        lambda room, wxid: knowledge.style(room, wxid)))
    tools.register(ContextTool(
        "speech_examples",
        f"从本群最近 {EXAMPLE_POOL} 条群友发言里挑约 {EXAMPLE_COUNT} 条 {EXAMPLE_MIN_CHARS}—{EXAMPLE_MAX_CHARS} 字的短句，"
        "作为“群友说话的样子”；已排除机器人发言、艾特、链接、命令以及死亡、家人、辱骂类内容。",
        _object({"room": _ROOM}, ("room",)),
        lambda room: pick_speech_examples(members, room, rng=rng)))
    return tools


@dataclass(frozen=True)
class MentionContext:
    """艾特回复的预取结果，字段与 ``Dialogue.reply`` 的同名参数一一对应。"""

    knowledge: list
    style: Any
    speaker_profile: Any
    people: list
    examples: list


def prefetch_mention_context(tools, room, sender_id, query):
    """按固定顺序预取艾特回复需要的上下文；调用顺序、limit 与去重方式与重构前一致。"""
    entity = tools.call("member_entity", room=room, wxid=sender_id)
    profile = tools.call("speaker_profile", room=room, wxid=sender_id)
    own = tools.call("member_memory", room=room, entity_id=entity["id"])
    if len(query) <= SELF_QUERY_MAX_CHARS and any(hint in query for hint in SELF_QUERY_HINTS):
        relevant = []
    else:
        relevant = tools.call("search_memory", room=room, query=query)
    people = tools.call("lookup_people", room=room, query=query)
    knowledge = list({item["id"]: item for item in own + relevant}.values())
    style = tools.call("speaker_style", room=room, wxid=sender_id)
    examples = tools.call("speech_examples", room=room)
    return MentionContext(knowledge, style, profile, people, examples)
