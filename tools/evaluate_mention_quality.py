"""艾特回复质量旧新对比：虚构样本 × 旧版（1021fae）/新版（工作区）提示词链路。

默认只校验样本并离线渲染，不联网；显式 --run 才调用模型，每条每版恰一次请求，不重试。
两版的 dialogue.py / personality.py 都在隔离命名空间执行，GROUP_LORE 换成样本里的虚构设定，
不导入生产 wxbot_framework.lore，不读数据库、不加载微信。结果写入 .runtime/mention-quality-eval/<时间>/。
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import types
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wxbot_framework import commands as real_commands  # noqa: E402
from wxbot_framework import timeutil as real_timeutil  # noqa: E402
from wxbot_framework.conversation import members as real_members  # noqa: E402
from wxbot_framework.integrations import llm as real_llm  # noqa: E402
from wxbot_framework.messaging import mentions as real_mentions  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "mention_quality_cases.json"
OUTPUT_ROOT = ROOT / ".runtime" / "mention-quality-eval"
OLD_COMMIT = "1021fae47cd2b446b46746189f2daf0ae81976de"
SOURCES = ("wxbot_framework/conversation/dialogue.py", "wxbot_framework/conversation/personality.py")
DEFECTS = {f"D{n}" for n in range(1, 11)}
LEGACY_IDS = {"complex06", "complex09", "normal07", "normal10"}
HARD_LIMIT_SECONDS = 60
# 旧版：全局 20 秒 / 512；新版：聊天 60 秒、普通 512、详细 2048。只覆盖内存中的配置，不改私有文件。
VERSION_OPTIONS = {
    "old": {"timeout_seconds": 20, "max_tokens": 512,
            "chat_timeout_seconds": 20, "chat_max_tokens": 512, "detail_max_tokens": 512},
    "new": {"timeout_seconds": 20, "max_tokens": 512,
            "chat_timeout_seconds": 60, "chat_max_tokens": 512, "detail_max_tokens": 2048},
}
SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9]{8,}|api[_-]?key|Bearer\s", re.I)


# ---------- 样本 ----------

def load_cases(path=FIXTURES):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate_cases(data):
    """返回问题列表；空列表表示样本可用。"""
    problems = []
    cases = data.get("cases") or []
    people = set(data.get("fictional_people") or [])
    if not 16 <= len(cases) <= 20:
        problems.append(f"样本数应为16—20，实际{len(cases)}")
    ids = [case.get("id") for case in cases]
    if len(set(ids)) != len(ids):
        problems.append("样本 id 重复")
    if not LEGACY_IDS <= set(ids):
        problems.append(f"缺少旧反例 id：{sorted(LEGACY_IDS - set(ids))}")
    covered = {case.get("defect") for case in cases}
    if not DEFECTS <= covered:
        problems.append(f"未覆盖缺陷：{sorted(DEFECTS - covered)}")
    if not isinstance(data.get("group_lore"), str):
        problems.append("缺少虚构 group_lore")
    for case in cases:
        cid = case.get("id")
        if not case.get("current") or not case.get("expected"):
            problems.append(f"{cid}：缺 current 或 expected")
        if cid in LEGACY_IDS and "非旧原题" not in case.get("variant_note", ""):
            problems.append(f"{cid}：旧反例改写需注明“同类改写，非旧原题”")
        names = [case.get("speaker")]
        names += [row.get("name") for row in case.get("group_recent", [])]
        names += [row.get("name") for row in case.get("history", []) if row.get("role") == "user"]
        names += [row.get("name") for row in case.get("knowledge", [])]
        if case.get("speaker_profile"):
            names.append(case["speaker_profile"].get("name"))
        for name in names:
            if name not in people:
                problems.append(f"{cid}：人物“{name}”不在虚构名单")
    if SECRET_PATTERN.search(json.dumps(data, ensure_ascii=False)):
        problems.append("样本中出现疑似密钥字段")
    return problems


def detail_hints():
    """从插件源码取 DETAIL_HINTS 字面量，避免导入插件（插件会加载生产 GROUP_LORE）。"""
    tree = ast.parse((ROOT / "plugins" / "mention_chat" / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "DETAIL_HINTS" for t in node.targets):
            return ast.literal_eval(node.value)
    raise RuntimeError("插件中找不到 DETAIL_HINTS")


def snapshot_for(case, day):
    def iso(value):
        return value
    return SimpleNamespace(
        speaker_name=case["speaker"], day=day, current=case["current"],
        messages=[{"received_at": iso(row["t"]), "text": row["text"]} for row in case.get("previous", [])],
        group_messages=[{"received_at": iso(row["t"]), "speaker": "wxid_fake_" + str(index),
                         "speaker_name": row["name"], "text": row["text"]}
                        for index, row in enumerate(case.get("group_recent", []))],
        history=[{"bot_generated": row["role"] == "assistant", "text": row["text"],
                  "speaker_name": row.get("name")} for row in case.get("history", [])])


def knowledge_for(case):
    return [{"id": f"k{index}", "name": item["name"], "kind": item["kind"], "scope": item.get("scope", "person"),
             "content": item["content"], "updated_at": item["date"] + "T00:00:00Z" if item.get("date") else None}
            for index, item in enumerate(case.get("knowledge", []))]


# ---------- 隔离加载两版源码 ----------

def read_sources(version):
    if version == "old":
        texts = {}
        for path in SOURCES:
            result = subprocess.run(["git", "show", f"{OLD_COMMIT}:{path}"], cwd=ROOT,
                                    capture_output=True, check=True)
            texts[path] = result.stdout.decode("utf-8-sig")
        return texts
    return {path: (ROOT / path).read_text(encoding="utf-8-sig") for path in SOURCES}


def load_version(version, group_lore, texts):
    """在独立包名下执行两份源码：相对导入指向真实工具模块，只有 lore 换成虚构设定。"""
    pkg = f"_mq_eval_{version}"
    for name in (pkg, pkg + ".conversation", pkg + ".integrations", pkg + ".messaging"):
        module = types.ModuleType(name)
        module.__path__ = []
        sys.modules[name] = module
    lore = types.ModuleType(pkg + ".lore")
    lore.GROUP_LORE = group_lore
    aliases = {pkg + ".lore": lore, pkg + ".integrations.llm": real_llm, pkg + ".timeutil": real_timeutil,
               pkg + ".messaging.mentions": real_mentions, pkg + ".commands": real_commands,
               pkg + ".conversation.members": real_members}
    sys.modules.update(aliases)
    loaded = {}
    for path, short in zip(SOURCES, ("dialogue", "personality")):
        name = f"{pkg}.conversation.{short}"
        module = types.ModuleType(name)
        module.__package__ = pkg + ".conversation"
        module.__file__ = f"<{version}:{path}>"
        sys.modules[name] = module
        exec(compile(texts[path], module.__file__, "exec"), module.__dict__)
        loaded[short] = module
    if "wxbot_framework.lore" in sys.modules:
        raise RuntimeError("评测进程不应加载生产 GROUP_LORE")
    return loaded["dialogue"], loaded["personality"]


# ---------- 请求记录 ----------

class _Recorded(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Recorder:
    """包住 urlopen，只记录 finish_reason / usage / 耗时，不保存请求头或地址。"""

    def __init__(self):
        self.calls = []

    def __call__(self, request, timeout=None):
        entry = {"timeout": timeout}
        self.calls.append(entry)
        start = time.monotonic()
        try:
            with real_urlopen(request, timeout=timeout) as response:
                raw = response.read(1024 * 1024 + 1)
        finally:
            entry["http_seconds"] = round(time.monotonic() - start, 2)
        try:
            body = json.loads(raw)
            choice = (body.get("choices") or [{}])[0]
            entry["finish_reason"] = choice.get("finish_reason")
            entry["usage"] = body.get("usage")
            entry["response_model"] = body.get("model")
        except (ValueError, AttributeError, IndexError, TypeError):
            entry["finish_reason"] = None
        return _Recorded(raw)


real_urlopen = real_llm.urlopen


def classify_error(error):
    text = str(error)
    if "截断" in text:
        return "length"
    if "内容过滤" in text:
        return "content_filter"
    if "超时" in text or "连接失败" in text:
        return "timeout_or_connection"
    if "HTTP" in text:
        return "http_error"
    return "format_or_other"


# ---------- 确定性格式检查 ----------

MARKDOWN = re.compile(r"\*\*|__|^\s*#{1,6}\s|^\s*\|.*\|\s*$|```", re.M)
SENTENCE_END = re.compile(r"[。！？!?]+")


def format_checks(case, reply):
    checks = case.get("checks", {})
    result = {"markdown": not MARKDOWN.search(reply)}
    if "forbidden" in checks:
        hits = [word for word in checks["forbidden"] if word in reply]
        result["forbidden"] = not hits
        if hits:
            result["forbidden_hits"] = hits
    if "max_chars" in checks:
        result["max_chars"] = len(reply) <= checks["max_chars"]
    if "min_chars" in checks:
        result["min_chars"] = len(reply) >= checks["min_chars"]
    if checks.get("no_newline"):
        result["no_newline"] = "\n" not in reply
    if "max_sentences" in checks:
        count = len([part for part in SENTENCE_END.split(reply.strip()) if part.strip()])
        result["max_sentences"] = count <= checks["max_sentences"]
    result["all_pass"] = all(value for key, value in result.items() if key != "forbidden_hits")
    return result


# ---------- 运行 ----------

def build_dialogue(version, dialogue_mod, personality_mod, client):
    state = personality_mod.PersonalityState(room="eval@chatroom")
    wrapped = personality_mod.PersonalityLLM(client, state, current=lambda: True)
    return dialogue_mod.Dialogue(wrapped, display_timezone="Asia/Shanghai")


def reply_kwargs(version, case, hints):
    kwargs = {"knowledge": knowledge_for(case)}
    if case.get("speaker_profile"):
        kwargs["speaker_profile"] = case["speaker_profile"]
    if version == "new" and any(hint in case["current"] for hint in hints):
        kwargs["detail"] = True
    return kwargs


def run_one(dialogue, personality_mod, case, day, kwargs):
    outcome = {}

    def target():
        try:
            outcome["reply"] = personality_mod.personality_text(dialogue.reply(snapshot_for(case, day), **kwargs))
        except real_llm.LLMError as error:
            outcome["error"], outcome["error_kind"] = str(error), classify_error(error)
        except Exception as error:  # 只记类型，避免异常文本带出地址等配置。
            outcome["error"], outcome["error_kind"] = type(error).__name__, "exception"

    start = time.monotonic()
    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(HARD_LIMIT_SECONDS)
    outcome["seconds"] = round(time.monotonic() - start, 2)
    if worker.is_alive():
        outcome.update(error="超过评测硬上限60秒", error_kind="timeout_hard_limit")
    return outcome


def render_all(data, versions):
    day = data["day"]
    for version, (dialogue_mod, personality_mod) in versions.items():
        captured = []

        class Capture:
            def complete_text(self, system, user, **options):
                captured.append((system, user, options))
                return "（离线渲染，未调用模型）"

        dialogue = build_dialogue(version, dialogue_mod, personality_mod, Capture())
        for case in data["cases"]:
            dialogue.reply(snapshot_for(case, day), **reply_kwargs(version, case, detail_hints()))
        system_len = len(captured[0][0])
        details = sum(1 for _, _, options in captured if options.get("detail"))
        print(f"[{version}] 渲染 {len(captured)} 条，system {system_len} 字符，detail={details} 条，未联网")
    return captured


def run_batch(data, versions, config_path, selected):
    config = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
    out = OUTPUT_ROOT / stamp
    out.mkdir(parents=True, exist_ok=False)
    (out / "cases.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    meta = {"started_at": datetime.now().isoformat(timespec="seconds"), "model": config.get("model"),
            "old_commit": OLD_COMMIT, "new": "working tree",
            "source_sha256": {version: {path: hashlib.sha256(text.encode("utf-8")).hexdigest()
                                        for path, text in texts.items()}
                              for version, (_, _, texts) in versions.items()},
            "options": VERSION_OPTIONS, "group_lore": "fixtures 虚构设定", "retries": 0,
            "hard_limit_seconds": HARD_LIMIT_SECONDS, "cases": [case["id"] for case in selected]}
    (out / "versions.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    hints = detail_hints()
    clients = {version: real_llm.LLMClient({**config, **VERSION_OPTIONS[version]}) for version in versions}
    recorder = Recorder()
    results = []
    with mock.patch.object(real_llm, "urlopen", recorder), \
            open(out / "results.jsonl", "w", encoding="utf-8") as stream:
        for case in selected:
            for version, (dialogue_mod, personality_mod, _) in versions.items():
                before = len(recorder.calls)
                kwargs = reply_kwargs(version, case, hints)
                dialogue = build_dialogue(version, dialogue_mod, personality_mod, clients[version])
                outcome = run_one(dialogue, personality_mod, case, data["day"], kwargs)
                calls = recorder.calls[before:]
                row = {"id": case["id"], "defect": case["defect"], "version": version,
                       "detail": bool(kwargs.get("detail")), "requests": len(calls),
                       "completed": "reply" in outcome, **outcome,
                       "finish_reason": calls[-1].get("finish_reason") if calls else None,
                       "usage": calls[-1].get("usage") if calls else None}
                if "reply" in outcome:
                    row["chars"] = len(outcome["reply"])
                    row["format"] = format_checks(case, outcome["reply"])
                results.append(row)
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                stream.flush()
                status = "ok" if row["completed"] else row["error_kind"]
                print(f"{case['id']:<20} {version}: {status} {row['seconds']}s", flush=True)
    summary = summarize(results)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"结果目录：{out}")


def summarize(results):
    summary = {}
    for version in ("old", "new"):
        rows = [row for row in results if row["version"] == version]
        done = [row for row in rows if row["completed"]]
        usage = [row["usage"] for row in rows if row.get("usage")]
        summary[version] = {
            "requests": sum(row["requests"] for row in rows), "cases": len(rows), "completed": len(done),
            "failed": {row["id"]: row["error_kind"] for row in rows if not row["completed"]},
            "format_all_pass": sum(1 for row in done if row["format"]["all_pass"]),
            "format_fail_ids": [row["id"] for row in done if not row["format"]["all_pass"]],
            "seconds_total": round(sum(row["seconds"] for row in rows), 2),
            "seconds_max": max((row["seconds"] for row in rows), default=None),
            "usage_reported": len(usage),
            "prompt_tokens": sum(item.get("prompt_tokens", 0) for item in usage) if usage else None,
            "completion_tokens": sum(item.get("completion_tokens", 0) for item in usage) if usage else None,
            "total_tokens": sum(item.get("total_tokens", 0) for item in usage) if usage else None,
        }
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", action="store_true", help="真实调用模型（默认只校验并离线渲染，不联网）")
    parser.add_argument("--config", default=str(ROOT / "llm_config.json"), help="私有 llm_config 路径，仅 --run 时读取")
    parser.add_argument("--cases", help="逗号分隔的样本 id，用于日后定向重现")
    parser.add_argument("--limit", type=int, help="只取前 N 条样本")
    parser.add_argument("--fixtures", default=str(FIXTURES), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    data = load_cases(args.fixtures)
    problems = validate_cases(data)
    if problems:
        print("样本校验失败：\n" + "\n".join(problems))
        return 2
    print(f"样本校验通过：{len(data['cases'])} 条，覆盖 {sorted(DEFECTS)}")
    selected = data["cases"]
    if args.cases:
        wanted = args.cases.split(",")
        selected = [case for case in selected if case["id"] in wanted]
    if args.limit:
        selected = selected[:args.limit]

    versions = {}
    for version in ("old", "new"):
        texts = read_sources(version)
        dialogue_mod, personality_mod = load_version(version, data["group_lore"], texts)
        versions[version] = (dialogue_mod, personality_mod, texts)
    render_all({**data, "cases": selected}, {key: value[:2] for key, value in versions.items()})
    if not args.run:
        print("未指定 --run：未调用模型。")
        return 0
    run_batch(data, versions, args.config, selected)
    return 0


if __name__ == "__main__":
    sys.exit(main())
