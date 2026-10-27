"""主动发言时机判断（Jev）离线评测。

子命令：
  sample        只读 .runtime/member_data.sqlite3，按类别分层切出片段，写 .runtime/proactive_eval/dataset.json
  show          紧凑打印片段（便于人工预标），可按 --offset/--limit 分页
  apply-labels  把 labels.json（{id: [speak|silent, 理由]}）并入 dataset，并按标签分层划分 dev/holdout（30%）
  run           对片段调用 Jev（并发≤2、超时 5 秒），请求/响应存 .runtime/proactive_eval/<时间戳>_<变体>/
  report        汇总一个或多个 run 目录：误发言率、召回率、p50/p90 耗时，并扫阈值 0.50–0.95

数据库只读（URI mode=ro）；真实聊天只落在 .runtime（已 gitignore）。
API key 从 llm_config.json 读取，只放进请求头，不打印、不写入结果文件。
"""

import argparse
import concurrent.futures
import datetime as dt
import json
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / ".runtime" / "member_data.sqlite3"
EVAL_DIR = ROOT / ".runtime" / "proactive_eval"
DATASET = EVAL_DIR / "dataset.json"
LABELS = EVAL_DIR / "labels.json"
CONFIG = ROOT / "llm_config.json"

WINDOW = 15
LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))
PERSONA = "熟人损友：二十多岁、口语自然、懂群梗、能适度调侃；不是客服，不抢话，不刷存在感。"

# —— 片段类别判定用的通用关键词（不含任何真实聊天内容）——
MENTION_RE = re.compile(r"(?<![a-z])(ai|bot)(?![a-z])|机器人|人工智能", re.I)
QUESTION_RE = re.compile(r"[?？]\s*$|(吗|呢|么|啥|谁|几点|怎么|为什么|哪)\s*[?？]?\s*$")
TRANSACT_RE = re.compile(r"^(收到|好的?|ok|okk?|嗯+|行|可以|明白|了解|1|＋1|\+1)[!！。.~]*$|开会|会议|链接|https?://|文件|截止|报名|签到", re.I)
SENSITIVE_RE = re.compile(r"傻[逼b比]|滚|闭嘴|有病|吵|骂|妈的|tm|nm|政治|吵架|恶心|别逼", re.I)
CHAIN_RE = re.compile(r"接龙|^\d+[.、]|^[+＋]1$")


def open_readonly(path):
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def parse_time(value):
    value = value.replace("Z", "+00:00")
    t = dt.datetime.fromisoformat(value)
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t


def percentile(values, fraction):
    if not values:
        return None
    s = sorted(values)
    k = min(len(s) - 1, max(0, int(round(fraction * (len(s) - 1)))))
    return s[k]


BOT_AT_ALIASES = ("@bot",)  # 群友常用的艾特别称，同样走艾特回复路径


def is_bot_mention(text, bot_name):
    lowered = text.lower()
    return f"@{bot_name}" in text or any(alias in lowered for alias in BOT_AT_ALIASES)


def mentions_bot_unaddressed(text, bot_name):
    """提到机器人但没有艾特。"""
    if is_bot_mention(text, bot_name):
        return False
    return bool(MENTION_RE.search(text)) or (bot_name in text)


def anonymize_text(text, bot_name):
    text = text.replace(f"@{bot_name}", "@机器人").replace(bot_name, "机器人")
    text = re.sub(r"@[^\s @]{1,16}", "@群友", text)
    return text.replace(" ", " ")


# ——————————————————————— sample ———————————————————————

def load_rows(db, room):
    conn = open_readonly(db)
    try:
        if room is None:
            room = conn.execute("select room from messages group by room order by count(*) desc limit 1").fetchone()[0]
        rows = conn.execute(
            "select id, sender_id, text, received_at, is_self from messages where room=? order by received_at, id",
            (room,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"id": r[0], "sender": r[1], "text": r[2] or "", "t": parse_time(r[3]), "is_self": bool(r[4])}
        for r in rows
    ]


