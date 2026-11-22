"""从真实消息或抓取到的一手正文确认纠错提案。"""
from datetime import date
import json
import re

from ..integrations.jev import JevError
from ..integrations.llm import LLMContentBlocked, LLMError
from ..integrations.magpie_tools import MagpieToolsError
from .correction_lookup import normalize
from .mention_tools import _within


PROPOSE = """当前群友在纠正或更新一项事实。只提炼他正在纠正的客观事实，不采集旁支、猜测、模型承认动作或普通闲聊。
只输出 {"纠错":[{"范围":"公开或本群","对象":"标准名字，状态以机构或对应对象为主",
"关系":"短而稳定的关系，例如辞任CEO、现任CEO、集合地点、本人偏好","事实":"一句独立事实，保留原话限定",
"类别":"事件、状态或限定事实","适用范围":"仅限定事实填具体适用范围",
"生效日期":"YYYY-MM-DD，未知留空","资料确认至":"YYYY-MM-DD，未知留空",
"关联名称":["材料中涉及的人或机构的名称"],"常见问法":["同一个事实的另一种问法"],
"网址":"已经看到的一手来源网址","消息编号":0,"依据类型":"公开或本人或群决定或发言"}]}。
公开事实采用具体主体和在材料中已出现的一手发布来源，如监管披露、该组织公告或版本说明。
提供了已读正文时，从正文提取事实与日期。将相关公司和人物的名称都放入关联名称，以便通过公司和个人分别找到这条事实。
资料确认至写正文实际能证实的日期。不要用本次核实的日期替代来源日期。
限定事实指特定版本、版次或明确条件下成立的事实。对象要包含这个范围，例如 Python 3.14，而不要只写 Python。
官方正文足以确认某个限定事实时，缺日期可留空。会变化的现状和有明确发生时间的事件沿用各自的类别。
本群事实只用本群原话的实际编号。本人资料用本人原话；群决定用能直接确认决定的原话；发言记录只确认某人说了什么。
群决定只能来自程序已标记的本群管理员。决定或偏好从这条消息起适用时，程序可根据实际消息时间补确认日期。
明确区分历史事件、现状及发布日期。发生过的事件独立记录，现任状态可以被更新。同一主体同一关系尽量沿用已有命中的关系名。
如果没有实际支持或当前在讨论猜测，输出 {"纠错":[]}。通常只记一条。确实纠正了两项独立事实时可各写一条。
关联名称和常见问法各写至多三个，事实控制在80字以内。"""


def _date(value, today):
    if not isinstance(value, str):
        return ""
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return ""
    return parsed.isoformat() if parsed <= today else ""


def _strings(value, limit):
    return tuple(dict.fromkeys(item.strip()[:90] for item in value[:limit]
                              if isinstance(item, str) and len(item.strip()) >= 2)) if isinstance(value, list) else ()


