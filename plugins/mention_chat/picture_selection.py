"""根据各个入镜对象的原话选择素材，图片身份、顺序和可用性由程序核实。"""
import re
import time

from wxbot_framework.integrations.reply_budget import remaining
from wxbot_framework.recent_images import READY, RECENT_IMAGES


SOURCES = {
    "previous": "按当前话语指向的上图、刚发那张、或依发送人、顺序、编号指定的一张群图画；靠消息位置即可定位。",
    "quote": "使用本条消息在微信实际引用的一张图片。引用的是文字时，按文字提到的素材来源选。",
    "portrait": "使用这个人物自己的已登记形象或档案照。",
    "free": "出镜，但允许自由发挥或没有指定照片。",
    "absent": "不参与画面取材，例如只是署名、受众或被排除的人。",
    "inspect": "在好几张图片中按画面内容挑一张，须查看照片才能分辨。",
    "planning": "需要前文、人物事迹、组合多份来源，或原话不足以确定。",
}
EDIT_BASE = "待修改底图"
CONTINUE_QUOTE = re.compile(r"^(?:请|帮我|给我)?(?:画|来|生成)?(?:一张|个)?(?:后续图|续图|续画|下一幕|接着画)(?:$|[\s，。！？：、,.!?:])")


def _winner(scores):
    return max(scores, key=scores.get)


def _file(record, job, deadline):
    if record is None:
        return ""
    record = RECENT_IMAGES.wait(record, max(0, deadline - time.monotonic()), stopped=job.cancelled)
    return record.path if record.status == READY else ""


def _choose_group_picture(job, judge, label, clause, records, mode, deadline):
    if re.search(r"我(?:刚才|刚刚|刚|之前)?发", clause):
        records = tuple(r for r in records if r.sender == job.message.sender_id)
    if re.search(r"(?:刚发(?:的)?|那|这|我发的)[两二2]张", clause):
        records = records[-2:]
    if not records:
        return "", f"能补发或引用一下用作{label}的那张图吗？"
    if mode == "previous" and re.search(r"(?:上图|上一张(?:图|照片)|刚发的那张)(?:图|照片)?[。！! ]*$", clause):
        sender_specific = re.search(r"(\S+)刚发的那张", clause)
        if not sender_specific or sender_specific[1].endswith(("照", "用", "按", "参考", "看", "我")):
            path = _file(records[-1] if records else None, job, deadline)
            return path, "" if path else f"用作{label}的上一张图没收到，能引用原图吗？"
    if len(records) > 4:
        return "", f"前面有多张图片，能引用要给{label}用的那张吗？"
    options = {}
    for idx, record in enumerate(records, 1):
        who = "请求人" if record.sender == job.message.sender_id else job.ctx.members.display_name(record.sender, job.snapshot.room)
        options[f"I{idx}"] = f"第{idx}张，发送人：{who or '其他群友'}" + ("，请求之前最近的上图" if idx == len(records) else "")
    state = {"限定这份素材的原话": clause, "素材用途": label, "群图按时间从旧到新": options}
    if mode != "inspect":
        choices = {**options, "visual": "要在多张中按实际画面辨认，顺序与发送人不够。",
                   "missing": "指定的编号或发送人在候选中没有匹配。", "unclear": "无法唯一定位。"}
        scores = judge.choice(state, "仅凭原话、发送人及顺序，选出指定的群图。涉及画面筛选时选 visual。", choices, timeout=3)
        selected = _winner(scores)
        if selected in options and scores[selected] >= 0.75:
            path = _file(records[int(selected[1:]) - 1], job, deadline)
            return path, "" if path else f"用作{label}的图还没收到，能引用原图吗？"
        if selected == "missing":
            return "", f"我没找到指定给{label}的那张图，能引用原图吗？"
        if selected == "unclear" and scores["visual"] < 0.25:
            return "", f"想用哪张图作{label}？可以引用那张图。"
    observations = {}
    paths = []
    for key, record in zip(options, records):
        path = _file(record, job, deadline)
        if not path:
            return "", f"用作{label}的候选图还没收齐，能引用要用的那张吗？"
        with job.guard():
            pass
        observations[key] = job.ctx.magpie.analyze_image(path, "描述与这句挑图要求有关的可见特征：" + clause,
                                                         max_chars=500, timeout=remaining(15)).text
        paths.append(path)
    state["逐张读取的画面"] = observations
    questions = [{"instructions": f"根据实际观察，{key} 这张本身是否满足原话的挑图条件？允许多张或零张符合。",
                  "true": "其画面直接支持指定特征。", "false": "不符合或观察不足以确认。"} for key in options]
    scores = judge.noul(state, questions, timeout=3)
    matches = [path for path, score in zip(paths, scores) if score >= 0.70]
    if len(matches) == 1 and sum(score >= 0.4 for score in scores) == 1:
        return matches[0], ""
    return "", f"按画面条件还没法确定用哪张作{label}，能引用你想用的那张吗？"


