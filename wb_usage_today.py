#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Print today's per-account token usage and current status for wb-proxy.

Read-only: this script never writes to the proxy, the logs, or the accounts.
It reads:
    usage/usage.jsonl
    usage/limit-events.jsonl
    accounts/*.json
    accounts/settings.json

By default only deepseek models are printed. Pass --all to include the
other models as well.

Usage:
    python wb_usage_today.py
    python wb_usage_today.py --all
    python wb_usage_today.py --limit 180000000
    python wb_usage_today.py --watch 10
    python wb_usage_today.py --active-minutes 10

Options:
    --all               Also print non-deepseek models (default: deepseek only).
    --limit N           Override both the daily and per-model limit for display.
    --active-minutes N  Treat an account as "serving" if it completed a request
                        in the last N minutes (default 5).
    --watch N           Refresh every N seconds (Ctrl+C to stop).
    --dir PATH          wb-proxy directory (default: this script's directory).
"""

import argparse
import glob
import json
import os
import sys
import time
import unicodedata
from datetime import datetime

SKIP_FILES = {"model.json", "settings.json", "active_realm.json"}


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def load_jsonl(path):
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return rows


def limit_for(settings, key, realm):
    """Effective limit for one realm: the realm override, else the global."""
    try:
        entry = (settings.get("limits") or {}).get(key) or {}
    except Exception:
        entry = {}
    value = entry.get(realm)
    if value is None:
        value = entry.get("global")
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def fmt_clock(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "-"


def fmt_int(value):
    return format(int(value), ",")


# (header, display width, alignment). The last column is not padded; it is the
# free-form status. Widths count terminal cells, not characters: a CJK glyph
# takes two cells, which is why plain "%-10s" misaligns the Chinese headers.
# One row per (account, model). This is the table that answers "which model
# hit its own 195M budget" - the account total alone cannot show that.
MODEL_COLUMNS = (
    ("账号", 10, "left"),
    ("昵称", 13, "left"),
    ("模型", 22, "left"),
    ("今日token", 16, "right"),
    ("距限额", 14, "right"),
    ("请求", 8, "right"),
    ("积分", 10, "right"),
    ("最后成功", 10, "left"),
    ("状态", 0, "left"),
)


def display_width(text):
    """Terminal cells occupied by `text` (CJK-wide glyphs count as 2)."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in str(text))


def truncate_display(text, width):
    """Cut `text` to at most `width` terminal cells, adding an ellipsis."""
    text = str(text)
    if width <= 0 or display_width(text) <= width:
        return text
    out = []
    used = 0
    for ch in text:
        cells = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + cells > width - 1:
            break
        out.append(ch)
        used += cells
    return "".join(out) + "…"


def cell(text, width, align="left"):
    """Pad `text` to `width` terminal cells."""
    text = truncate_display(text, width)
    pad = max(0, width - display_width(text)) if width > 0 else 0
    if align == "right":
        return " " * pad + text
    return text + " " * pad


def collect(base):
    now = time.time()
    midnight = datetime.now().replace(hour=0, minute=0, second=0,
                                      microsecond=0).timestamp()

    rows = load_jsonl(os.path.join(base, "usage", "usage.jsonl"))
    events = load_jsonl(os.path.join(base, "usage", "limit-events.jsonl"))
    settings = load_json(os.path.join(base, "accounts", "settings.json"), {}) or {}

    accounts = []
    for path in sorted(glob.glob(os.path.join(base, "accounts", "*.json"))):
        if os.path.basename(path) in SKIP_FILES:
            continue
        data = load_json(path)
        if isinstance(data, dict) and data.get("uid"):
            accounts.append(data)

    per_model = {}
    for row in rows:
        if row.get("outcome") != "completed" or not row.get("account"):
            continue
        if (row.get("at") or 0) < midnight:
            continue
        uid = row["account"]
        model = row.get("model") or "?"
        mstat = per_model.setdefault((uid, model), {
            "tokens": 0, "requests": 0, "credit": 0.0, "last": 0.0})
        mstat["tokens"] += row.get("total_tokens") or 0
        mstat["requests"] += 1
        mstat["credit"] += float(row.get("credit") or 0)
        mstat["last"] = max(mstat["last"], row.get("at") or 0)

    # Earliest future 6004 reset per (account, model). The event log is a
    # record; the proxy may have cleared the persisted cap when the account
    # was re-enabled, so this is informational - the "last OK" time is the
    # real signal.
    caps = {}
    for event in events:
        uid = event.get("account")
        model = event.get("model") or "?"
        reset = event.get("reset") or 0
        if not uid or reset <= now:
            continue
        key = (uid, model)
        caps[key] = min(caps.get(key, reset), reset)

    return now, midnight, accounts, per_model, caps, settings


def render(args):
    base = args.dir
    now, midnight, accounts, per_model, caps, settings = collect(base)

    print("截至 %s   今日统计起点 %s" % (
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        datetime.fromtimestamp(midnight).strftime("%Y-%m-%d %H:%M")))
    if args.limit:
        print("显示限额（命令行覆盖）：%s" % fmt_int(args.limit))
    else:
        daily = limit_for(settings, "daily_token_limit", "cn")
        model = limit_for(settings, "model_daily_token_limit", "cn")
        print("限额（cn）：每日 Token=%s   按模型 Token=%s" % (
            fmt_int(daily) if daily else "未启用",
            fmt_int(model) if model else "未启用"))
    print("“正在接单”判定窗口：最近 %d 分钟    |    Ctrl+C 退出" % args.active_minutes)
    print("")

    def print_model_row(values):
        parts = []
        for (_, width, align), value in zip(MODEL_COLUMNS[:-1], values[:-1]):
            parts.append(cell(str(value), width, align))
        print((" ".join(parts) + " " + str(values[-1])).rstrip())

    def model_line(account, model, mstat):
        uid = account["uid"]
        nickname = account.get("nickname") or ""
        realm = account.get("realm") or "cn"
        enabled = account.get("enabled", True) is not False
        model_limit = (args.limit if args.limit
                       else limit_for(settings, "model_daily_token_limit", realm))
        m_remaining = (model_limit - mstat["tokens"]) if model_limit > 0 else None
        m_cap = caps.get((uid, model))
        m_last = mstat["last"]
        if not enabled:
            m_status = "已停用"
        elif m_last >= now - args.active_minutes * 60:
            m_status = "正在接单"
        elif m_cap:
            m_status = "上游6004 恢复 " + fmt_clock(m_cap)
        elif m_remaining is not None and m_remaining <= 0:
            m_status = "本地限额已满"
        else:
            m_status = "空闲(可接单)"
        return [uid[:8], nickname, model, fmt_int(mstat["tokens"]),
                "不限" if m_remaining is None else fmt_int(m_remaining),
                mstat["requests"], "%.2f" % mstat["credit"],
                fmt_clock(m_last), m_status]

    def print_model_section(title, deepseek_only):
        lines = []
        for account in accounts:
            for (uid_, model), mstat in per_model.items():
                if uid_ != account["uid"]:
                    continue
                is_deepseek = "deepseek" in model.lower()
                if is_deepseek != deepseek_only:
                    continue
                lines.append((account, model, mstat))
        if not lines:
            return
        # deepseek 部分按用量降序（最能看出谁快用完）；其他模型按模型名、
        # 再按账号文件顺序，方便逐个模型对照。
        if deepseek_only:
            lines.sort(key=lambda item: -item[2]["tokens"])
        else:
            lines.sort(key=lambda item: (item[1].lower(), item[0]["uid"]))
        print(title)
        print_model_row([name for name, _, _ in MODEL_COLUMNS])
        print("-" * 150)
        for account, model, mstat in lines:
            print_model_row(model_line(account, model, mstat))
        print("")

    print_model_section("分模型明细 — deepseek 系列：", True)
    if args.all:
        print_model_section("分模型明细 — 其他模型：", False)
    print("注：")
    print("  1. “距限额”只按该模型的限额计算；每个模型各有独立额度。")
    print("  2. “上游6004”来自 usage/limit-events.jsonl 的未来恢复记录；")
    print("     代理重新启用账号时可能已清掉 modelCaps，是否真能接单以“最后成功”时间为准。")
    print("  3. 表格只统计 outcome=completed 且带账号的请求；被本地 429/502 拒绝的")
    print("     请求不计入 token，也不计入积分。")
    print("")


def main():
    parser = argparse.ArgumentParser(
        description="Print today's per-account token usage and current status.")
    parser.add_argument("--dir", default=os.path.dirname(os.path.abspath(__file__)),
                        help="wb-proxy directory (default: this script's directory)")
    parser.add_argument("--limit", type=int, default=0,
                        help="override daily/model limit for display (e.g. 180000000)")
    parser.add_argument("--active-minutes", type=int, default=5,
                        help="an account counts as serving if it completed a request "
                             "in the last N minutes (default 5)")
    parser.add_argument("--watch", type=float, default=0,
                        help="refresh every N seconds (0 = print once)")
    parser.add_argument("--all", action="store_true",
                        help="also print non-deepseek models (default: deepseek only)")
    args = parser.parse_args()

    if args.watch and args.watch > 0:
        try:
            while True:
                os.system("cls" if os.name == "nt" else "clear")
                render(args)
                time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\n已停止。")
    else:
        render(args)


if __name__ == "__main__":
    main()