def capture_corrections(model, judge, ctx, message, snapshot, question, quote, sources, matches, *, guard, store):
    if judge is None:
        return ()
    members = ctx.members
    room = snapshot.room
    own = {"消息编号": snapshot.anchor_id, "发言人": snapshot.speaker_name, "内容": message.text,
           "本群管理员": members.is_admin(message.sender_id)}
    history = [{"消息编号": row["id"], "发言人": row.get("speaker_name"), "内容": row["text"][:400],
                "机器人发言": bool(row.get("bot_generated")), "本群管理员": members.is_admin(row.get("sender_id"))}
               for row in snapshot.history[-10:]]
    url_pattern = r'https?://[^\s\])}>，。；"\x27]+'
    direct_urls = tuple(dict.fromkeys(re.findall(url_pattern, message.text)))[:2]
    search_text = "\n".join(row["内容"] for row in sources if row.get("类别") == "公开搜索结果")
    urls = {*direct_urls, *re.findall(url_pattern, search_text)}
    pages = {}

    def read_page(url):
        if url not in pages:
            try:
                with guard():
                    pass
                pages[url] = _within(lambda: ctx.magpie.fetch(url).text, 8)[:4000]
            except (MagpieToolsError, ValueError, OSError, TimeoutError) as error:
                ctx.logger.info("纠错来源读取失败：消息=%s error=%s", snapshot.anchor_id, type(error).__name__)
                pages[url] = ""
        return pages[url]

    for url in direct_urls:
        read_page(url)
    state = {"本次纠正": question, "当前日期": members.now().date().isoformat(), "引用": quote,
             "当前原话": own, "前文": history, "已找到资料": sources,
             "已读公开正文": [{"网址": url, "正文": body} for url, body in pages.items() if body],
             "已命中的记录": [row.material() for row in (*matches.facts, *matches.refresh)]}
    try:
        result = model.complete_json(PROPOSE, json.dumps(state, ensure_ascii=False))
    except LLMContentBlocked:
        raise
    except (LLMError, TimeoutError) as error:
        ctx.logger.info("纠错提案未取到：消息=%s error=%s", snapshot.anchor_id, type(error).__name__)
        return ()
    proposals = result.get("纠错")
    if not isinstance(proposals, list):
        return ()
    today = members.now().date()
    ctx.logger.info("纠错提案：消息=%s 条数=%s 已读源=%s", snapshot.anchor_id, len(proposals), len(pages))
    confirmed = []
    for row in proposals[:2]:
        if not isinstance(row, dict):
            continue
        scope, subject, relation, statement, kind = [row.get(key) for key in ("范围", "对象", "关系", "事实", "类别")]
        if (scope not in ("公开", "本群") or kind not in ("事件", "状态", "限定事实") or
                any(not isinstance(value, str) or not 1 <= len(value.strip()) <= limit for value, limit
                    in ((subject, 90), (relation, 60), (statement, 320)))):
            continue
        subject, relation, statement = subject.strip(), relation.strip(), statement.strip()
        on = _date(row.get("生效日期"), today)
        observed = _date(row.get("资料确认至"), today)
        applies_to = row.get("适用范围")
        applies_to = applies_to.strip() if isinstance(applies_to, str) else ""
        names = (subject, *_strings(row.get("关联名称"), 5))
        key = normalize(subject)
        if scope == "公开":
            url = row.get("网址")
            if not isinstance(url, str) or url not in urls:
                continue
            body = read_page(url)
            if not body:
                continue
            source = {"网址": url, "正文": body, "类型": "公开来源"}
            if kind == "限定事实":
                if not 2 <= len(applies_to) <= 150:
                    continue
                source.update(适用范围=applies_to, 核实日期=today.isoformat())
        else:
            if kind == "限定事实":
                continue
            source_id = row.get("消息编号")
            if type(source_id) is not int or not 0 < source_id <= snapshot.anchor_id:
                continue
            records = members.recent_rows(room, before_id=source_id + 1, after_id=source_id - 1,
                                          context=True, limit=1)
            if not records or records[0]["bot_generated"] or records[0]["context_kind"] == "forwarded":
                continue
            raw = records[0]
            evidence_kind = row.get("依据类型")
            if evidence_kind == "群决定":
                if not members.is_admin(raw["sender_id"]):
                    continue
                authority = "程序已对上发言账号，确认是本群管理员，有权确认本群决定。"
            elif evidence_kind in ("本人", "发言"):
                person = members.find_by_wxid(raw["sender_id"], room)
                if person is None or normalize(subject) not in {normalize(person.name), normalize(person.display_name)}:
                    continue
                key = "member:" + raw["sender_id"]
                names = (person.name, person.display_name)
                authority = "程序已对上发言账号和本人绑定，可以核对这位群友的本人资料或原话。"
            else:
                continue
            source = {"消息编号": source_id, "正文": raw["text"][:2400], "类型": evidence_kind,
                      "时间": raw["received_at"], "发言身份": authority}
            actual_day = raw["received_at"][:10]
            observed = actual_day
            if kind == "状态":
                on = on or actual_day
        if (kind != "限定事实" and (not on or not observed)) or (on and observed and observed < on):
            continue
        claim = {"范围": scope, "对象": subject, "关系": relation, "事实": statement, "类别": kind,
                 "生效日期": on, "资料确认至": observed}
        if kind == "限定事实":
            claim["适用范围"] = applies_to
        check = {"instructions": "这份实际来源是否直接支持本次正在纠正的事实和它的适用范围？已知的日期请核对。"
                                 "如果程序已确认发言人的身份和权限，就直接用该身份读原话，不需要另找任命证明或外部佐证。",
                 "true": "公开的一手来源直接说明在指定范围内成立。限定事实的具体版本、版次或条件就是有效范围，日期留空也可核实。"
                         "本群管理员明确把群活动的方案定下来、本人的直接偏好、对应发言原话，都由该来源直接确认。",
                 "false": "仅有转述、传闻、猜测、计划选项、意向、问题或答非所问，或把事实扩展到了原文没支持的日期、范围或版本。"
                          "会随时间变化的现状不能只靠旧简介；发言证据只证明原话。"}
        try:
            yes, = judge.noul({"本次纠正": question, "拟记录事实": claim, "证据": source}, [check], timeout=3)
        except (JevError, OSError, ValueError, TimeoutError):
            continue
        ctx.logger.info("纠错证据核对：消息=%s 范围=%s 证据分=%.2f", snapshot.anchor_id, scope, yes)
        if yes < 0.80:
            continue
        same = next((fact for fact in (*matches.facts, *matches.refresh)
                     if fact.scope == scope and fact.kind == kind and fact.relation == normalize(relation)
                     and normalize(subject) in {normalize(name) for name in fact.names}), None)
        if same is not None:
            key, subject = same.subject_key, same.subject
            names = (*same.names, *names)
        fact = dict(scope=scope, subject_key=key, subject=subject, relation=normalize(relation), kind=kind,
                    statement=statement, names=tuple(dict.fromkeys(names)), phrases=_strings(row.get("常见问法"), 6),
                    effective_on=on, observed_on=observed, source=source)
        with guard():
            number = store.record(room, fact, snapshot.anchor_id)
        if number is not None:
            confirmed.append(number)
            ctx.logger.info("已核实纠错入列：消息=%s 条目=%s 范围=%s", snapshot.anchor_id, number, scope)
    return tuple(dict.fromkeys(confirmed))
