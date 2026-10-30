"""艾特回复的看图 / 联网判断（Jev）离线评测。

子命令：
  sample        只读 .runtime/member_data.sqlite3，切出机器人已回复的艾特提问，写 .runtime/tool_eval/dataset.json
  show          紧凑打印样本（便于人工标注），可按 --offset/--limit 分页
  apply-labels  把 labels.json（{id: {"search": bool, "vision": bool}}）并入 dataset，按标签分层划分 dev/holdout（30%）
  run           用线上同一套 ``JevToolRouter`` 提示调用 Jev（并发≤2），结果存 .runtime/tool_eval/<时间戳>_<工具>/
  report        汇总 run 目录：误触发率、召回率、p50/p90 耗时，并扫阈值 0.30–0.90

看图评测的每条样本都假定“提问附带了一张图片”，只判断提问本身是否依赖图片。
数据库只读（URI mode=ro）；真实聊天只落在 .runtime（已 gitignore）。人工补充的合成问法单独标为
``synthetic``，报告里与真实样本分开统计。
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wxbot_framework.conversation.tool_router import JevToolRouter  # noqa: E402
from wxbot_framework.integrations.jev import JevClient  # noqa: E402

DEFAULT_DB = ROOT / ".runtime" / "member_data.sqlite3"
EVAL_DIR = ROOT / ".runtime" / "tool_eval"
DATASET = EVAL_DIR / "dataset.json"
LABELS = EVAL_DIR / "labels.json"
CONFIG = ROOT / "llm_config.json"

# 抽样时用于富集正例的通用关键词（不含真实聊天内容）。
SEARCH_HINT = re.compile(r"今天|最新|现在|价格|多少钱|新闻|版本|天气|发布|比赛|搜|查|官网|股|汇率|多少")
IMAGE_HINT = re.compile(r"图|截图|照片|表情包|看看这")
AT_LABEL = re.compile(r"@[^@\r\n ]+ ")

# 合成问法：真实库里还没有“附图提问”，先用通用问法补齐两类，报告里单独统计。
SYNTHETIC = (
    ("v-pos-1", "这是什么", True, False), ("v-pos-2", "图里写的啥", True, False),
    ("v-pos-3", "帮我看看这个报错怎么解决", True, False), ("v-pos-4", "这张截图里的价格是多少", True, False),
    ("v-pos-5", "看看我这个穿搭怎么样", True, False), ("v-pos-6", "这道题选什么", True, False),
    ("v-pos-7", "这个表情包什么意思", True, False), ("v-pos-8", "识别一下这个植物", True, True),
    ("v-pos-9", "这车什么型号，现在卖多少", True, True), ("v-pos-10", "翻译一下图上的英文", True, False),
    ("v-neg-1", "哈哈哈哈", False, False), ("v-neg-2", "你今天吃饭了吗", False, False),
    ("v-neg-3", "晚上打游戏不", False, False), ("v-neg-4", "给我讲个笑话", False, False),
    ("v-neg-5", "你觉得我该不该辞职", False, False), ("v-neg-6", "1+1等于几", False, False),
    ("s-pos-1", "今天上海天气怎么样", False, True), ("s-pos-2", "最新的 iPhone 多少钱", False, True),
    ("s-pos-3", "昨晚的球赛谁赢了", False, True), ("s-pos-4", "Python 最新版本是多少", False, True),
    ("s-pos-5", "帮我搜一下这家店的营业时间", False, True), ("s-neg-1", "你是谁", False, False),
    ("s-neg-2", "李白是哪个朝代的", False, False), ("s-neg-3", "帮我想个群名", False, False),
)


def open_readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))]


def mention_questions(db):
    """机器人紧接着回复过的、带 @ 的群友提问；去掉 @ 标签后至少 2 字。"""
    conn = open_readonly(db)
    try:
        rows = conn.execute("select id, room, text, is_self, bot_generated from messages "
                            "order by room, received_at, id").fetchall()
    finally:
        conn.close()
    result, seen = [], set()
    for current, following in zip(rows, rows[1:]):
        if current[3] or following[1] != current[1] or not following[4] or "@" not in current[2]:
            continue
        text = AT_LABEL.sub("", current[2]).strip()
        if len(text) < 2 or len(text) > 300 or text in seen:
            continue
        seen.add(text)
        result.append({"id": f"m{current[0]}", "question": text})
    return result


def cmd_sample(args):
    questions = mention_questions(args.db)
    rng = random.Random(args.seed)
    enriched = [q for q in questions if SEARCH_HINT.search(q["question"]) or IMAGE_HINT.search(q["question"])]
    rng.shuffle(enriched)
    chosen = {q["id"]: q for q in enriched[:args.enriched]}
    rest = [q for q in questions if q["id"] not in chosen]
    rng.shuffle(rest)
    for q in rest[:args.random]:
        chosen[q["id"]] = q
    samples = [dict(q, source="real", search=None, vision=None, split=None)
               for q in sorted(chosen.values(), key=lambda q: int(q["id"][1:]))]
    samples += [{"id": sid, "question": text, "source": "synthetic", "vision": vision, "search": search,
                 "split": "synthetic"} for sid, text, vision, search in SYNTHETIC]
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    DATASET.write_text(json.dumps({"samples": samples}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"样本 {len(samples)} 条（真实 {len(chosen)}，合成 {len(SYNTHETIC)}）-> {DATASET}")


def load_dataset():
    return json.loads(DATASET.read_text(encoding="utf-8"))


def cmd_show(args):
    samples = [s for s in load_dataset()["samples"] if s["source"] == "real"]
    for s in samples[args.offset: args.offset + args.limit]:
        print(f"{s['id']}\t{s['question'][:args.width]}")


def cmd_apply_labels(args):
    data = load_dataset()
    labels = json.loads(LABELS.read_text(encoding="utf-8"))
    rng = random.Random(args.seed)
    groups = {}
    for s in data["samples"]:
        if s["source"] != "real":
            continue
        label = labels.get(s["id"])
        if label is not None:
            s["search"], s["vision"] = bool(label["search"]), bool(label["vision"])
        groups.setdefault((s["search"], s["vision"]), []).append(s)
    for group in groups.values():
        group.sort(key=lambda s: s["id"])
        rng.shuffle(group)
        hold = round(len(group) * 0.3)
        for index, s in enumerate(group):
            s["split"] = "holdout" if index < hold else "dev"
    DATASET.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    stat = {}
    for s in data["samples"]:
        key = f"{s['source']}/{s['split']}/search={s['search']}/vision={s['vision']}"
        stat[key] = stat.get(key, 0) + 1
    print(json.dumps(stat, ensure_ascii=False, indent=1))


def cmd_run(args):
    samples = [s for s in load_dataset()["samples"] if s[args.tool] is not None]
    if args.split != "all":
        samples = [s for s in samples if s["split"] == args.split]
    if args.limit:
        samples = samples[:args.limit]
    router = JevToolRouter(JevClient.from_file(CONFIG))
    out = EVAL_DIR / f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}_{args.tool}_{args.split}"
    out.mkdir(parents=True, exist_ok=True)

    def one(sample):
        start = time.perf_counter()
        if args.tool == "vision":
            decision = router.decide_vision(question=sample["question"], has_image=True)
        else:
            decision = router.decide_search(question=sample["question"])
        return {"id": sample["id"], "source": sample["source"], "split": sample["split"],
                "label": sample[args.tool], "probability": decision.probability, "status": decision.status,
                "error": decision.error, "latency": round(time.perf_counter() - start, 3)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(2, args.concurrency)) as pool:
        results = list(pool.map(one, samples))
    with (out / "results.jsonl").open("w", encoding="utf-8") as handle:
        for row in results:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"{len(results)} 次调用，失败 {sum(1 for r in results if r['error'])} -> {out}")
    summarize(results, out.name)


THRESHOLDS = [round(0.3 + 0.05 * k, 2) for k in range(13)]


def rates(results, threshold):
    negative = [r for r in results if r["label"] is False]
    positive = [r for r in results if r["label"] is True]
    fp = sum(1 for r in negative if (r["probability"] or 0) >= threshold)
    tp = sum(1 for r in positive if (r["probability"] or 0) >= threshold)
    return (fp / len(negative) if negative else None, tp / len(positive) if positive else None,
            len(negative), len(positive))


def summarize(results, name):
    latency = [r["latency"] for r in results if not r["error"]]
    p50, p90 = percentile(latency, 0.5), percentile(latency, 0.9)
    print(f"[{name}] n={len(results)} p50={p50 or 0:.2f}s p90={p90 or 0:.2f}s "
          f"错误={sum(1 for r in results if r['error'])}")
    for source in ("real", "synthetic"):
        rows = [r for r in results if r["source"] == source]
        if not rows:
            continue
        print(f"  {source}: 阈值  误触发率  召回率")
        for threshold in THRESHOLDS:
            fpr, recall, negatives, positives = rates(rows, threshold)
            fpr_text = f"{fpr:7.1%}" if fpr is not None else "      -"
            recall_text = f"{recall:6.1%}" if recall is not None else "     -"
            print(f"        {threshold:.2f}  {fpr_text}  {recall_text}")
        print(f"        (negative={negatives}, positive={positives})")


def cmd_report(args):
    for directory in args.run_dirs:
        path = Path(directory)
        results = [json.loads(line) for line in (path / "results.jsonl").read_text(encoding="utf-8").splitlines()
                   if line]
        if args.split != "all":
            results = [r for r in results if r["split"] == args.split]
        summarize(results, path.name)
        if args.errors:
            wrong = [r for r in results if r["label"] is not None and
                     r["label"] != ((r["probability"] or 0) >= args.threshold)]
            for row in wrong[:args.errors]:
                print(f"  x {row['id']} label={row['label']} p={row['probability']}")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sample = sub.add_parser("sample")
    sample.add_argument("--db", default=DEFAULT_DB)
    sample.add_argument("--enriched", type=int, default=70, help="按搜索/图片关键词富集的条数")
    sample.add_argument("--random", type=int, default=110, help="其余随机抽取的条数")
    sample.add_argument("--seed", type=int, default=7)
    show = sub.add_parser("show")
    show.add_argument("--offset", type=int, default=0)
    show.add_argument("--limit", type=int, default=60)
    show.add_argument("--width", type=int, default=80)
    apply = sub.add_parser("apply-labels")
    apply.add_argument("--seed", type=int, default=11)
    run = sub.add_parser("run")
    run.add_argument("--tool", choices=["search", "vision"], required=True)
    run.add_argument("--split", choices=["dev", "holdout", "synthetic", "all"], default="all")
    run.add_argument("--concurrency", type=int, default=2)
    run.add_argument("--limit", type=int, default=0)
    report = sub.add_parser("report")
    report.add_argument("run_dirs", nargs="+")
    report.add_argument("--split", choices=["dev", "holdout", "synthetic", "all"], default="all")
    report.add_argument("--errors", type=int, default=0, help="列出前 N 个判错的样本 id")
    report.add_argument("--threshold", type=float, default=0.5)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    {"sample": cmd_sample, "show": cmd_show, "apply-labels": cmd_apply_labels,
     "run": cmd_run, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
