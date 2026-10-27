"""离线回放：用最近的真实非详细艾特提问，对比旧/新 SYSTEM 的回复长度、空行分段和套路短语命中率。

不连微信、不发送任何消息；只读打开 SQLite，向本机 llm_config.json 配置的模型各发一次请求（每条每版一次，
不重试），结果和统计只写到 .runtime/humanlike-replay/<时间>/（已被 .gitignore 忽略，里面有真实群聊原文，不要提交）。
控制台只输出统计数字，不输出群聊原文，也不输出 api_key。

    python tools/replay_mention_length.py --dry-run            # 只统计选样与提示词，不调用模型
    python tools/replay_mention_length.py                      # 旧/新各回放最近 50 条
    python tools/replay_mention_length.py --systems new --limit 20

旧 SYSTEM 取自 git 提交 --old-commit（默认 dc52f19，本轮改动前），用 ast 取出赋值表达式后在只含 GROUP_LORE 的
命名空间里求值，不导入旧模块。两版共用同一个 Dialogue 与人格前缀，只有 SYSTEM（和新版才有的“群友说话的样子”示例）不同。
上下文只取库里该提问之前的群聊（发言人匿名为“群友N”），不带知识库，两版条件一致。
"""
from __future__ import annotations

import argparse
import ast
import json
import random
import re
import sqlite3
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from plugins.mention_chat import wants_detail  # noqa: E402
from wxbot_framework.commands import is_control  # noqa: E402
from wxbot_framework.conversation import dialogue as dialogue_module  # noqa: E402
from wxbot_framework.conversation.context_tools import pick_speech_examples  # noqa: E402
from wxbot_framework.conversation.dialogue import AVOID_PHRASES, Dialogue  # noqa: E402
from wxbot_framework.conversation.members import ConversationSnapshot  # noqa: E402
from wxbot_framework.conversation.personality import PersonalityLLM, PersonalityState  # noqa: E402
from wxbot_framework.conversation.utterance import classify_tier, plan_utterance  # noqa: E402
from wxbot_framework.integrations.llm import LLMClient  # noqa: E402
from wxbot_framework.lore import GROUP_LORE  # noqa: E402
from wxbot_framework.messaging.mentions import strip_at_labels  # noqa: E402

DB = ROOT / ".runtime" / "member_data.sqlite3"
CONFIG = ROOT / "llm_config.json"
OUTPUT_ROOT = ROOT / ".runtime" / "humanlike-replay"
BLANK_LINE = re.compile(r"\n[ \t　]*\n")
CONTEXT_ROWS = 25
TARGETS = {"median": 30, "p90": 70, "blank_ratio": 0.05, "avoid_drop": 0.5}


