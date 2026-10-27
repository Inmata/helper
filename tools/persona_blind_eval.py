"""人格补充盲评：只读库取 30 个真实聊天场景，基准人格与当前（或指定）人格补充各生成一条回复。

    python tools/persona_blind_eval.py                          # 当前生效的人格补充
    python tools/persona_blind_eval.py --supplement-file x.json # 指定补充（JSON 含 text，或纯文本）

A/B 顺序逐条随机；样本写入 .runtime/persona_eval/<时间戳>/samples.json，标签单独写入 key.json，
评审时只看 samples.json。输出目录在 .runtime 下，不进仓库；控制台只打印计数。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime
from pathlib import Path

from persona_common import ROOT, pick_rooms, readonly_members

from wxbot_framework.commands import is_control  # noqa: E402
from wxbot_framework.conversation.dialogue import Dialogue  # noqa: E402
from wxbot_framework.conversation.members import ConversationSnapshot  # noqa: E402
from wxbot_framework.conversation.personality import PersonalityLLM, PersonalityState  # noqa: E402
from wxbot_framework.integrations.llm import LLMClient  # noqa: E402
from wxbot_framework.messaging.mentions import strip_at_labels  # noqa: E402

CONTEXT_ROWS = 30


def load_supplement(path):
    text = Path(path).read_text(encoding="utf-8-sig").strip()
    try:
        data = json.loads(text)
    except ValueError:
        return text
    return data.get("text", "").strip() if isinstance(data, dict) else ""


def scenarios(members, rooms, count, rng):
    """从各群最近的群友消息里随机抽锚点，锚点之前的消息作上下文，锚点本身作提问。"""
    pool = []
    with members.database.lock:
        db = members.connection()
        for room in rooms:
            rows = db.execute("SELECT id, sender_id, text, received_at FROM messages WHERE account=? AND room=? "
                              "AND is_self=0 AND bot_generated=0 ORDER BY id DESC LIMIT 3000",
                              (members.account, room)).fetchall()
            rows = list(reversed(rows))
            for index, row in enumerate(rows):
                query = strip_at_labels(row["text"] or "").strip()
                if index >= CONTEXT_ROWS and 4 <= len(query) <= 60 and not is_control(row["text"]):
                    pool.append((room, row, rows[index - CONTEXT_ROWS:index]))
    return rng.sample(pool, min(count, len(pool)))


def snapshot_for(room, anchor, context):
    names = {}

    def name_of(sender):
        return names.setdefault(sender, "群友" + chr(ord("A") + len(names) % 26) + str(len(names) // 26 or ""))

    speaker = name_of(anchor["sender_id"])
    group = tuple({"received_at": row["received_at"], "speaker": row["sender_id"],
                   "speaker_name": name_of(row["sender_id"]), "text": strip_at_labels(row["text"]) or row["text"]}
                  for row in context)
    query = strip_at_labels(anchor["text"]).strip()
    return ConversationSnapshot(room, anchor["received_at"][:10], 0, speaker, (), query, group), group, query


def reply(client, state, snapshot):
    llm = PersonalityLLM(client, state, current=lambda: True)
    try:
        return Dialogue(llm).reply(snapshot), None
    except Exception as error:  # 只记录类型，不带可能含密钥或原文的消息。
        return None, type(error).__name__


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=ROOT / "bot_config.json")
    parser.add_argument("--llm-config", default=ROOT / "llm_config.json")
    parser.add_argument("--room")
    parser.add_argument("--supplement-file")
    parser.add_argument("--count", type=int, default=30)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--output", default=ROOT / ".runtime" / "persona_eval")
    args = parser.parse_args(argv)
    members, _ = readonly_members(args.config)
    rooms = [room for _, room in pick_rooms(members, args.room)]
    rng = random.Random(args.seed)
    fixed = load_supplement(args.supplement_file) if args.supplement_file else None
    client = LLMClient.from_file(args.llm_config)
    samples, key, errors = [], {}, 0
    for index, (room, anchor, context) in enumerate(scenarios(members, rooms, args.count, rng), 1):
        current = members.personality(room)
        supplement = fixed if fixed is not None else current.supplement
        if not supplement:
            raise SystemExit("当前没有生效的人格补充，请用 --supplement-file 指定")
        snapshot, group, query = snapshot_for(room, anchor, context)
        replies = {"base": reply(client, PersonalityState(room), snapshot),
                   "persona": reply(client, PersonalityState(room, generation=current.generation or -1,
                                                             supplement=supplement), snapshot)}
        errors += sum(1 for _, error in replies.values() if error)
        order = ["base", "persona"]
        rng.shuffle(order)
        sample_id = f"s{index:02d}"
        samples.append({"id": sample_id, "context": [{"name": row["speaker_name"], "text": row["text"]}
                                                     for row in group[-12:]],
                        "query": query, "A": replies[order[0]][0], "B": replies[order[1]][0]})
        key[sample_id] = {"A": order[0], "B": order[1]}
    folder = Path(args.output) / datetime.now().strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "samples.json").write_text(json.dumps(samples, ensure_ascii=False, indent=2), encoding="utf-8")
    (folder / "key.json").write_text(json.dumps(key, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"samples={len(samples)} errors={errors} output={folder}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
