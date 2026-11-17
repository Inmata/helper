"""按当前话题定向读取成员原话；身份和消息范围由程序约束。"""
from datetime import timedelta
import logging

logger = logging.getLogger(__name__)
TARGET = {
    "instructions": "只判断当前话语实质谈论的人。前文只用于解析当前的代词，旧话题里出现过这个人不算。覆盖接话、近况、偏好、推荐、比较、评价和玩笑，明确比较时多个对象可同时为真。",
    "true": "当前话题涉及该成员本人，或者明确承接他的行为、原话、偏好。",
    "false": "只在旧上下文出现、与当前话题无关、指代不清，或谈的是外部同名人物。",
}
RELEVANT = {
    "instructions": "这条本人原话是否有助于接住当前话题？保留具体偏好、相关行为、旧新变化；结合时间和说话者，不把转述别人的爱好当成本人的爱好。",
    "true": "直接相关或能补充必要前情，包括较早但可用于比较变化的原话。",
    "false": "无关话题、空确认或没有帮助的重复。",
}
PERSON_GUIDANCE = ("聊到成员时优先使用【相关成员原话】回答当前话题。先归纳有依据的具体内容，再自然接话。"
                   "区别本人要求、他人描述和实际画面；保留原话时间，不凭少量请求断定长期性格。"
                   "需要归纳的问答可用80—180字，短接话仍简短。先给贴合问题的判断，选两三个相关特点即可，"
                   "不要逐条复读敏感生图描述，不要扩写未出现的动作、肢体或衣服细节。"
                   "用户问群内表现时就评价其群内表现，不主动扩展到现实背景或全面人格。"
                   "不要用‘缺现实资料、不了解全部、摸不清本人’占篇幅；不相关的旧记忆与旧梗不用凑进来。"
                   "优先概括本人原话中的可观察特点，不主动扩写生图是否成功、审核先后、具体次数或动机。"
                   "资料只是节选，不能说他总共只提了几次；转述与玩笑不得写成本人现实经历。"
                   "任务进度只按明确的任务阶段回答，没有该阶段就不说图是否生成或发出。"
                   "直接回答当前话题，不重提你上次怎么回答、是否漏看或答得敷衍，除非用户正在要求纠错。"
                   "资料不足时说清缺哪部分，不用泛泛调侃替代答案。")


def person_context(members, knowledge, jev, room, question, *, history=(), before_id=None, quote=None, sender_id=None):
    candidates = knowledge.conversation_people(room)
    if not candidates:
        return []
    recent = [{"name": r.get("speaker_name", ""), "text": r["text"][:300]}
              for r in history[-8:]]
    state = {"当前话语": question, "前文": recent, "引用": quote}
    explicit = [p for p in candidates if any(len(name) >= 2 and name in question
                for name in (p['name'], *p['aliases']))]
    selected = list(explicit)
    # 每道题的目标固定，模型只能判断已存在的本群人物，不能返回新的身份。
    for start in range(0, len(candidates), 60):
        if explicit:
            if sender_id and "我" in question and any(word in question for word in ("谁", "比", "和", "跟")):
                selected.extend(p for p in candidates if p['wxid'] == sender_id and p not in selected)
            break
        batch = candidates[start:start + 60]
        questions = [{**TARGET, "instructions": TARGET["instructions"] +
                      f" 本题目标：{p['name']}；已确认别名：{'、'.join(p['aliases'])}。"} for p in batch]
        try:
            scores = jev.noul(state, questions, timeout=2) if jev is not None else [0] * len(batch)
        except Exception as error:
            logger.info("人物话题判断不可用：%s", type(error).__name__)
            scores = [0] * len(batch)
        for person, score in zip(batch, scores):
            if score >= 0.85:
                selected.append(person)
    if not selected:
        # 明确且唯一的名字仍能读取；模糊代词留给回复询问，不猜身份。
        selected = [p for p in candidates if any(name and name in question
                    for name in (p['name'], *p['aliases']))]
    if not selected:
        return []
    per_person = max(1, 60 // len(selected))
    rows = []
    since = members.now() - timedelta(days=7)
    for person in selected:
        source = members.recent_rows(room, sender_id=person['wxid'], before_id=before_id,
                                     since=since, limit=per_person, context=True, include_commands=True)
        for row in source:
            if row.get('context_kind') == 'forwarded':
                continue
            rows.append({"id": row['id'], "name": person['name'], "time": str(row['received_at']),
                         "text": row['text']})
    if not rows:
        return [{"name": p['name'], "note": "近7天可用上下文中没有找到本人原话"} for p in selected]
    questions = [{**RELEVANT, "instructions": RELEVANT['instructions'] +
                  f" 本题原话：{r['name']}在{r['time']}说：{r['text'][:600]}"}
                 for r in rows]
    try:
        scores = jev.noul({**state, "本人原话": rows}, questions, timeout=3) if jev is not None else [1] * len(rows)
    except Exception as error:
        logger.info("人物选材判断不可用：%s", type(error).__name__)
        scores = [1] * len(rows)
    ranked = sorted(zip(rows, scores), key=lambda item: item[1], reverse=True)
    picked, size = [], 0
    seen = set()
    for row, score in ranked:
        key = row['name'], row['text']
        if score < 0.5 or key in seen:
            continue
        seen.add(key)
        if size + len(row['text']) > 6000:
            continue
        picked.append(row)
        size += len(row['text'])
        if len(picked) == 12:
            break
    logger.info("人物上下文：targets=%s source_ids=%s", [p['id'] for p in selected], [r['id'] for r in picked])
    return sorted(picked, key=lambda row: row['id'])
