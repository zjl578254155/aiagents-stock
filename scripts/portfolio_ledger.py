#!/usr/bin/env python3
"""
买卖点计划 + 成交流水台账脚本（eod_action_plans / eod_trades 的唯一写入口）

与 save_decisions.py 同纪律：会话不得内联写这两张表，一律走本脚本（带回读校验）。

用法:
  plan add   --code 601012 --direction TRIM --condition "放量冲13.5减1000" \
             [--price 13.5] [--qty 1000] [--date 2026-07-31] [--record-id 224]
  plan close --id 3 --status hit_executed|hit_missed|expired|cancelled [--note "..."]
  plan list  [--all]                    # 默认只列 open
  trade add  --code 600143 --direction SELL --price 15.30 --qty 1500 \
             [--date 2026-07-31] [--note "..."] [--plan-id 1] [--no-ledger]
             # 默认联动：SELL 减台账数量(成本不变)、BUY 加权摊薄成本并加数量；
             # --plan-id 同时把计划置 hit_executed；--no-ledger 只记流水不动台账(历史回填用)
  report                                # open 计划 × 台账 × 腾讯实时价对照表
"""
import argparse
import sqlite3
import sys
from datetime import date

sys.path.insert(0, '.')
import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

DB = 'data/eod_analysis.db'


def _conn():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def plan_add(a):
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO eod_action_plans (code, plan_date, direction, condition_text, trigger_price, qty, record_id) "
        "VALUES (?,?,?,?,?,?,?)",
        (a.code, a.date or date.today().isoformat(), a.direction, a.condition, a.price, a.qty, a.record_id))
    conn.commit()
    row = conn.execute("SELECT * FROM eod_action_plans WHERE id=?", (cur.lastrowid,)).fetchone()
    print(f"[OK] plan#{row['id']} {row['code']} {row['direction']} @{row['trigger_price']} x{row['qty']} | {row['condition_text']}")
    conn.close()


def plan_close(a):
    conn = _conn()
    conn.execute("UPDATE eod_action_plans SET status=?, close_date=?, close_note=? WHERE id=?",
                 (a.status, date.today().isoformat(), a.note or '', a.id))
    conn.commit()
    row = conn.execute("SELECT * FROM eod_action_plans WHERE id=?", (a.id,)).fetchone()
    if not row or row['status'] != a.status:
        sys.exit(f"[FAIL] plan#{a.id} 状态回读不符")
    print(f"[OK] plan#{a.id} {row['code']} → {row['status']} ({row['close_note']})")
    conn.close()


def plan_list(a):
    conn = _conn()
    sql = "SELECT * FROM eod_action_plans" + ("" if a.all else " WHERE status='open'") + " ORDER BY id"
    for r in conn.execute(sql):
        print(f"#{r['id']} [{r['status']}] {r['plan_date']} {r['code']} {r['direction']} "
              f"@{r['trigger_price']} x{r['qty']} | {r['condition_text']} {r['close_note'] or ''}")
    conn.close()


def trade_add(a):
    conn = _conn()
    amount = round(a.price * a.qty, 2)
    cur = conn.execute(
        "INSERT INTO eod_trades (trade_date, code, direction, price, qty, amount, note, plan_id) VALUES (?,?,?,?,?,?,?,?)",
        (a.date or date.today().isoformat(), a.code, a.direction, a.price, a.qty, amount, a.note or '', a.plan_id))
    trade_id = cur.lastrowid

    ledger_msg = "台账未联动(--no-ledger)"
    if not a.no_ledger:
        stock = conn.execute("SELECT * FROM eod_watch_stocks WHERE code=?", (a.code,)).fetchone()
        if stock:
            old_qty, old_cost = stock['quantity'] or 0, stock['cost_price'] or 0
            if a.direction == 'BUY':
                new_qty = old_qty + a.qty
                new_cost = round((old_cost * old_qty + a.price * a.qty) / new_qty, 3) if new_qty else a.price
            else:
                new_qty, new_cost = old_qty - a.qty, old_cost  # 减仓成本不变（既有惯例）
            conn.execute("UPDATE eod_watch_stocks SET quantity=?, cost_price=?, updated_at=CURRENT_TIMESTAMP WHERE code=?",
                         (new_qty, new_cost, a.code))
            ledger_msg = f"台账 {old_qty}股@{old_cost} → {new_qty}股@{new_cost}"
        else:
            ledger_msg = "台账无此code(转债/场外)，仅记流水"
    if a.plan_id:
        conn.execute("UPDATE eod_action_plans SET status='hit_executed', close_date=?, close_note=? WHERE id=?",
                     (a.date or date.today().isoformat(), f"trade#{trade_id} 成交", a.plan_id))
    conn.commit()
    row = conn.execute("SELECT * FROM eod_trades WHERE id=?", (trade_id,)).fetchone()
    if not row:
        sys.exit("[FAIL] 流水回读失败")
    print(f"[OK] trade#{trade_id} {row['trade_date']} {row['code']} {row['direction']} {row['qty']}股@{row['price']} 金额{row['amount']} | {ledger_msg}")
    conn.close()


