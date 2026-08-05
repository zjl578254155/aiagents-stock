#!/usr/bin/env python3
# ⚠️ 已迁移：本文件为 2026-08-05 冻结镜像，现行版本在 ~/.claude/skills/stock-portfolio-analysis/scripts/，不要再改这里。
"""
在场计划盘中盯价（eod_action_plans × 腾讯行情 × 飞书卡片）

背景：eod_action_plans 里的条件买卖点此前只在分析会话和 15:35 概况卡里对照，
盘中触价无任何实时提醒（2026-08-04 金发距 #6 触发线 0.7% 时用户发现空窗）。
本脚本由 cron 交易时段每 2 分钟执行，直接读 open 计划做真理源——新计划落库即自动纳入盯守。

触发语义（两级）：
  接近：距触发线 ≤1% → 📍 接近卡（blue，预热：给用户挂券商条件单的时间窗），每日一次
  TRIM  现价 >= 触发价 → 🎯 触价卡（red，需用户决策；"放量"类条件量能由用户自查）
  ADD   现价 <= 触发价 → 🎯 触价卡（red）
  EXIT  现价 <  触发价 → ⚠️ 破位预警卡（orange；D1=B 收盘价确认口径，盘中破位≠触发）
  无触发价的计划（如日期兜底类）不盯，由提醒表/分析会话管。触价卡发出后当日不再发接近卡。
同一计划每日只推一次（data/plan_watch_state.json 按日期记录）；非交易日靠行情时间戳自动静默。

用法: plan_watch.py [--dry]   # --dry 只打印不发送
"""
import json
import os
import sqlite3
import sys
from datetime import datetime

os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.insert(0, '.')

STATE_PATH = 'data/plan_watch_state.json'
DRY = '--dry' in sys.argv


def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict, today: str) -> None:
    state = {d: v for d, v in state.items() if d >= today}  # 只留当日，历史自动清
    with open(STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def _fetch_quotes(codes: list) -> dict:
    """腾讯接口直连。返回 {code: (现价, 今日涨跌%, 行情日期YYYYMMDD)}"""
    import requests
    qcodes = ','.join(('sh' if c.startswith('6') else 'sz') + c for c in codes)
    r = requests.get(f"https://qt.gtimg.cn/q={qcodes}", timeout=10,
                     proxies={'http': None, 'https': None})
    r.encoding = 'gbk'
    quotes = {}
    for line in r.text.splitlines():
        if '=' not in line:
            continue
        p = line.split('"')[1].split('~')
        if len(p) > 32:
            quotes[p[2]] = (float(p[3]), float(p[32]), p[30][:8])
    return quotes


def _send(content: str, title: str, level: str) -> None:
    if DRY:
        print(f"--- DRY [{level}] {title}\n{content}\n---")
        return
    from services.eod_analysis_service import _send_webhook
    _send_webhook(content, title=title, level=level)


def main():
    now = datetime.now()
    stamp = now.strftime('%Y-%m-%d %H:%M:%S')
    today = now.strftime('%Y-%m-%d')
    conn = sqlite3.connect('data/eod_analysis.db')
    conn.row_factory = sqlite3.Row
    plans = conn.execute(
        "SELECT id, code, direction, trigger_price, qty, condition_text FROM eod_action_plans "
        "WHERE status='open' AND trigger_price IS NOT NULL").fetchall()
    stocks = {r['code']: r for r in conn.execute(
        "SELECT code, name, quantity, cost_price FROM eod_watch_stocks WHERE enabled=1")}
    conn.close()
    if not plans:
        return

    try:
        quotes = _fetch_quotes(sorted({p['code'] for p in plans}))
    except Exception as e:
        print(f"[{stamp}] WARN 行情拉取失败: {e!r}")
        return
    state = _load_state()
    seen = state.setdefault(today, {})

    for p in plans:
        code = p['code']
        if code not in quotes or str(p['id']) in seen:
            continue
        px, day_pct, qdate = quotes[code]
        if qdate != now.strftime('%Y%m%d'):  # 非交易日/行情未开，静默
            continue
        trig = p['trigger_price']
        s = stocks.get(code)
        name = s['name'] if s else code
        hit = (p['direction'] == 'TRIM' and px >= trig) or \
              (p['direction'] == 'ADD' and px <= trig) or \
              (p['direction'] == 'EXIT' and px < trig)
        pos = (f"持仓 {s['quantity']} 股 · 成本 {s['cost_price']:.3f} · "
               f"现盈亏 {(px / s['cost_price'] - 1) * 100:+.1f}%") if s and s['quantity'] else '无持仓'
        if not hit:
            near = (p['direction'] == 'TRIM' and px >= trig * 0.99) or \
                   (p['direction'] in ('ADD', 'EXIT') and px <= trig * 1.01)
            nkey = f"near{p['id']}"
            if near and nkey not in seen:
                dist = (trig / px - 1) * 100
                _send(f"**计划#{p['id']}**：{p['direction']} @{trig:g}，现价 {px:.2f}（{day_pct:+.2f}%），距触发 {dist:+.1f}%\n{pos}\n---\n"
                      f"**你需要做什么**：可先在券商挂好条件单/限价单，免得尖峰行情来不及；真正越线时会再推确认卡\n{stamp}",
                      title=f"📍 接近触发 · {name} 距 {trig:g} 还差 {abs(dist):.1f}%", level='info')
                seen[nkey] = now.strftime('%H:%M')
                print(f"[{stamp}] 接近提醒 plan#{p['id']} {name} {p['direction']}@{trig:g} 现价{px:.2f}")
            continue
        cond = p['condition_text'][:60]
        if p['direction'] == 'EXIT':
            _send(f"**计划#{p['id']}**：EXIT 清仓 @{trig:g}（收盘价确认口径 D1=B）\n"
                  f"**现价**：{px:.2f}（{day_pct:+.2f}%）｜{pos}\n**条件原文**：{cond}\n---\n"
                  f"**你需要做什么**：盘中破位≠触发，收盘价确认才算；收盘后复盘对账，勿盘中抢跑\n{stamp}",
                  title=f"⚠️ 破位预警 · {name} 盘中跌破 {trig:g}", level='warn')
        else:
            act = '减' if p['direction'] == 'TRIM' else '买'
            _send(f"**计划#{p['id']}**：{p['direction']} {act}{p['qty'] or '?'}股 @{trig:g}\n"
                  f"**现价**：{px:.2f}（{day_pct:+.2f}%）｜{pos}\n**条件原文**：{cond}\n---\n"
                  f"**你需要做什么**：核对条件（量能等自查），决定是否按计划执行；成交后对 Claude 报一声即记账\n{stamp}",
                  title=f"🎯 触价 · {name} 到 {trig:g}", level='error')
        seen[str(p['id'])] = now.strftime('%H:%M')
        print(f"[{stamp}] 推送 plan#{p['id']} {name} {p['direction']}@{trig:g} 现价{px:.2f}")
        if p['direction'] in ('ADD', 'TRIM'):  # 触价后拉起高频跟踪判稳器（红卡当日一次，故仅拉起一次）
            import subprocess
            with open('data/plan_track.log', 'a') as logf:
                subprocess.Popen(
                    [sys.executable, '-u', 'scripts/plan_track.py', '--plan-id', str(p['id']),
                     '--code', code, '--name', name, '--direction', p['direction'],
                     '--trigger-price', str(trig)] + (['--dry'] if DRY else []),
                    stdout=logf, stderr=logf, start_new_session=True)
            print(f"[{stamp}] 已拉起 plan_track plan#{p['id']}")

    _save_state(state, today)


if __name__ == '__main__':
    main()