def categorize(rows, i, bot_name):
    """返回端点 i 命中的类别集合。"""
    cur = rows[i]
    text = cur["text"].strip()
    window = rows[max(0, i - WINDOW + 1): i + 1]
    cats = set()
    if mentions_bot_unaddressed(text, bot_name):
        cats.add("mention_no_at")
    if QUESTION_RE.search(text) and len(text) >= 4:
        cats.add("question")
    if TRANSACT_RE.search(text):
        cats.add("transactional")
    bot_idx = [k for k, r in enumerate(window) if r["is_self"]]
    if bot_idx and (len(window) - 1 - bot_idx[-1] <= 3 or (cur["t"] - window[bot_idx[-1]]["t"]).total_seconds() < 120):
        cats.add("bot_recent")
    local = cur["t"].astimezone(LOCAL_TZ)
    gap = (cur["t"] - rows[i - 1]["t"]).total_seconds() if i > 0 else 1e9
    if (local.hour < 6 or local.hour >= 23) and gap > 600:
        cats.add("late_quiet")
    recent = [r for r in window if (cur["t"] - r["t"]).total_seconds() <= 180]
    if len(recent) >= 10 and len({r["sender"] for r in recent}) >= 3:
        cats.add("busy_chat")
    if SENSITIVE_RE.search(text) or sum(1 for r in window[-5:] if SENSITIVE_RE.search(r["text"])) >= 2:
        cats.add("sensitive")
    last5 = [r["text"].strip() for r in window[-5:]]
    if len(last5) == 5 and (len(set(last5)) <= 2 or sum(1 for t in last5 if CHAIN_RE.search(t)) >= 3):
        cats.add("chain_spam")
    return cats


def build_snippet(rows, i, bot_name, cats):
    cur = rows[i]
    window = rows[max(0, i - WINDOW + 1): i + 1]
    alias = {}
    lines = []
    for r in window:
        if r["is_self"]:
            who = "机器人"
        else:
            alias.setdefault(r["sender"], f"P{len(alias) + 1}")
            who = alias[r["sender"]]
        lines.append({
            "who": who,
            "text": anonymize_text(r["text"], bot_name)[:300],
            "seconds_before": int((cur["t"] - r["t"]).total_seconds()),
        })
    bot_last = None
    for k in range(i, -1, -1):
        if rows[k]["is_self"]:
            bot_last = int((cur["t"] - rows[k]["t"]).total_seconds())
            break
    return {
        "id": f"s{cur['id']}",
        "categories": sorted(cats),
        "local_time": cur["t"].astimezone(LOCAL_TZ).strftime("%H:%M"),
        "bot_last_spoke_seconds_ago": bot_last,
        "someone_mentioned_bot_without_at": any(
            mentions_bot_unaddressed(r["text"], bot_name) for r in window[-3:] if not r["is_self"]
        ),
        "messages": lines,
        "label": None,
        "reason": None,
        "split": None,
    }


