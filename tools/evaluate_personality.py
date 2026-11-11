"""用正式生成路径回放虚构人格场景；默认列出场景，--run 才调用模型，始终不发送群消息。"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from plugins.mention_chat.cold_reply import SYSTEM as COLD_SYSTEM
from plugins.mention_chat.voice_reply import VOICE_PROMPT, voice_reply_format
from wxbot_framework.conversation.dialogue import Dialogue, SYSTEM
from wxbot_framework.conversation.members import ConversationSnapshot
from wxbot_framework.conversation.personality import PersonalityLLM, PersonalityState, PLAYFUL_NAME
from wxbot_framework.conversation.planner import generate_proactive
from wxbot_framework.integrations.llm import LLMClient
from wxbot_framework.memory.hourly import SYSTEM as HOURLY_SYSTEM

CASES = ROOT / "tests/fixtures/personality_cases.json"
NOW = datetime(2026, 11, 12, 13, tzinfo=timezone.utc)


def check_reply(case, replies):
    errors = []
    if not replies or not all(replies):
        return ["empty"]
    last, preference = voice_reply_format(replies[-1])
    if case.get("literal") and last != case["literal"]:
        errors.append("literal")
    if case["kind"] == "voice" and preference is not True:
        errors.append("voice_marker")
    if case.get("one_of") and last not in case["one_of"]:
        errors.append("exact_format")
    limit = case.get("max_chars") or (60 if case["kind"] == "chat" and not case.get("detail") else None)
    if limit and any(len(voice_reply_format(reply)[0]) > limit for reply in replies):
        errors.append("length")
    if any(piece not in last for piece in case.get("required", ())):
        errors.append("missing_fact")
    if any(piece not in last for piece in case.get("contains", ())):
        errors.append("missing_content")
    if case["kind"] in ("proactive", "hourly") and any(c in last for c in "@＠\r\n"):
        errors.append("proactive_format")
    return errors


def run_case(case, config, repetition):
    client = LLMClient.from_file(config)
    model = PersonalityLLM(client, PersonalityState("fictional@chatroom", name=PLAYFUL_NAME,
        supplement="口头禅：兄弟、急了、真的假的。群里经常互相调侃。"), current=lambda: True, for_reply=True)
    history = [{"role": role, "text": text} for role, text in case["context"]]
    replies, seconds = [], []
    error = None
    try:
        for query in case["turns"]:
            rows = tuple({"received_at": NOW.isoformat(), "text": row["text"],
                          "speaker": "bot" if row["role"] == "assistant" else "P2" if row["role"] == "other" else "P1",
                          "speaker_name": "机器人" if row["role"] == "assistant" else "另一位群友" if row["role"] == "other" else "小栗",
                          "bot_generated": row["role"] == "assistant"} for row in history)
            started = time.monotonic()
            if case["kind"] == "proactive":
                lines = tuple((len(rows) - i, row["speaker_name"], row["text"]) for i, row in enumerate(rows))
                reply = generate_proactive(model, lines, intent="reply_banter", target=len(lines) - 1,
                                           local_time="11-12 21:00（晚上）", examples=["兄弟，又急了", "想得挺美"])
            elif case["kind"] == "hourly":
                recent = [{"time": row["received_at"], "name": row["speaker_name"],
                           "speaker": row["speaker"], "text": row["text"]} for row in rows]
                payload = {"recent": recent, "knowledge": [], "recent_topics": []}
                result = model.complete_json(HOURLY_SYSTEM, json.dumps(payload, ensure_ascii=False), max_tokens=1024)
                reply = result.get("text", "") if result.get("speak") else ""
            elif case["kind"] == "catch_up":
                reply = Dialogue(model, clock=lambda: NOW).catch_up(rows, query)
            elif case["kind"] == "cold":
                reply = model.complete_text(COLD_SYSTEM, "对方刚说：" + query, max_tokens=128)
            else:
                snapshot = ConversationSnapshot("fictional@chatroom", "2026-11-12", 0, "小栗", (), query,
                    tuple(row for row in rows if not row["bot_generated"]), 0, rows, "P1", ())
                options = {"examples": ["兄弟，又急了", "想得挺美", "真的假的"]}
                if case["kind"] in ("chat", "voice") and not case.get("detail"):
                    options["system"] = SYSTEM + VOICE_PROMPT
                if case.get("evidence"):
                    options["tool_results"] = [{"tool": "search", "ok": True, "text": case["evidence"]}]
                reply = Dialogue(model, clock=lambda: NOW).reply(snapshot, detail=case.get("detail", False), **options)
            seconds.append(round(time.monotonic() - started, 2))
            replies.append(reply)
            history.extend([{"role": "user", "text": query}, {"role": "assistant", "text": reply}])
    except Exception as exc:
        error = type(exc).__name__
    return {"id": case["id"], "repetition": repetition, "replies": replies, "seconds": seconds,
            "hard_errors": [error] if error else check_reply(case, replies), "expectation": case["expectation"]}


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--llm-config", type=Path, default=ROOT / "llm_config.json")
    parser.add_argument("--split", choices=("all", "development", "holdout"), default="all")
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--output", type=Path, default=ROOT / ".runtime/persona_eval")
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat 必须大于 0")
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    cases = [case for case in cases if args.split == "all" or case["split"] == args.split]
    if not args.run:
        print(json.dumps({"场景数": len(cases), "每场轮数": args.repeat, "场景": [c["id"] for c in cases]}, ensure_ascii=False))
        return 0
    client = LLMClient.from_file(args.llm_config)
    results = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = [pool.submit(run_case, case, args.llm_config, i) for case in cases for i in range(args.repeat)]
        for job in as_completed(jobs):
            result = job.result()
            results.append(result)
            print(json.dumps({"场景": result["id"], "重复": result["repetition"],
                              "硬约束错误": result["hard_errors"]}, ensure_ascii=False), flush=True)
    times = sorted(seconds for result in results for seconds in result["seconds"])
    report = {"model": client.model, "chat_reasoning_effort": client.chat_reasoning_effort,
              "scenarios": len(cases), "runs": len(results), "turns": len(times),
              "hard_failures": sum(bool(r["hard_errors"]) for r in results),
              "median_seconds": round(statistics.median(times), 2) if times else None,
              "p90_seconds": times[min(len(times) - 1, int(len(times) * .9))] if times else None,
              "results": results}
    args.output.mkdir(parents=True, exist_ok=True)
    path = args.output / (datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"报告": str(path), **{k: v for k, v in report.items() if k != "results"}}, ensure_ascii=False))
    return 1 if report["hard_failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
