"""拟人化指标：机器人与群友的发言长度、空行分段占比，以及机器人独有的高频短语。

只读打开 SQLite（URI mode=ro），不写库、不联网；输出只有统计数字和机器人自己说过的短语，
不输出任何群友原文。可重复运行，参数见 --help。

    python tools/humanlike_metrics.py
    python tools/humanlike_metrics.py --since 2026-10-20 --until 2026-10-27 --min-count 10 --top 30

口径：机器人 = bot_generated=1；群友 = bot_generated=0 且 is_self=0（排除机器人账号自己的原生回显）。
--since/--until 为 ISO 日期或时间，无时区时按 UTC 处理，区间为 [since, until)。
短语排序依据：机器人出现率 /（群友出现率 + 平滑项）；出现率按“含该短语的消息条数 / 该方消息总数”。
"""
from __future__ import annotations

import argparse
import math
import re
import sqlite3
import statistics
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / ".runtime" / "member_data.sqlite3"
BLANK_LINE = re.compile(r"\n[ \t　]*\n")


def normalize_bound(value):
    """把 --since/--until 规整成与库内时间可比的 'YYYY-MM-DDTHH:MM:SS'（UTC）。"""
    if value is None:
        return None
    moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def open_readonly(path):
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def load_texts(path, *, since=None, until=None, room=None):
    """返回 (机器人文本列表, 群友文本列表)。只执行 SELECT。"""
    sql = "SELECT text, bot_generated, is_self FROM messages WHERE 1=1"
    values = []
    if room:
        sql += " AND room=?"
        values.append(room)
    if since:
        sql += " AND substr(received_at,1,19)>=?"
        values.append(since)
    if until:
        sql += " AND substr(received_at,1,19)<?"
        values.append(until)
    connection = open_readonly(path)
    try:
        rows = connection.execute(sql, values).fetchall()
    finally:
        connection.close()
    bot = [text for text, generated, _ in rows if generated]
    members = [text for text, generated, is_self in rows if not generated and not is_self]
    return bot, members


def percentile(sorted_values, fraction):
    """最近秩法分位数；空列表返回 None。"""
    if not sorted_values:
        return None
    index = max(0, math.ceil(fraction * len(sorted_values)) - 1)
    return sorted_values[index]


def length_stats(texts):
    lengths = sorted(len(text) for text in texts)
    if not lengths:
        return {"count": 0, "median": None, "p90": None, "blank_line_ratio": None}
    blank = sum(1 for text in texts if BLANK_LINE.search(text.strip()))
    return {"count": len(lengths), "median": statistics.median(lengths),
            "p90": percentile(lengths, 0.9), "blank_line_ratio": blank / len(lengths)}


def _word_chars(text):
    return all(unicodedata.category(char)[0] in "LN" for char in text)


def message_grams(text, low, high):
    """一条消息里出现过的字符 n-gram 集合；只取由字母/汉字/数字组成的片段，标点和空白处断开。"""
    grams = set()
    for run in re.split(r"[^\w]+|_", text):
        if not run or not _word_chars(run):
            continue
        for size in range(low, min(high, len(run)) + 1):
            for start in range(len(run) - size + 1):
                grams.add(run[start:start + size])
    return grams


def document_frequency(texts, low, high):
    counter = Counter()
    for text in texts:
        counter.update(message_grams(text, low, high))
    return counter


