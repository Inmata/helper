"""重复回复检测：生成的新话和自己刚说过的太像时，提示换角度重试一次。

判据取两条，任一命中即算相近：

* 开头若干字相同（同一句“某某你今天…”式开场）；
* 去掉标点后的字二元组 Jaccard 相似度不低于阈值。

只做提示和重试，不做改写：第二次仍然相近也照常发出，由调用方记日志。
"""
import unicodedata

HEAD_CHARS = 4
JACCARD = 0.5
RECENT_LIMIT = 5


def _body(text):
    """去掉标点、空白和符号，只留字和数字；称呼前缀里的 @ 一并去掉。"""
    return "".join(char for char in text or "" if unicodedata.category(char)[0] in "LN")


def bigrams(text):
    body = _body(text)
    return {body[index:index + 2] for index in range(len(body) - 1)}


def similarity(text, other):
    """两条正文的字二元组 Jaccard 相似度；任一条短到没有二元组时为 0。"""
    left, right = bigrams(text), bigrams(other)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def too_similar(text, recent, *, head=HEAD_CHARS, jaccard=JACCARD):
    """与 ``recent``（机器人最近几条发言，从旧到新）里最像的一条太像时返回该条，否则返回 None。"""
    body = _body(text)
    if not body:
        return None
    for other in reversed(list(recent)[-RECENT_LIMIT:]):
        other_body = _body(other)
        if not other_body:
            continue
        if len(body) >= head and len(other_body) >= head and body[:head] == other_body[:head]:
            return other
        if similarity(text, other) >= jaccard:
            return other
    return None