def report(_a):
    import requests
    conn = _conn()
    plans = list(conn.execute(
        "SELECT p.*, w.name, w.cost_price FROM eod_action_plans p LEFT JOIN eod_watch_stocks w ON p.code=w.code "
        "WHERE p.status='open' ORDER BY p.code"))
    if not plans:
        print("无 open 计划")
        return
    codes = sorted({('sh' if p['code'].startswith('6') else 'sz') + p['code'] for p in plans})
    price_map = {}
    try:
        resp = requests.get(f"https://qt.gtimg.cn/q={','.join(codes)}", timeout=10,
                            proxies={'http': None, 'https': None})
        resp.encoding = 'gbk'
        for line in resp.text.strip().split(';'):
            if '=' in line:
                f = line.split('=', 1)[1].strip('"').split('~')
                if len(f) > 4:
                    price_map[f[2]] = float(f[3])
    except Exception as e:
        print(f"[WARN] 行情获取失败: {e}")
    print(f"{'#':<4}{'代码':<8}{'名称':<8}{'方向':<6}{'触发价':<8}{'现价':<8}{'距离':<9}{'计划'}")
    for p in plans:
        cur_price = price_map.get(p['code'])
        dist = f"{(cur_price - p['trigger_price']) / p['trigger_price'] * 100:+.1f}%" \
            if cur_price and p['trigger_price'] else '-'
        print(f"{p['id']:<4}{p['code']:<8}{p['name'] or '-':<8}{p['direction']:<6}"
              f"{p['trigger_price'] or '-':<8}{cur_price or '-':<8}{dist:<9}{p['condition_text']}")
    conn.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('plan')
    ps = p.add_subparsers(dest='sub', required=True)
    pa = ps.add_parser('add')
    pa.add_argument('--code', required=True); pa.add_argument('--direction', required=True, choices=['TRIM', 'EXIT', 'ADD'])
    pa.add_argument('--condition', required=True); pa.add_argument('--price', type=float)
    pa.add_argument('--qty', type=int); pa.add_argument('--date'); pa.add_argument('--record-id', type=int)
    pa.set_defaults(func=plan_add)
    pc = ps.add_parser('close')
    pc.add_argument('--id', type=int, required=True)
    pc.add_argument('--status', required=True, choices=['hit_executed', 'hit_missed', 'expired', 'cancelled'])
    pc.add_argument('--note'); pc.set_defaults(func=plan_close)
    pl = ps.add_parser('list'); pl.add_argument('--all', action='store_true'); pl.set_defaults(func=plan_list)

    t = sub.add_parser('trade')
    ts = t.add_subparsers(dest='sub', required=True)
    ta = ts.add_parser('add')
    ta.add_argument('--code', required=True); ta.add_argument('--direction', required=True, choices=['BUY', 'SELL'])
    ta.add_argument('--price', type=float, required=True); ta.add_argument('--qty', type=int, required=True)
    ta.add_argument('--date'); ta.add_argument('--note'); ta.add_argument('--plan-id', type=int)
    ta.add_argument('--no-ledger', action='store_true')
    ta.set_defaults(func=trade_add)

    r = sub.add_parser('report'); r.set_defaults(func=report)

    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