def phrase_ranking(bot_texts, member_texts, *, low=3, high=6, min_count=8, smoothing=0.002, top=40,
                   max_member_count=None):
    """机器人高频、群友几乎不用的短语，按机器人出现率与群友出现率之比降序。

    平滑项避免群友出现率为 0 时比值无限大，同时让更高频的短语排在前面。
    max_member_count 非 None 时，群友侧出现次数超过它的短语直接排除。
    同一串词里被更长短语包含且次数几乎相同的短片段会被合并掉。
    """
    if not bot_texts:
        return []
    bot_freq = document_frequency(bot_texts, low, high)
    member_freq = document_frequency(member_texts, low, high)
    bot_total, member_total = len(bot_texts), max(1, len(member_texts))
    scored = []
    for gram, count in bot_freq.items():
        if count < min_count:
            continue
        member_count = member_freq.get(gram, 0)
        if max_member_count is not None and member_count > max_member_count:
            continue
        ratio = (count / bot_total) / (member_count / member_total + smoothing)
        scored.append({"phrase": gram, "bot_count": count, "member_count": member_count, "ratio": ratio})
    # 合并被更长短语覆盖的片段：长短语次数不少于短片段的 80% 时，只保留长的。
    scored.sort(key=lambda item: (-len(item["phrase"]), -item["bot_count"]))
    kept = []
    for item in scored:
        if any(item["phrase"] in other["phrase"] and other["bot_count"] >= 0.8 * item["bot_count"]
               and other["member_count"] <= item["member_count"] + 1 for other in kept):
            continue
        kept.append(item)
    kept.sort(key=lambda item: (-item["ratio"], -item["bot_count"], item["phrase"]))
    return kept[:top]


def format_stat(stats):
    if stats["count"] == 0:
        return "0 条"
    return (f"{stats['count']} 条；长度中位数 {stats['median']:g}，p90 {stats['p90']}；"
            f"含空行分段 {stats['blank_line_ratio'] * 100:.1f}%")


def report(bot, members, args):
    lines = []
    window = f"{args.since or '开头'} ~ {args.until or '现在'}"
    lines.append(f"统计区间：{window}（UTC）" + (f"；room 限定" if args.room else ""))
    lines.append("机器人：" + format_stat(length_stats(bot)))
    lines.append("群友  ：" + format_stat(length_stats(members)))
    ranking = phrase_ranking(bot, members, low=args.ngram_min, high=args.ngram_max, min_count=args.min_count,
                             smoothing=args.smoothing, top=args.top, max_member_count=args.max_member_count)
    lines.append(f"机器人高频、群友几乎不用的短语（{args.ngram_min}—{args.ngram_max} 字，"
                 f"机器人至少 {args.min_count} 条，前 {args.top}）：")
    lines.append("  排名  机器人条数  群友条数  比值  短语")
    for index, item in enumerate(ranking, 1):
        lines.append(f"  {index:>3}  {item['bot_count']:>9}  {item['member_count']:>7}  "
                     f"{item['ratio']:>6.1f}  {item['phrase']}")
    if not ranking:
        lines.append("  （没有满足条件的短语，可降低 --min-count）")
    return "\n".join(lines)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 路径（只读打开）")
    parser.add_argument("--since", help="起始（含），ISO 日期或时间，无时区按 UTC")
    parser.add_argument("--until", help="截止（不含），格式同上")
    parser.add_argument("--room", help="只统计这个群（默认全部）")
    parser.add_argument("--min-count", type=int, default=8, help="机器人至少出现在多少条消息里才入选（默认 8）")
    parser.add_argument("--ngram-min", type=int, default=3, help="短语最短字数（默认 3）")
    parser.add_argument("--ngram-max", type=int, default=6, help="短语最长字数（默认 6）")
    parser.add_argument("--smoothing", type=float, default=0.002, help="比值分母平滑项（默认 0.002）")
    parser.add_argument("--max-member-count", type=int, default=None,
                        help="群友侧最多出现多少条（默认不限，仅靠比值排序）")
    parser.add_argument("--top", type=int, default=40, help="输出前多少个短语（默认 40）")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.min_count < 1 or args.top < 1 or not 1 <= args.ngram_min <= args.ngram_max <= 12 or args.smoothing <= 0:
        print("参数不合法：min-count/top ≥ 1，1 ≤ ngram-min ≤ ngram-max ≤ 12，smoothing > 0", file=sys.stderr)
        return 2
    try:
        since, until = normalize_bound(args.since), normalize_bound(args.until)
    except ValueError:
        print("--since/--until 需要 ISO 日期或时间", file=sys.stderr)
        return 2
    if not Path(args.db).exists():
        print("找不到数据库文件", file=sys.stderr)
        return 2
    bot, members = load_texts(args.db, since=since, until=until, room=args.room)
    print(report(bot, members, args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