def percentile(values, fraction):
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[max(0, -(-int(fraction * 1000) * len(ordered) // 1000) - 1)]


def old_system(commit):
    """取 commit 里 dialogue.py 的 SYSTEM 字符串（表达式里只允许出现 GROUP_LORE 这个名字）。"""
    source = subprocess.run(["git", "show", f"{commit}:wxbot_framework/conversation/dialogue.py"], cwd=ROOT,
                            check=True, capture_output=True).stdout.decode("utf-8")
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "SYSTEM" for t in node.targets):
            return eval(compile(ast.Expression(node.value), "<old-system>", "eval"),
                        {"__builtins__": {}}, {"GROUP_LORE": GROUP_LORE})
    raise RuntimeError("旧提交里找不到 SYSTEM")


def open_readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def detect_bot_label(db):
    """机器人在 @ 标签里的昵称：库里“@xxx\\u2005”开头的消息里出现最多的那个。"""
    counts = {}
    for (text,) in db.execute("SELECT text FROM messages WHERE bot_generated=0 AND is_self=0 AND text LIKE '@%' "
                              "ORDER BY id DESC LIMIT 600"):
        match = re.match(r"@([^@\r\n ]+) ", text)
        if match:
            counts[match.group(1)] = counts.get(match.group(1), 0) + 1
    return max(counts, key=counts.get) if counts else None


def load_cases(path, limit, bot_label=None):
    """最近 limit 条非详细、非命令、有正文的艾特提问，各带其之前的群聊上下文。返回 (cases, 机器人昵称)。"""
    db = open_readonly(path)
    try:
        bot_label = bot_label or detect_bot_label(db)
        if not bot_label:
            raise RuntimeError("找不到机器人昵称，请用 --bot-label 指定")
        prefix = f"@{bot_label} "
        rows = db.execute("SELECT id,room,sender_id,text,received_at FROM messages "
                          "WHERE bot_generated=0 AND is_self=0 AND text LIKE ? ORDER BY id DESC LIMIT ?",
                          (prefix + "%", limit * 6)).fetchall()
        cases = []
        for row_id, room, sender, text, received_at in rows:
            query = strip_at_labels(text).strip()
            if not query or is_control(text) or wants_detail(query):
                continue
            context = db.execute("SELECT id,sender_id,text,received_at FROM messages WHERE room=? AND id<? "
                                 "AND bot_generated=0 AND is_self=0 ORDER BY id DESC LIMIT ?",
                                 (room, row_id, 120)).fetchall()
            cases.append({"id": row_id, "room": room, "sender": sender, "query": query, "received_at": received_at,
                          "context": list(reversed(context))})
            if len(cases) >= limit:
                break
        return cases, bot_label
    finally:
        db.close()


class RowsMembers:
    """把回放用例里的历史行包成 recent_rows，供 pick_speech_examples 使用（机器人行本就没取）。"""

    def __init__(self, case):
        self.rows = [{"id": row[0], "text": row[2], "bot_generated": 0, "is_self": 0}
                     for row in reversed(case["context"])]

    def recent_rows(self, room, **options):
        return self.rows


def snapshot_for(case):
    names = {}

    def name_of(sender):
        return names.setdefault(sender, "群友" + chr(ord("A") + len(names) % 26) + str(len(names) // 26 or ""))

    speaker = name_of(case["sender"])
    context = case["context"][-CONTEXT_ROWS:]
    group = tuple({"received_at": received_at, "speaker": sender, "speaker_name": name_of(sender),
                   "text": strip_at_labels(text) or text} for _, sender, text, received_at in context)
    day = case["received_at"][:10]
    return ConversationSnapshot(case["room"], day, 0, speaker, (), case["query"], group)


def generate(case, system_text, with_examples, client, seed):
    started = time.monotonic()
    llm = PersonalityLLM(client, PersonalityState(case["room"]), current=lambda: True)
    examples = pick_speech_examples(RowsMembers(case), case["room"], rng=random.Random(seed + case["id"])) \
        if with_examples else []
    try:
        reply = Dialogue(llm).reply(snapshot_for(case), examples=examples, system=system_text)
        error = None
    except Exception as exc:  # 只记录类型，不带可能含密钥或原文的消息。
        reply, error = None, type(exc).__name__
    return {"id": case["id"], "reply": reply, "error": error, "seconds": round(time.monotonic() - started, 2)}


def stats(results, cases):
    by_id = {case["id"]: case for case in cases}
    replies = [item["reply"] for item in results if item["reply"]]
    lengths = [len(reply) for reply in replies]
    sent = []
    for item in results:
        if item["reply"]:
            tier = classify_tier(by_id[item["id"]]["query"])
            sent.extend(len(part) for part in plan_utterance(item["reply"], tier, byte_budget=1000).parts)
    if not lengths:
        return {"count": 0, "failed": len(results)}
    hits = sum(1 for reply in replies if any(phrase in reply for phrase in AVOID_PHRASES))
    blank = sum(1 for reply in replies if BLANK_LINE.search(reply))
    return {"count": len(replies), "failed": len(results) - len(replies),
            "median": statistics.median(lengths), "p90": percentile(lengths, 0.9), "max": max(lengths),
            "blank_ratio": round(blank / len(replies), 4), "avoid_hit_ratio": round(hits / len(replies), 4),
            "sent_messages": len(sent), "per_message_median": statistics.median(sent) if sent else None,
            "per_message_p90": percentile(sent, 0.9) if sent else None}


def verdict(old, new):
    checks = {}
    if new.get("count"):
        checks["median<=30"] = new["median"] <= TARGETS["median"]
        checks["p90<=70"] = new["p90"] <= TARGETS["p90"]
        checks["blank<=5%"] = new["blank_ratio"] <= TARGETS["blank_ratio"]
        if old and old.get("avoid_hit_ratio"):
            checks["avoid_hit_down_over_half"] = new["avoid_hit_ratio"] <= old["avoid_hit_ratio"] * (1 - TARGETS["avoid_drop"])
    return checks


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(DB))
    parser.add_argument("--config", default=str(CONFIG), help="大模型配置文件（只读，不输出内容）")
    parser.add_argument("--limit", type=int, default=50, help="回放条数（默认 50）")
    parser.add_argument("--systems", choices=("old", "new", "both"), default="both")
    parser.add_argument("--old-commit", default="dc52f19", help="取旧 SYSTEM 的提交（默认本轮改动前）")
    parser.add_argument("--bot-label", help="机器人在 @ 标签里的昵称（默认从库里推断）")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--seed", type=int, default=20261027, help="示例抽样种子")
    parser.add_argument("--dry-run", action="store_true", help="只选样和渲染，不调用模型")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 200 or not 1 <= args.workers <= 8:
        print("参数不合法：limit 1—200，workers 1—8", file=sys.stderr)
        return 2
    if not Path(args.db).exists():
        print("找不到数据库文件", file=sys.stderr)
        return 2
    cases, label = load_cases(args.db, args.limit, args.bot_label)
    print(f"选样：{len(cases)} 条非详细艾特提问（目标 {args.limit}）；每条取其之前最多 {CONTEXT_ROWS} 条群聊作上下文")
    versions = {"old": (old_system(args.old_commit), False), "new": (dialogue_module.SYSTEM, True)}
    wanted = ["old", "new"] if args.systems == "both" else [args.systems]
    if args.dry_run:
        for name in wanted:
            print(f"{name} SYSTEM 长度 {len(versions[name][0])} 字")
        return 0
    client = LLMClient.from_file(args.config)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = OUTPUT_ROOT / stamp
    out.mkdir(parents=True, exist_ok=True)
    summary = {"time": stamp, "cases": len(cases), "old_commit": args.old_commit, "targets": TARGETS, "stats": {}}
    all_results = {}
    for name in wanted:
        system_text, with_examples = versions[name]
        with ThreadPoolExecutor(args.workers) as pool:
            results = list(pool.map(lambda case: generate(case, system_text, with_examples, client, args.seed), cases))
        all_results[name] = results
        summary["stats"][name] = stats(results, cases)
        print(f"[{name}] {json.dumps(summary['stats'][name], ensure_ascii=False)}")
    summary["checks"] = verdict(summary["stats"].get("old"), summary["stats"].get("new", {})) if "new" in wanted else {}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with (out / "replies.jsonl").open("w", encoding="utf-8") as handle:
        for name, results in all_results.items():
            for item in results:
                handle.write(json.dumps({"version": name, **item}, ensure_ascii=False) + "\n")
    print("达标检查：" + json.dumps(summary["checks"], ensure_ascii=False))
    print(f"结果目录：{out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
