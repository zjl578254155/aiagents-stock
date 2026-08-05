#!/usr/bin/env python3
"""
EOD 快照采集兜底任务（独立于 Streamlit 进程）

背景：6 个调度器都跑在 Streamlit 的 daemon 线程里，Streamlit 一退定时任务静默消亡，
2026-06~07 采集覆盖率仅约 50%。本脚本由 cron 每交易日 15:35 独立执行，
成功标准 = 当日快照文件生成且非空；失败推飞书 webhook 告警（需 .env WEBHOOK_ENABLED=true）。

cron 示例:
    35 15 * * 1-5 cd <项目根> && .venv/bin/python scripts/eod_export_cron.py >> logs/eod_cron.log 2>&1

局限：仅按周一~周五判断，法定节假日不跳过（当日会生成一份与前一交易日相同的快照，无害）。
"""
import json
import os
import sys
from datetime import datetime

os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.insert(0, '.')


def _ensure_tdx() -> bool:
    """TDX 预检自愈：探测不通则拉起本地 docker 容器 tdx-api（服务加载代码表约需 1 分钟），仍不通返回 False"""
    import subprocess
    import time
    import requests
    from services.eod_analysis_service import TDX_BASE_URL

    def probe() -> bool:
        r = requests.get(f"{TDX_BASE_URL}/api/kline", params={'code': '600143', 'type': 'day'},
                         timeout=5, proxies={'http': None, 'https': None})
        return r.json().get('code') == 0

    try:
        if probe():
            return True
    except Exception:
        pass
    subprocess.run(['docker', 'start', 'tdx-api'], capture_output=True)
    for _ in range(14):
        time.sleep(5)
        try:
            if probe():
                return True
        except Exception:
            continue
    return False


def _push_portfolio_summary(stamp: str) -> None:
    """采集成功后附发"持仓收盘概况"卡片。行情走腾讯接口（硬口径纪律），失败则跳过并打 WARN。"""
    import sqlite3
    import requests
    from services.eod_analysis_service import _send_webhook, ACTION_ICONS

    conn = sqlite3.connect('data/eod_analysis.db')
    conn.row_factory = sqlite3.Row
    stocks = conn.execute(
        "SELECT code, name, quantity, cost_price FROM eod_watch_stocks WHERE enabled=1 AND quantity>0").fetchall()
    actions = {r['code']: r['action_code'] for r in conn.execute(
        "SELECT code, action_code, MAX(trade_date) FROM eod_analysis_records GROUP BY code")}
    plans = conn.execute(
        "SELECT code, direction, trigger_price, qty, condition_text FROM eod_action_plans "
        "WHERE status='open' ORDER BY code, id").fetchall()
    conn.close()
    if not stocks:
        return

    qcodes = ','.join(('sh' if s['code'].startswith('6') else 'sz') + s['code'] for s in stocks)
    try:
        r = requests.get(f"https://qt.gtimg.cn/q={qcodes}", timeout=10,
                         proxies={'http': None, 'https': None})
        r.encoding = 'gbk'
        live = {}
        for line in r.text.splitlines():
            if '=' not in line:
                continue
            p = line.split('"')[1].split('~')
            if len(p) > 32:
                live[p[2]] = (float(p[3]), float(p[32]))  # code: (现价, 今日涨跌%)
        if len(live) < len(stocks):
            raise RuntimeError(f"行情缺口 {len(live)}/{len(stocks)}")
    except Exception as e:
        print(f"[{stamp}] WARN 持仓概况推送跳过（行情拉取失败: {e!r}）")
        return

    rows = sorted(stocks, key=lambda s: -live[s['code']][0] * s['quantity'])
    total_mv = sum(live[s['code']][0] * s['quantity'] for s in rows)
    total_pnl = sum((live[s['code']][0] - s['cost_price']) * s['quantity'] for s in rows)
    names = {s['code']: s['name'] for s in rows}
    lines = [f"**组合**：市值 {total_mv / 10000:.1f}万 · 总盈亏 {total_pnl / 10000:+.1f}万"]
    for s in rows:
        px, day_pct = live[s['code']]
        pnl = (px / s['cost_price'] - 1) * 100
        icon = ACTION_ICONS.get(actions.get(s['code'], ''), '⚪')
        lines.append(f"{icon} {s['name']}  {px:.2f} ({day_pct:+.2f}%) · 持仓 {pnl:+.1f}%")
    plan_lines = []
    for p in plans:
        name = names.get(p['code'], p['code'])
        if p['trigger_price']:
            dist = (p['trigger_price'] / live[p['code']][0] - 1) * 100 if p['code'] in live else None
            dist_str = f"（距触发 {dist:+.1f}%）" if dist is not None else ''
            plan_lines.append(f"· {name} {p['direction']} {p['trigger_price']:g} × {p['qty'] or '?'}{dist_str}")
        else:
            plan_lines.append(f"· {name} {p['direction']} × {p['qty'] or '?'}（{p['condition_text'][:20]}…）")
    content = '\n'.join(lines)
    if plan_lines:
        content += "\n---\n**在场计划**（现价距触发价）：\n" + '\n'.join(plan_lines)
    content += f"\n---\n**你需要做什么**：无；想看完整分析对 Claude 说「持仓分析」\n{stamp}"
    _send_webhook(content, title=f"📊 持仓收盘概况 · {stamp[5:10]}", level='info')


