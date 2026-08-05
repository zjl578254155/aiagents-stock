#!/usr/bin/env python3
"""周复盘记分牌：持仓周收益（等权粗口径）vs 沪深300/创业板指。

背景（2026-08-05 框架审计）：体系运行 60 天从未产出过"组合 vs 基准"数字，
跑输基准不触发任何框架层动作。本脚本是定期复盘SOP 1a 的记分牌硬产出。

口径声明：等权、忽略调仓时序、不含现金/费用/股息——趋势对照用，非精确核算。
用法：python3 scripts/weekly_scoreboard.py [--since 2026-07-29]（默认 7 天前）
"""
import argparse
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import requests

DB = Path(__file__).resolve().parent.parent / "data" / "eod_analysis.db"
NOPROXY = {"http": None, "https": None}
UA = {"User-Agent": "Mozilla/5.0"}
INDEXES = [("sh000300", "沪深300"), ("sz399006", "创业板指"), ("sh000001", "上证指数")]


def qcode(code):
    return ("sh" if code.startswith(("60", "68")) else "sz") + code


def base_close(cur, code, since):
    row = cur.execute(
        "SELECT trade_date, close FROM eod_kline_cache WHERE code=? AND trade_date<=? "
        "ORDER BY trade_date DESC LIMIT 1", (code, since)).fetchone()
    return row  # (date, close) or None


def live_prices(codes):
    url = "http://qt.gtimg.cn/q=" + ",".join(qcode(c) for c in codes)
    r = requests.get(url, timeout=10, proxies=NOPROXY, headers=UA)
    r.encoding = "gbk"
    out = {}
    for line in r.text.strip().split(";"):
        if "=" not in line:
            continue
        f = line.split('="')[1].split("~")
        out[f[2]] = (f[1], float(f[3]))
    return out


def index_range(idx, since):
    today = datetime.now().strftime("%Y-%m-%d")
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"param={idx},day,{since},{today},320,qfq")
    d = requests.get(url, timeout=10, proxies=NOPROXY, headers=UA).json()["data"][idx]
    kl = d.get("qfqday") or d.get("day")
    if not kl:
        return None
    first, last = kl[0], kl[-1]
    return first[0], float(first[2]), last[0], float(last[2])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=(datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"),
                    help="基期日期（取该日或之前最近的缓存交易日收盘）")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = con.cursor()
    holds = cur.execute(
        "SELECT code, name, quantity, cost_price FROM eod_watch_stocks "
        "WHERE enabled=1 AND quantity>0 ORDER BY code").fetchall()
    if not holds:
        print("无持仓（enabled=1 且 quantity>0）")
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    px = live_prices([h[0] for h in holds])
    print(f"# 组合记分牌  基期<= {args.since}  现价取数 {now}（腾讯接口直连）")
    print("口径：等权粗口径，忽略调仓时序，不含现金/费用/股息\n")
    print(f"{'代码':<8}{'名称':<6}{'基期收盘':>9}{'现价':>8}{'区间%':>8}{'持仓盈亏%':>9}")

    rets = []
    for code, name, qty, cost in holds:
        base = base_close(cur, code, args.since)
        _, p = px[code]
        if base is None:
            print(f"{code:<8}{name:<6}{'缓存缺':>9}{p:>8}{'n/a':>8}{(p/cost-1)*100:>8.1f}%")
            continue
        r = (p / base[1] - 1) * 100
        rets.append(r)
        print(f"{code:<8}{name:<6}{base[1]:>9}{p:>8}{r:>7.1f}%{(p/cost-1)*100:>8.1f}%")

    port = sum(rets) / len(rets) if rets else float("nan")
    print(f"\n组合等权区间收益: {port:+.2f}%  （{len(rets)}/{len(holds)} 只有基期数据）")
    for idx, iname in INDEXES:
        rng = index_range(idx, args.since)
        if rng:
            d0, c0, d1, c1 = rng
            ir = (c1 / c0 - 1) * 100
            print(f"{iname}: {d0}={c0} -> {d1}={c1}  {ir:+.2f}%   组合超额 {port-ir:+.2f}pct")
    con.close()


if __name__ == "__main__":
    main()
