"""真实群艾特名单来自微信 msgsource，不凭可见昵称猜测目标。

除结构化艾特外，这里还识别两种“在叫机器人”的写法：

* 手打或复制粘贴的 ``@名字``：名字与机器人昵称、群昵称、配置别名或本次运行学到的标签一致；
* 引用机器人自己的消息（``refermsg`` 的 ``chatusr``/``fromusr`` 是本账号）。

引用消息的正文在 ``appmsg/title``，被引用的原文在 ``refermsg``；解析只做只读 XML 查询，
拒绝 DOCTYPE/ENTITY，超长直接放弃。
"""
import re
import threading
from xml.etree import ElementTree

from ..account import validate_account

# 手打/复制的 @ 名字按已知称呼匹配：名字里允许空格，但前后必须是分隔符或标点，
# 免得把邮箱、域名里的 @ 当成艾特。微信插入的结构艾特用 U+2005 分隔。
_SEPARATORS = " \t\r\n ，。！？、；：,.!?;:…—（）()【】[]「」『』\"'“”‘’"
_EMAIL_CHARS = "._-"
# 结构艾特的可见标签：微信在名字后插入 U+2005 作为分隔符。
_VISIBLE_AT = re.compile(r"@([^@\s\u2005]{1,24})\u2005")

QUOTE_MAX_CHARS = 8191
QUOTE_TEXT_MAX_CHARS = 300
_IMAGE_MD5 = re.compile(r"(?<![\w])md5\s*=\s*[\"']([0-9a-fA-F]{32})[\"']")

_learned = {}
_learned_lock = threading.Lock()


def _label_boundary(text, index, length):
    """``text[index:index+length]`` 这一段 ``@名字`` 前后是否落在边界上。"""
    before = text[index - 1] if index else ""
    after_index = index + length
    after = text[after_index] if after_index < len(text) else ""
    if before and before.isascii() and (before.isalnum() or before in _EMAIL_CHARS):
        return False
    return after == "" or after in _SEPARATORS


def parse_at_members(source):
    if not isinstance(source, str) or len(source) > 4095 or "<!DOCTYPE" in source.upper() or "<!ENTITY" in source.upper():
        return ()
    try:
        root = ElementTree.fromstring(source)
    except (ElementTree.ParseError, ValueError):
        return ()
    node = root if root.tag == "atuserlist" else root.find(".//atuserlist")
    if node is None or not node.text:
        return ()
    result = []
    for value in node.text.split(","):
        value = value.strip()
        try:
            validate_account(value)
        except ValueError:
            continue
        if value not in result:
            result.append(value)
    return tuple(result)


def strip_at_labels(text):
    return re.sub(r"@[^@\r\n\u2005]+\u2005", "", text).strip()


def _quoted_image_md5(content):
    """被引用图片 XML 里的 md5；只取这一项，aeskey、CDN 地址等不带出来。"""
    match = _IMAGE_MD5.search(content or "")
    return match.group(1).lower() if match else ""


def parse_quote(source):
    """解析引用消息 XML：返回 ``{title, name, text, ref_type, chatusr, fromusr, svrid, image_md5}``，
    不是引用时返回 None。

    ``title`` 是这次回复的正文，``text`` 是被引用的原文（只取文本类引用，截断到 300 字）；
    ``svrid`` 是被引用消息的编号，引用图片（ref_type 3）时 ``image_md5`` 是那张图的 md5，用来找本地图片。
    """
    if not isinstance(source, str) or len(source) > QUOTE_MAX_CHARS:
        return None
    upper = source.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        return None
    start = source.find("<")
    if start < 0:
        return None
    try:
        root = ElementTree.fromstring(source[start:])
    except (ElementTree.ParseError, ValueError):
        return None
    appmsg = root if root.tag == "appmsg" else root.find(".//appmsg")
    if appmsg is None:
        return None
    refer = appmsg.find("refermsg")
    if refer is None:
        return None
    ref_type = (refer.findtext("type") or "").strip()
    svrid = (refer.findtext("svrid") or "").strip()
    return {"title": (appmsg.findtext("title") or "").strip(),
            "name": (refer.findtext("displayname") or "").strip(),
            "text": (refer.findtext("content") or "").strip()[:QUOTE_TEXT_MAX_CHARS] if ref_type == "1" else "",
            "ref_type": ref_type,
            "chatusr": (refer.findtext("chatusr") or "").strip(),
            "fromusr": (refer.findtext("fromusr") or "").strip(),
            "svrid": svrid if svrid.isascii() and svrid.isdigit() and len(svrid) <= 20 else "",
            "image_md5": _quoted_image_md5(refer.findtext("content")) if ref_type == "3" else ""}


def typed_at(text, names=()):
    """手打/复制的 @名字：返回命中的那个名字，没有则 None。

    按已知称呼逐个匹配，名字里可以有空格；前面是英文字母/数字/._- 的 ``@`` 视为邮箱或域名，
    后面必须紧跟分隔符、标点或结尾，避免 ``@小助手abc`` 这类前缀误命中。
    """
    if not isinstance(text, str) or not text:
        return None
    haystack = text.casefold()
    ordered = sorted({name for name in names if isinstance(name, str) and name}, key=len, reverse=True)
    for name in ordered:
        needle = ("@" + name).casefold()
        start = 0
        while True:
            index = haystack.find(needle, start)
            if index < 0:
                break
            start = index + 1
            if _label_boundary(text, index, len(needle)):
                return name
    return None


def learned_aliases(room=None):
    """本次运行学到的机器人称呼；给出群 ID 时只返回该群学到的。"""
    with _learned_lock:
        if room is None:
            return tuple(sorted(set().union(*_learned.values()))) if _learned else ()
        return tuple(sorted(_learned.get(room, ())))


def learn_alias(label, room=""):
    """记下群友真实艾特时用的可见标签：同一群这次运行里手打 ``@同一个标签`` 也认。"""
    if isinstance(label, str) and 0 < len(label) <= 24:
        with _learned_lock:
            _learned.setdefault(room or "", set()).add(label)


def reset_learned_aliases():
    with _learned_lock:
        _learned.clear()


def bot_names(members, room, aliases=()):
    """机器人可能被叫到的名字：微信昵称、群昵称、配置别名、本次运行学到的标签。"""
    names = list(aliases or ())
    for getter, args in (("wechat_name", (members.account,)), ("display_name", (members.account, room))):
        try:
            name = getattr(members, getter)(*args)
        except Exception:  # 读不到昵称（或成员目录没有这个方法）不影响艾特判定
            name = None
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    names.extend(learned_aliases(room))
    return tuple(dict.fromkeys(names))


def at_source(message, members, aliases=()):
    """这条消息是不是在叫机器人：返回 ``structural`` / ``quote`` / ``typed``，不是则 None。

    结构化艾特时顺带学习可见标签：只有恰好一个 ``@标签`` 时才能确定那是机器人的名字。
    """
    data = message.data or {}
    account = getattr(members, "account", None)
    if not isinstance(account, str) or not account:
        return None
    if data.get("is_at_self") and account in data.get("at_members", ()):
        labels = set(_VISIBLE_AT.findall(message.text or ""))
        if len(labels) == 1:
            learn_alias(labels.pop(), message.chat_id)
        return "structural"
    quote = data.get("quote") or {}
    if account in (quote.get("chatusr"), quote.get("fromusr")):
        return "quote"
    text = message.text or ""
    if "@" in text and typed_at(text, bot_names(members, message.chat_id, aliases)):
        return "typed"
    return None