def _role_sources(job, profiles, request):
    ctx = job.ctx
    judge = ctx.jev
    roles = {name: name for name in profiles.mentioned}
    if "你" in request and profiles.bot:
        bot_name = profiles.bot["name"]
        roles[bot_name] = f"{bot_name}（本群机器人，群友直接对她说的你、你自己）"
    use_quoted_base = job.quoted_image and not job.replacement_role and bool(CONTINUE_QUOTE.match(job.prompt))
    check_edit_base = job.quoted_image and not (use_quoted_base or job.base_reference or job.replacement_role)
    fixed_base = use_quoted_base or bool(job.base_reference)
    if not roles or re.search(r"背景|构图|风格|场景图|底图", request):
        roles["画面参考"] = "画面参考（背景、构图或整体画法，与单个人物的外貌分开）"
    if check_edit_base:
        roles[EDIT_BASE] = EDIT_BASE
    clauses = {f"E{i}": clause for i, clause in enumerate(filter(None, re.split(r"[，。；：、\n]+|\s+", request)), 1)}
    if not clauses or len(clauses) > 60 or len(roles) > 16:
        return None
    quote = job.message.data.get("quote") or {}
    state = {"群友完整指令": request, "可引用的原文片段": clauses,
             "本群机器人": profiles.bot["name"] if profiles.bot else "",
             "引用原话": {"发言人": quote.get("name"), "正文": (quote.get("text") or "")[:1000]} if quote else None,
             "本条微信明确引用的图片": "引用图" if job.quoted_image else "无",
             "群图片从旧到新": [f"I{i+1}" for i in range(len(job.recent_pictures))],
             "待判断的人物或用途": roles,
             "本群已存在的形象照": [p["name"] for p in profiles.portraits.people.values()]}
    questions = {role: (f"聚焦{label}。哪段原话最能决定其素材来源，或表明此人仅被题字提及？",
                        {**clauses, "whole": "需要结合多处或没有单独说明。"}) for role, label in roles.items()}
    questions["任务范围"] = ("群友有没有要求使用图片之外特定的旧对话、人物经历、群梗或群体名单？"
                           "分配哪张照片、核对画面特征，以及按已给出的引用原话确认用谁的档案照，均在当前选图环节处理。",
                          {"direct": "原话已经给出场景，剩下是按指示配图或自由创作。",
                           "plan": "必须检索或推导上述额外的文字资料才能构思成品。"})
    evidence = judge.choices(state, questions, timeout=3)
    if evidence["任务范围"]["plan"] >= 0.55:
        return None
    excerpts = {role: clauses.get(_winner(evidence[role]), request) for role in roles}
    questions = {role: (f"现在只判断{label}的{'画面' if role == '画面参考' else '外貌'}来源。先按其专属证据「{excerpts[role]}」回答，"
                        "并用完整指令和引用原话核对代词。对机器人说的‘你的头像’指她的档案照，引用和转述中的‘你’按原话的对象理解。"
                        "别把其他人物的要求套给这个对象。无论素材有没有到，按话里想要的来源选择。", SOURCES)
                 for role, label in roles.items() if role != EDIT_BASE}
    if check_edit_base:
        questions[EDIT_BASE] = (
            "只判断本条微信引用图的用途。正文是否要求接着引用图画，或修改这张图？正文里的新画面要求是要绘制的内容。",
            {"quote": "沿用引用图续画或修改，例如画它的下一幕、换背景、把另一个人的脸换到图中人物身上。没有点名人物也适用。",
             "absent": "正文要求引用图只作画风、背景、人物外貌等参考来创作新图，或者明确另指定了要修改的图。"})
    answers = judge.choices(state, questions, timeout=3)
    modes = {role: _winner(scores) for role, scores in answers.items() if role != EDIT_BASE}
    if check_edit_base:
        use = answers.get(EDIT_BASE, {})
        fixed_base = use.get("quote", 0) >= 0.55 or (job.edit and use.get("absent", 0) < 0.55)
    if fixed_base and job.required_references < 2 and modes.get("画面参考") in ("quote", "previous", "inspect"):
        modes.pop("画面参考")
    elif (job.quoted_image and not job.replacement_role and job.required_references < 2
          and modes.get("画面参考") in ("previous", "inspect")):
        modes["画面参考"] = "quote"
    if fixed_base:
        modes = {EDIT_BASE: "quote", **modes}
        excerpts.setdefault(EDIT_BASE, job.prompt)
    if any(mode == "planning" or (answers[role][mode] < 0.55 and mode not in ("previous", "inspect"))
           for role, mode in modes.items() if role not in (EDIT_BASE, "画面参考") or mode != "quote"):
        return None
    if all(mode in ("free", "absent") for mode in modes.values()):
        return None
    return {role: (mode, excerpts[role]) for role, mode in modes.items()}