def cmd_sample(args):
    rows = load_rows(args.db, args.room)
    rng = random.Random(args.seed)
    by_cat = {}
    for i in range(WINDOW, len(rows)):
        r = rows[i]
        if r["is_self"] or not r["text"].strip() or is_bot_mention(r["text"], args.bot_name):
            continue  # 艾特机器人的消息走艾特回复路径，不作为端点
        cats = categorize(rows, i, args.bot_name) or {"ordinary"}
        for c in cats:
            by_cat.setdefault(c, []).append(i)
    chosen, used = [], set()
    existing = []
    if args.append and DATASET.exists():
        # 追加模式：保留已标注片段（剔除终点其实是艾特机器人的），只给更可能该说的类别补样
        index_of = {f"s{r['id']}": k for k, r in enumerate(rows)}
        for s in load_dataset()["snippets"]:
            k = index_of.get(s["id"])
            if k is None or is_bot_mention(rows[k]["text"], args.bot_name):
                continue
            existing.append(s)
            used.add(k)
    cats_order = sorted(by_cat, key=lambda c: len(by_cat[c]))  # 稀有类别先挑
    for c in cats_order:
        pool = by_cat[c][:]
        rng.shuffle(pool)
        quota = args.ordinary if c == "ordinary" else args.per_category
        if c in ("mention_no_at", "question"):
            quota = args.per_category + args.extra_speak_candidates  # 这两类更可能该说，多采以稳定召回率估计
        if args.append:
            quota = args.extra_speak_candidates if c in ("mention_no_at", "question") else 0
        got = 0
        for i in pool:
            if got >= quota:
                break
            if any(abs(i - j) < 6 for j in used):  # 片段之间留间隔，避免高度重叠
                continue
            used.add(i)
            chosen.append(i)
            got += 1
    chosen.sort()
    snippets = [build_snippet(rows, i, args.bot_name, categorize(rows, i, args.bot_name) or {"ordinary"}) for i in chosen]
    if args.append:
        for s in snippets:
            s["batch"] = 2
        snippets = sorted(existing + snippets, key=lambda s: int(s["id"][1:]))
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    DATASET.write_text(json.dumps({"window": WINDOW, "persona": PERSONA, "snippets": snippets},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
    counts = {}
    for s in snippets:
        for c in s["categories"]:
            counts[c] = counts.get(c, 0) + 1
    print(f"片段 {len(snippets)} 个 -> {DATASET}")
    print("类别覆盖：", json.dumps(counts, ensure_ascii=False))


def load_dataset():
    return json.loads(DATASET.read_text(encoding="utf-8"))


def cmd_show(args):
    data = load_dataset()
    snippets = data["snippets"]
    if args.batch:
        snippets = [s for s in snippets if s.get("batch") == args.batch]
    for s in snippets[args.offset: args.offset + args.limit]:
        print(f"## {s['id']} {','.join(s['categories'])} t={s['local_time']} bot={s['bot_last_spoke_seconds_ago']}s")
        for m in s["messages"][-args.lines:]:
            print(f"  -{m['seconds_before']}s {m['who']}: {m['text'][:args.width]}")


def cmd_apply_labels(args):
    data = load_dataset()
    labels = json.loads(LABELS.read_text(encoding="utf-8"))
    rng = random.Random(args.seed)
    groups = {}
    for s in data["snippets"]:
        if s["id"] in labels:
            s["label"], s["reason"] = labels[s["id"]][0], labels[s["id"]][1]
        groups.setdefault(s["label"], []).append(s)
    for label, group in groups.items():
        group = sorted(group, key=lambda s: s["id"])
        rng.shuffle(group)
        n_hold = round(len(group) * 0.3)
        for k, s in enumerate(group):
            s["split"] = "holdout" if k < n_hold else "dev"
    DATASET.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    stat = {}
    for s in data["snippets"]:
        key = f"{s['label']}/{s['split']}"
        stat[key] = stat.get(key, 0) + 1
    print(json.dumps(stat, ensure_ascii=False))


# ——————————————————————— 问法变体 ———————————————————————

def describe_gap(seconds):
    if seconds is None:
        return "很久没说过话"
    if seconds < 60:
        return f"{seconds} 秒前"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    return f"{seconds // 3600} 小时前"


def state_basic(s):
    return {
        "群聊最近消息（从旧到新，最后一条是刚发的）": [
            f"[{m['seconds_before']}秒前] {m['who']}: {m['text']}" for m in s["messages"]
        ],
        "机器人上次发言": describe_gap(s["bot_last_spoke_seconds_ago"]),
        "最近是否有人提到机器人（未艾特）": s["someone_mentioned_bot_without_at"],
        "当前时间": s["local_time"],
        "机器人人格": PERSONA,
    }


SILENT_RULES = (
    "真人群聊里大约 87% 的时刻应该保持沉默，默认不说话。以下情况都应沉默："
    "事务性消息（收到、好的、开会通知、链接、文件、报名）；两三个人正在你来我往地对话，插话会打断；"
    "机器人刚说过话且没人接它的话；话题与机器人无关、它说不出有价值或好笑的内容；"
    "争吵、敏感或私人话题；刷屏、接龙、复读；深夜冷清时没人需要回应。"
)
SPEAK_RULES = (
    "只有在这些情况插一句才自然且受欢迎：有人提到机器人（ai/bot/机器人）像是在叫它或议论它；"
    "有人问了一个大家都能答的问题，一段时间没人回，机器人能简短回答；"
    "大家在玩一个梗、气氛热闹，机器人接一句不显突兀。"
)


def variant_choice_basic(s):
    return {
        "q0": {
            "type": "choice",
            "instructions": "你是群里的一个群友（机器人）。根据最近的聊天，判断机器人现在该不该主动插一句话（没人艾特它）。",
            "criteria": {
                "reply": "现在插一句话自然且受欢迎",
                "wait": "可能有机会，但现在先等等看",
                "skip": "现在不该说话",
            },
        }
    }, state_basic(s)


def variant_choice_strict(s):
    return {
        "q0": {
            "type": "choice",
            "instructions": (
                "你在判断群里的机器人（一个群友，没人艾特它）此刻要不要主动插一句。"
                "重点看最后几条消息和机器人上次发言时间。" + SILENT_RULES + SPEAK_RULES
                + "拿不准就选 wait 或 skip。"
            ),
            "criteria": {
                "reply": "此刻插一句，真人群友会觉得自然、有趣或有帮助，而不是打扰",
                "wait": "话题可能和机器人有关，但现在插话还早或时机不对",
                "skip": "这一刻与机器人无关，或插话会显得突兀、刷存在感、打断别人",
            },
        }
    }, state_basic(s)


def variant_noul_strict(s):
    return {
        "q0": {
            "type": "noul",
            "instructions": (
                "判断群里的机器人（一个群友，没人艾特它）此刻主动插一句话，是否自然且受欢迎。"
                + SILENT_RULES + SPEAK_RULES
            ),
            "criteria": {
                "true": "此刻插一句，真人群友会觉得自然、有趣或有帮助",
                "false": "此刻插话会显得突兀、打断别人、刷存在感，或者没必要",
            },
        }
    }, state_basic(s)


def state_focus(s):
    """把最后一条单独拎出来，并给出前文的发言节奏。"""
    msgs = s["messages"]
    last = msgs[-1]
    prev = msgs[:-1]
    speakers = [m["who"] for m in prev[-6:]]
    return {
        "前文（从旧到新）": [f"[{m['seconds_before']}秒前] {m['who']}: {m['text']}" for m in prev],
        "刚刚这条": f"{last['who']}: {last['text']}",
        "最近6条的发言人": speakers,
        "机器人上次发言": describe_gap(s["bot_last_spoke_seconds_ago"]),
        "最近是否有人提到机器人（未艾特）": s["someone_mentioned_bot_without_at"],
        "当前时间": s["local_time"],
        "机器人人格": PERSONA,
    }


def variant_choice_reasons(s):
    """把 reply 拆成具体理由，reply 概率 = 各 reply_* 之和；沉默也拆成具体理由，避免一个笼统选项吸走概率。"""
    return {
        "q0": {
            "type": "choice",
            "instructions": (
                "群里的机器人是一个群友，此刻没人艾特它。看“刚刚这条”和前文，选出此刻最贴切的一项。"
                "真人群聊里大约 87% 的时刻都应沉默，拿不准时选沉默类选项。"
            ),
            "criteria": {
                "reply_called": "有人在叫机器人、问机器人或议论机器人（说到 ai/bot/机器人），回应一句很自然",
                "reply_unanswered": "有人问了个问题，过了一阵没人回，机器人能简短答上",
                "reply_banter": "大家在热闹地玩梗，机器人接一句好笑的话不突兀",
                "silent_transaction": "事务性消息：收到、好的、通知、链接、文件、报名",
                "silent_conversation": "别人之间正在对话或问答，插话会打断",
                "silent_bot_just_spoke": "机器人刚说过话，没人接它，再说就是刷存在感",
                "silent_irrelevant": "话题和机器人无关、它说不出有价值的东西，或是争吵、敏感、刷屏接龙、深夜冷清",
            },
        }
    }, state_focus(s)


VARIANTS = {
    "choice_basic": variant_choice_basic,
    "choice_strict": variant_choice_strict,
    "noul_strict": variant_noul_strict,
    "choice_reasons": variant_choice_reasons,
}


def reply_probability(answer):
    if answer.get("type") == "noul":
        return float(answer["noul"])
    probs = answer.get("probabilities", {})
    return float(sum(v for k, v in probs.items() if k == "reply" or k.startswith("reply_")))


# ——————————————————————— run ———————————————————————

def load_endpoint():
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    return cfg["base_url"].rstrip("/") + "/systemone", cfg["api_key"]


def call_jev(url, key, body, timeout):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return payload, time.perf_counter() - start, None
    except urllib.error.HTTPError as exc:
        return None, time.perf_counter() - start, f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}"
    except Exception as exc:  # 超时、连接错误
        return None, time.perf_counter() - start, f"{type(exc).__name__}: {exc}"[:300]


def cmd_run(args):
    data = load_dataset()
    snippets = [s for s in data["snippets"] if s["label"] in ("speak", "silent")]
    if args.split != "all":
        snippets = [s for s in snippets if s["split"] == args.split]
    if args.limit:
        snippets = snippets[: args.limit]
    url, key = load_endpoint()
    build = VARIANTS[args.prompt_variant]
    out = EVAL_DIR / f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}_{args.prompt_variant}_{args.split}"
    out.mkdir(parents=True, exist_ok=True)

    def one(s):
        questions, state = build(s)
        body = {"model": args.model, "state": state, "questions": questions}
        payload, latency, error = call_jev(url, key, body, args.timeout)
        prob = None
        if payload and "q0" in payload.get("answers", {}):
            prob = reply_probability(payload["answers"]["q0"])
        return {"id": s["id"], "label": s["label"], "split": s["split"], "categories": s["categories"],
                "reply_prob": prob, "latency": round(latency, 3), "error": error,
                "request": body, "response": payload}

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(2, args.concurrency)) as pool:
        for r in pool.map(one, snippets):
            results.append(r)
    with (out / "results.jsonl").open("w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    (out / "meta.json").write_text(json.dumps({"variant": args.prompt_variant, "split": args.split,
                                               "model": args.model, "count": len(results)},
                                              ensure_ascii=False, indent=1), encoding="utf-8")
    errors = sum(1 for r in results if r["error"])
    print(f"{len(results)} 次调用，失败 {errors} -> {out}")
    if errors:
        print("首个错误：", next(r["error"] for r in results if r["error"])[:200])
    summarize(results, args.prompt_variant)