def main():
    stamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if datetime.now().weekday() >= 5:
        print(f"[{stamp}] 周末，跳过")
        return
    from services.eod_analysis_service import export_snapshots, _send_webhook
    try:
        tdx_ok = _ensure_tdx()
    except Exception as e:  # 预检自身崩溃（如 cron PATH 缺 docker）也必须告警，不得静默
        tdx_ok = False
        _send_webhook(f"**出了什么事**：数据备份程序自身报错，今天的收盘数据没备份上\n**技术细节**：{e!r}\n---\n"
                      f"**你需要做什么**：对 Claude 说「排查EOD采集」\n{stamp}",
                      title='🔴 持仓数据备份出错', level='error')
        print(f"[{stamp}] WARN TDX 预检异常: {e!r}")
    if not tdx_ok:
        _send_webhook(f"**出了什么事**：行情数据服务(TDX)没能启动，本次备份只能用旧数据\n---\n"
                      f"**你需要做什么**：对 Claude 说「排查EOD采集」\n{stamp}",
                      title='🔴 行情服务启动失败', level='error')
        print(f"[{stamp}] WARN TDX 自愈失败，继续采集（K线将为陈旧缓存）")
    today = datetime.now().strftime('%Y%m%d')
    try:
        path = export_snapshots()
        size = os.path.getsize(path)
        with open(path, encoding='utf-8') as f:
            snaps = json.load(f)
        stale = [s['code'] for s in snaps if s.get('data_stale')]
        if today not in os.path.basename(path) or size < 2000 or not snaps:
            raise RuntimeError(f"快照产出异常: path={path} size={size} 只数={len(snaps)}")
        msg = f"[{stamp}] OK {path} {len(snaps)}只 {size}B"
        if stale:
            msg += f"  ⚠️ K线滞后(未含今日bar): {','.join(stale)}"
            stale_names = [s['name'] for s in snaps if s.get('data_stale')]
            scope = '全部持仓' if len(stale) == len(snaps) else '、'.join(stale_names)
            _send_webhook(f"**这是什么**：每个交易日收盘后，系统自动备份 {len(snaps)} 只持仓的数据（供复盘分析用）\n"
                          f"**本次结果**：✅ 备份成功。但数据源还没更新今天的行情（{scope}），属常见现象，一般当晚自动落地\n---\n"
                          f"**你需要做什么**：无。下次分析时会自动用实时行情补上，不影响任何结论\n{stamp}",
                          title='✅ 持仓数据已备份（今日行情稍后落地）', level='warn')
        print(msg)
        _push_portfolio_summary(stamp)
    except Exception as e:
        print(f"[{stamp}] FAIL {e!r}")
        _send_webhook(f"**出了什么事**：今天的持仓收盘数据备份失败\n**技术细节**：{e!r}\n---\n"
                      f"**你需要做什么**：对 Claude 说「排查EOD采集」\n{stamp}",
                      title='🔴 持仓数据备份失败', level='error')
        sys.exit(1)
    finally:
        # 按需启停：采集结束即关停 TDX 容器，不留空跑（unless-stopped 尊重手动 stop，不会拉回来）
        import subprocess
        subprocess.run(['docker', 'stop', 'tdx-api'], capture_output=True)
        print(f"[{stamp}] TDX 容器已按需关停")


if __name__ == '__main__':
    main()
