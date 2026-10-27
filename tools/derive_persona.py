"""人格补充归纳的试运行：只读库，调用真实模型与 Jev，打印草案统计和筛查结果，不写库。

    python tools/derive_persona.py --dry-run                 # 所有配置群
    python tools/derive_persona.py --dry-run --room <群ID>   # 指定群
    python tools/derive_persona.py --dry-run --show-text     # 额外打印保留下来的补充文本

控制台只输出计数、丢弃原因分类；不输出群 ID、聊天原文、成员名或 api_key。
丢弃条目可能含成员名或冒犯内容，默认不打印；--show-dropped 仅供本机自查。
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter

from persona_common import ROOT, pick_rooms, readonly_members

from wxbot_framework.integrations.jev import JevClient  # noqa: E402
from wxbot_framework.integrations.llm import LLMClient  # noqa: E402
from wxbot_framework.memory.persona import CATEGORIES, PersonaDeriver  # noqa: E402


def current_version(members, room):
    """只读库可能还没迁移出新列（机器人重启后才迁移）；读不到就直接查生效指针。"""
    try:
        return members.personality(room).generation
    except Exception:
        row = members.connection().execute("SELECT generation FROM room_personalities WHERE account=? AND room=?",
                                           (members.account, room)).fetchone()
        return row[0] if row else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", required=True, help="必须指定：本工具从不写库")
    parser.add_argument("--config", default=ROOT / "bot_config.json")
    parser.add_argument("--llm-config", default=ROOT / "llm_config.json")
    parser.add_argument("--room")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--show-text", action="store_true")
    parser.add_argument("--show-dropped", action="store_true")
    args = parser.parse_args(argv)
    members, _ = readonly_members(args.config)
    deriver = PersonaDeriver(members, LLMClient.from_file(args.llm_config), JevClient.from_file(args.llm_config),
                             days=args.days, limit=args.limit, seed=args.seed)
    for label, room in pick_rooms(members, args.room):
        result = deriver.derive(room, dry_run=True)
        reasons = Counter(reason for _, _, reason in result.dropped)
        print(f"{label}: status={result.status} messages={result.message_count} "
              f"ngrams={len(result.stats.get('ngrams', []))} kept={result.kept} dropped={len(result.dropped)} "
              f"reasons={dict(sorted(reasons.items()))} current_version={current_version(members, room)}")
        if result.data:
            counts = {key: len(result.data[key]) for key in CATEGORIES}
            print(f"  categories={counts} rhythm={'有' if result.data['rhythm'] else '无'} "
                  f"chars={len(result.data['text'])}")
            if args.show_text:
                print("  " + result.data["text"].replace("\n", "\n  "))
        if args.show_dropped:
            for key, item, reason in result.dropped:
                print(f"  drop {key}/{reason}: {item}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