# ——————————————————————— report ———————————————————————

THRESHOLDS = [round(0.5 + 0.05 * k, 2) for k in range(10)]
# 验收线与 .spec/proactive-chat/spec.md 一致：误发言率 ≤5%、召回 ≥60%。
MAX_FALSE_SPEAK = 0.05
MIN_RECALL = 0.60


def passes(fpr, rec):
    return fpr is not None and rec is not None and fpr <= MAX_FALSE_SPEAK and rec >= MIN_RECALL


def rates(results, threshold):
    silent = [r for r in results if r["label"] == "silent"]
    speak = [r for r in results if r["label"] == "speak"]
    fp = sum(1 for r in silent if (r["reply_prob"] or 0) >= threshold)
    tp = sum(1 for r in speak if (r["reply_prob"] or 0) >= threshold)
    return (fp / len(silent) if silent else None, tp / len(speak) if speak else None, len(silent), len(speak))


def summarize(results, name):
    lat = [r["latency"] for r in results]
    print(f"[{name}] n={len(results)} p50={percentile(lat, 0.5):.2f}s p90={percentile(lat, 0.9):.2f}s "
          f"错误={sum(1 for r in results if r['error'])}")
    print("  阈值  误发言率  召回率")
    for t in THRESHOLDS:
        fpr, rec, ns, nk = rates(results, t)
        ok = "  达标" if passes(fpr, rec) else ""
        print(f"  {t:.2f}  {fpr:7.1%}  {rec:6.1%}{ok}")
    print(f"  (silent={ns}, speak={nk})")


