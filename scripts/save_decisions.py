#!/usr/bin/env python3
"""
决策批量写库脚本（幂等 + 写后回读校验）

背景：EOD 决策写库此前由 Claude 会话内联执行，会话输出中断即丢数据且日志无从对账
（2026-07-27 一次、2026-07-31 三次写库失败的事故根因）。自本脚本起，
Claude 会话只负责生成 JSON 决策文件，落库一律走本脚本，禁止会话内联写库。

用法:
    .venv/bin/python scripts/save_decisions.py <decisions.json> [--snapshot <snapshot.json>]

decisions.json 格式（数组，每项一只股票）:
    {
      "code": "002594",
      "trade_date": "2026-07-31",           # 必填：显式指定，绝不隐式取快照日期（快照可能滞后）
      "trigger_type": "manual",             # 可选，默认 manual
      "overrides": {"close_price": 95.77},  # 可选：覆盖快照字段，如用行情接口实测收盘价校正滞后快照
      "decision": {"action_code": "HOLD", "action": "...", "confidence": 8,
                    "margin_of_safety": "...", "thesis_still_valid": true, "reasoning": "..."}
    }

--snapshot 不传时自动取 data/eod_snapshots/ 最新一份。
幂等：同 (code, trade_date, trigger_type) 重跑为 INSERT OR REPLACE 覆盖，不产生重复行。
退出码：全部落库且回读校验通过 = 0，任何一条缺失 = 1。
"""
import json
import os
import sqlite3
import sys

os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.insert(0, '.')

from db.eod_analysis_db import eod_analysis_db  # noqa: E402
from services.eod_analysis_service import save_decision, SNAPSHOT_DIR  # noqa: E402


def latest_snapshot_path() -> str:
    files = sorted(f for f in os.listdir(SNAPSHOT_DIR)
                   if f.startswith('snapshot_') and f.endswith('.json'))
    if not files:
        sys.exit('[FAIL] data/eod_snapshots/ 下无快照文件')
    return os.path.join(SNAPSHOT_DIR, files[-1])


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    decisions_path = sys.argv[1]
    snap_path = (sys.argv[sys.argv.index('--snapshot') + 1]
                 if '--snapshot' in sys.argv else latest_snapshot_path())

    with open(decisions_path, encoding='utf-8') as f:
        items = json.load(f)
    with open(snap_path, encoding='utf-8') as f:
        snapshots = {s['code']: s for s in json.load(f)}
    print(f"[INFO] 快照: {snap_path}")
    print(f"[INFO] 决策: {decisions_path}（{len(items)} 条）")

    written = []
    for item in items:
        code, trade_date = item['code'], item['trade_date']
        snap = dict(snapshots.get(code) or {})
        if not snap:
            print(f"[WARN] {code} 不在快照中，用持仓台账最小快照兜底")
            stock = eod_analysis_db.get_watch_stock(code) or {}
            snap = {'name': stock.get('name', code), 'cost_price': stock.get('cost_price')}
        snap['trade_date'] = trade_date
        snap['trigger_type'] = item.get('trigger_type', 'manual')
        overrides = item.get('overrides') or {}
        snap.update(overrides)
        if 'close_price' in overrides:
            cost = snap.get('cost_price') or 0
            close = snap.get('close_price') or 0
            snap['pnl_pct'] = round((close - cost) / cost * 100, 2) if cost > 0 else None
        save_decision(code, snap, item['decision'])
        written.append((code, trade_date, snap['trigger_type']))

    print('\n=== 写后回读校验 ===')
    ok = True
    conn = sqlite3.connect('data/eod_analysis.db')
    for code, trade_date, trigger_type in written:
        row = conn.execute(
            "SELECT id, action_code, close_price, pnl_pct FROM eod_analysis_records "
            "WHERE code=? AND trade_date=? AND trigger_type=?",
            (code, trade_date, trigger_type)).fetchone()
        if row:
            print(f"[OK]   {code} {trade_date} → id={row[0]} {row[1]} close={row[2]} pnl={row[3]}%")
        else:
            ok = False
            print(f"[FAIL] {code} {trade_date} 未落库！")
    conn.close()
    if not ok:
        sys.exit(1)
    print(f"\n[DONE] {len(written)}/{len(items)} 条全部落库并回读校验通过")


if __name__ == '__main__':
    main()
