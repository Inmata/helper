"""在隔离的群与数据库中，向真实模型和 Magpie 回放两轮纠错。"""
import argparse
import logging
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from test_verified_corrections import ROOM, URL, VerifiedCorrectionTests
from wxbot_framework.conversation.mention_tools import MentionTools
from wxbot_framework.conversation.reply_wait import ReplyWait
from wxbot_framework.integrations.jev import JevClient
from wxbot_framework.integrations.llm import LLMClient
from wxbot_framework.integrations.magpie_tools import MagpieTools


def main(config):
    case = VerifiedCorrectionTests()
    try:
        case.setUp()
        case.ctx._llm = LLMClient.from_file(config)
        case.ctx._jev = JevClient.from_file(config)
        case.ctx._magpie = MagpieTools.from_file(config)
        wait = ReplyWait(case.plugin._status)
        case.plugin._wait = wait
        case.addCleanup(wait.close)
        opening = case.fixture.message("英伟达的黄仁勋现在还在任？")
        case.members.record_bot_reply(ROOM, "黄仁勋仍是英伟达CEO。", source_id=case.members.message_id(opening))
        print("【旧答】黄仁勋仍是英伟达CEO。", flush=True)
        correction = "你搞错了，黄仁勋已经辞去英伟达CEO了。看英伟达提交给SEC的8-K：" + URL
        print("【第一轮群消息】", correction, flush=True)
        case.send(correction)
        print("【第一轮发送】", case.fixture.backend.texts[-1][1] if case.fixture.backend.texts else "无", flush=True)
        facts = case.store.list(ROOM)
        for fact in facts:
            print("【入列】", {**fact.material(), "检索名称": fact.names, "录入问法": fact.phrases}, flush=True)
        if not any(fact.effective_on == "2026-11-11" and "ceo" in fact.relation.casefold() for fact in facts):
            raise SystemExit("验收未通过：没留下有官方证据的离任记录。")
        case.members.reset_context(ROOM)
        question = "英伟达的老黄辞职了？"
        print("【清空近期上下文，另一位群友发问】", question, flush=True)
        with patch.object(MentionTools, "search", autospec=True, side_effect=MentionTools.search) as search:
            request = case.send(question, sender="wxid_next")
        source_id = case.members.message_id(request)
        used = case.store.used(ROOM, (source_id,))
        rows = case.members.connection().execute(
            "SELECT output FROM reply_receipts WHERE source_id=? AND status='submitted' AND kind='text'",
            (source_id,)).fetchall()
        for row in rows:
            print("【第二轮发送】", row["output"], flush=True)
        print("【第二轮联网次数】", search.call_count, "【回复使用的纠错编号】", sorted(used), flush=True)
        if not (rows and used and search.call_count == 0):
            raise SystemExit("验收未通过：次轮没有命中纠错并直接发送。")
    finally:
        case.doCleanups()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="本机 llm_config.json 路径")
    options = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main(options.config)