def resolve_materials(job, profiles):
    """返回已确定的群图、档案照与用途；依赖历史或额外构思的任务继续原有规划。"""
    ctx = job.ctx
    request = "\n".join(filter(None, (job.previous_prompt, job.prompt, *job.revisions)))
    bindings = job.material_sources or _role_sources(job, profiles, request)
    if not bindings:
        return None
    job.material_sources = bindings
    paths, names, notes = [], [], []
    deadline = time.monotonic() + 6
    for role, (mode, excerpt) in bindings.items():
        if job.replacement_role == role:
            mode = "quote"
        if mode == "portrait":
            if role == "画面参考":
                job.material_sources = {}
                return None
            names.append(role)
        elif mode in ("quote", "previous", "inspect"):
            if mode == "quote":
                path = job.base_reference if role == EDIT_BASE else ""
                if not path and job.references and not job.missing_reference:
                    path = job.references[0].path
                question = "" if path else (
                    "请补发或引用要修改的底图。" if role == EDIT_BASE else f"请补发或引用用作{role}的原图。")
            else:
                records = job.recent_pictures
                path, question = _choose_group_picture(job, ctx.jev, role, excerpt, records, mode, deadline)
            if question:
                job.material_question = role
                return (), (), "", question
            if path not in paths:
                paths.append(path)
            index = paths.index(path) + 1
            if role == EDIT_BASE:
                job.base_reference = path
                notes.append(f"第{index}张是待修改的底图。在这张图上修改，保留其余画面。")
            else:
                notes.append(f"第{index}张用作{role}的参考。")
    if job.quoted_image and job.references and job.references[0].path not in paths:
        job.material_sources = {}
        return None
    if job.previous_image and not paths:
        job.material_sources = {}
        return None
    picking_one = any(mode == "inspect" for mode, _ in bindings.values()) or re.search(r"[两二2]张(?:图|照片)?(?:中|里)", request)
    if job.required_references == 2 and len(paths) < 2 and not picking_one:
        job.material_sources = {}
        return None
    ctx.logger.info("生图素材已定位：消息=%s 群图=%d 档案照=%d", job.snapshot.anchor_id, len(paths), len(names))
    return tuple(paths), names, "".join(notes) + "\n", ""
