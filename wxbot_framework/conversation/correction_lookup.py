"""把本轮指代、待回答的关系和记录的适用时间分别对上。"""
from dataclasses import dataclass
from datetime import date, timedelta
import re
import unicodedata

from ..integrations.jev import JevError
from .reply_review import _speech_terms


def normalize(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def named(text, label):
    text, label = normalize(text), normalize(label)
    if len(label) < 2:
        return False
    if label.isascii():
        return re.search(r"(?<![a-z0-9])" + re.escape(label) + r"(?![a-z0-9])", text) is not None
    return label in text


def candidates(facts, question, *, quote="", recent=(), used=()):
    """对象、关联消息和问法各保留候选，版本共用一个名额。"""
    groups = {}
    q_terms = _speech_terms(normalize(question))
    for fact in facts:
        key = fact.scope, fact.subject_key, fact.relation
        group = groups.setdefault(key, {"records": [], "name": False, "exact": False, "phrase": 0,
                                        "indexed": False, "quote": False, "used": False, "context": 0})
        group["records"].append(fact)
        group["name"] |= any(named(question, name) for name in fact.names)
        group["quote"] |= bool(quote) and any(named(quote, name) for name in fact.names)
        group["used"] |= fact.id in used
        group["context"] = max(group["context"], max(
            (number for number, text in enumerate(recent, 1) if any(named(text, name) for name in fact.names)),
            default=0))
        group["exact"] |= any(normalize(question).strip("？?。！!") == normalize(phrase).strip("？?。！!")
                              for phrase in fact.phrases)
        group["indexed"] |= any(len(phrase) >= 3 and named(question, phrase) for phrase in fact.phrases)
        group["phrase"] = max(group["phrase"], max(
            (len(q_terms & _speech_terms(normalize(text))) for text in (fact.relation, *fact.phrases)), default=0))
    ordered = [group for group in groups.values()
               if group["name"] or group["quote"] or group["used"] or group["context"] or group["phrase"] >= 2]
    result = {}
    for key, limit in (("used", 2), ("quote", 2), ("name", 3), ("context", 3), ("phrase", 2)):
        lane = sorted((group for group in ordered if group[key]),
                      key=lambda item: (item["exact"], item[key], item["phrase"], item["context"]), reverse=True)
        for group in lane[:limit]:
            first = group["records"][0]
            result[(first.scope, first.subject_key, first.relation)] = group
    return list(result.values())[:12]


@dataclass(frozen=True)
class CorrectionMatches:
    answers: tuple = ()
    premises: tuple = ()
    refresh: tuple = ()
    ambiguous: tuple = ()
    shortlisted: tuple = ()

    @property
    def facts(self):
        return self.answers + self.premises

    def clarification(self):
        if len(self.ambiguous) == 2:
            left, right = self.ambiguous
            return f"你问的是{left}，还是{right}？"
        return "你接的是哪一件事？"


AMBIGUOUS = {
    "instructions": "本轮要回答具体事实，但这句话在两个并行话题之间没有清楚指向吗？只看发言和前文，忽略候选是否能答。",
    "true": "两个独立主体都符合这句追问，当前没点名、没有有用引用、也没明确要求分别回答。‘他们’本身不等于询问两家。",
    "false": "当前点名、切换或引用后主体明确；唯一一条前文与这句有实际承接；或本句明确问多个对象；或只做翻译创作。"}
USE_CURRENT = {
    "instructions": "本句在问答复时刻的最新状态，还是问一件事是否发生、发生时或指定历史日期的情况？",
    "true": "问现在谁在任、是否仍然或又发生变化、当前版本等实时状态。现在作为口头衔接、问后来谁接替也要区分。",
    "false": "问已发生事件、当时或指定日期；只翻译；预测未知去向而不在问现任是谁。"}


def route(facts, question, judge, *, quote="", recent=(), used=(), now, logger, anchor_id):
    groups = candidates(facts, question, quote=quote, recent=[row["内容"] for row in recent], used=used)
    if not groups:
        return CorrectionMatches()
    shortlist = tuple(group["records"][0].id for group in groups)
    state = {"本句": question, "明确引用及原问题": quote, "前文": recent,
             "候选": [{**group["records"][0].material(),
                     "对象名称": group["records"][0].names, "从本句捞到对象名": group["name"],
                     "入库时登记的常见问法": group["records"][0].phrases,
                     "从引用找到": group["quote"], "该引用的原回答用过": group["used"]}
                     for group in groups]}
    checks = [AMBIGUOUS, USE_CURRENT]
    for group in groups:
        label = f"候选编号 {group['records'][0].id}"
        checks += [
            {"instructions": f"{label}的对象和范围是否就是本句明确或从前文承接要谈的对象？",
             "true": "当前句、可靠引用或同一话题能确定这个对象。对象只指代机构时，机构任职关系也属于此对象。",
             "false": "对象只在别的话题中出现、身份同名、当前已经换题、版本或适用条件不同，或没有清楚依据。"},
            {"instructions": f"假定对象已经对上，{label}描述的具体关系能直接回答这句在问的事实吗？",
             "true": "同一个关系，包括问后来谁接班、谁掌舵而记录是接任。只问关系，时间适用性由程序处理。",
             "false": "离任和接任是不同关系。只问创办、去向、推测、翻译、改写或评价，不算问该事实。"},
            {"instructions": f"{label}是不是这句追问明确承接的前提？允许它不直接回答这句话。",
             "true": "前文或本句明确采用这条认识，并追问它之后的可能安排、影响或下一步。",
             "false": "只同名、只同公司、同一件事的旁支、前文没采用这条认识，或就是本句直接问的关系。"},
        ]
    exact = [index for index, group in enumerate(groups) if group["exact"] and group["name"]]
    try:
        scores = judge.noul(state, checks, timeout=2) if judge is not None else ()
        if len(scores) != len(checks):
            scores = ()
    except (JevError, OSError, ValueError, TimeoutError):
        scores = ()
    if not scores:
        if len(exact) != 1:
            return CorrectionMatches(shortlisted=shortlist)
        scores = [0.0, 1.0] + [value for i in range(len(groups)) for value in (i in exact, i in exact, False)]

    # 只有真实引用或本句中的对象能压过并行话题的歧义，候选强弱本身不能定对象。
    direct_hits = [i for i, group in enumerate(groups)
                   if scores[3 + 3 * i] >= (0.54 if group["name"] and group["indexed"] else 0.66)]
    matched = [i for i in direct_hits if scores[2 + 3 * i] >= 0.64]
    explicit = [i for i in matched if groups[i]["name"]]
    linked = [i for i in matched if groups[i]["quote"] or groups[i]["used"]]
    if scores[0] >= 0.68 and not explicit and not linked:
        names = tuple(dict.fromkeys(group["records"][0].subject for i, group in enumerate(groups)
                                    if group["context"] and scores[3 + 3 * i] >= 0.60))
        logger.info("纠错检索：消息=%s 候选=%s 对象未定=%s", anchor_id, shortlist, names[:2])
        return CorrectionMatches(ambiguous=names[:2] or ("需要前文",), shortlisted=shortlist)
    answers, premises, refresh = [], [], []
    chosen = explicit or linked
    for i, group in enumerate(groups):
        target, _, premise = scores[2 + 3 * i:5 + 3 * i]
        direct = i in direct_hits
        if target < 0.64 or not (direct or premise >= 0.66):
            continue
        if chosen and i in matched and i not in chosen:
            continue
        selected, expired = _version(group["records"], question, now, current=scores[1] >= 0.80)
        if selected is None:
            continue
        if expired and direct:
            refresh.append(selected)
        elif direct:
            answers.append(selected)
        elif not expired:
            premises.append(selected)
    match = CorrectionMatches(tuple(answers[:3]), tuple(premises[:3]), tuple(refresh[:3]), shortlisted=shortlist)
    logger.info("纠错检索：消息=%s 候选=%s 直接=%s 前提=%s 待更新=%s",
                anchor_id, shortlist, [f.id for f in match.answers], [f.id for f in match.premises],
                [f.id for f in match.refresh])
    return match


DATE = re.compile(r"(?:(\d{4})[-年])?(\d{1,2})[-月](\d{1,2})(?:日|\b)")


def _version(records, question, now, *, current):
    on = now.date()
    requested = DATE.search(question)
    if requested:
        try:
            on = date(int(requested[1] or on.year), int(requested[2]), int(requested[3]))
        except ValueError:
            pass
    applicable = [row for row in records if (not requested or row.effective_on) and row.effective_on <= on.isoformat()
                  and (not current or requested or row.status == "active")]
    if not applicable:
        return None, False
    latest = max(applicable, key=lambda row: (row.effective_on, row.observed_on, row.id))
    overdue = latest.kind == "状态" and latest.scope == "公开" and current and not requested and (
        latest.observed_on < (on - timedelta(days=2)).isoformat())
    return latest, overdue