def cmd_report(args):
    for d in args.run_dirs:
        path = Path(d)
        results = [json.loads(line) for line in (path / "results.jsonl").read_text(encoding="utf-8").splitlines() if line]
        if args.split != "all":
            results = [r for r in results if r["split"] == args.split]
        summarize(results, path.name)
        if args.errors:
            t = args.threshold
            bad = [r for r in results if (r["label"] == "silent") == ((r["reply_prob"] or 0) >= t)]
            for r in bad[: args.errors]:
                last = r["request"]["state"]
                print(f"  x {r['id']} {r['label']} p={r['reply_prob']:.2f} {','.join(r['categories'])}")


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--db", default=DEFAULT_DB)
    s.add_argument("--room", default=None)
    s.add_argument("--bot-name", required=True, help="机器人群昵称（用于识别艾特与匿名化）")
    s.add_argument("--per-category", type=int, default=16)
    s.add_argument("--ordinary", type=int, default=24)
    s.add_argument("--extra-speak-candidates", type=int, default=14)
    s.add_argument("--append", action="store_true", help="保留现有片段，只补采 mention_no_at/question 类")
    s.add_argument("--seed", type=int, default=7)
    sh = sub.add_parser("show")
    sh.add_argument("--offset", type=int, default=0)
    sh.add_argument("--limit", type=int, default=20)
    sh.add_argument("--lines", type=int, default=6)
    sh.add_argument("--width", type=int, default=40)
    sh.add_argument("--batch", type=int, default=0)
    a = sub.add_parser("apply-labels")
    a.add_argument("--seed", type=int, default=11)
    r = sub.add_parser("run")
    r.add_argument("--prompt-variant", choices=sorted(VARIANTS), required=True)
    r.add_argument("--split", choices=["dev", "holdout", "all"], default="dev")
    r.add_argument("--model", default="typesafe/jev-latest")
    r.add_argument("--timeout", type=float, default=5.0)
    r.add_argument("--concurrency", type=int, default=2)
    r.add_argument("--limit", type=int, default=0)
    rp = sub.add_parser("report")
    rp.add_argument("run_dirs", nargs="+")
    rp.add_argument("--split", choices=["dev", "holdout", "all"], default="all")
    rp.add_argument("--errors", type=int, default=0, help="列出前 N 个判错的片段 id")
    rp.add_argument("--threshold", type=float, default=0.7)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    {"sample": cmd_sample, "show": cmd_show, "apply-labels": cmd_apply_labels,
     "run": cmd_run, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
