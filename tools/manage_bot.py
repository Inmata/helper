"""离线维护知识库，不连接微信。机器人运行时请使用其控制台的 manage 命令。"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wxbot_framework.authorization import LOCAL_EDITOR
from wxbot_framework.app.bootstrap import open_knowledge
from wxbot_framework.config import ConfigStore
from wxbot_framework.locking import InstanceLock
from wxbot_framework.app.maintenance import MaintenanceService


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "bot_config.json")
    parser.add_argument("--room")
    parser.add_argument("command", help='例如："人物 查看 示例人物"')
    args = parser.parse_args()
    members = None
    try:
        config = ConfigStore(args.config).load()
        options = maintenance_options(config)
        with InstanceLock(ROOT / ".runtime/bot.lock"):
            # 维护命令不导入知识种子，保持离线执行只做本次请求的改动。
            members, knowledge = open_knowledge(config, args.config.resolve().parent, seed=False)
            service = MaintenanceService(knowledge, options=options)
            print(service.execute(args.command, LOCAL_EDITOR, explicit_room=args.room))
        return 0
    except Exception as error:
        print(f"维护失败：{error}", file=sys.stderr)
        return 1
    finally:
        if members:
            members.close()


if __name__ == "__main__":
    raise SystemExit(main())
