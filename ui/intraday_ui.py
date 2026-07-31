"""
日内实时交易信号 UI
"""

import streamlit as st

from schedulers.intraday_scheduler import intraday_scheduler
from services.intraday_signal_service import (
    get_all_states, get_current_session, get_today_signals, refresh_all,
)

URGENCY_COLOR = {'HIGH': '🔴', 'MEDIUM': '🟡', 'LOW': '⚪'}
URGENCY_BADGE = {'HIGH': ':red[HIGH]', 'MEDIUM': ':orange[MEDIUM]', 'LOW': ':gray[LOW]'}
SESSION_LABEL = {
    'PRE': '🕒 集合竞价观察期',
    'OPEN_UNSTABLE': '⚡ 开盘不稳定区（9:30–10:00）',
    'MAIN': '✅ 主要交易期',
    'TAIL': '🔔 尾盘窗口（14:30–14:55）',
    'AUCTION_CLOSE': '🔒 尾盘集合竞价（不可撤单）',
    'CLOSED': '🌙 非交易时段',
}
EOD_COLOR = {'HOLD': '🟢', 'TRIM': '🔵', 'WATCH': '🟡', 'EXIT': '🔴', 'UNKNOWN': '⚪'}


def _tab_signals():
    """Tab1：今日信号列表"""
    col_refresh, col_status = st.columns([1, 2])
    with col_refresh:
        if st.button("立即刷新", type="primary", use_container_width=True):
            with st.spinner("正在拉取实时数据..."):
                refresh_all()
            st.rerun()
    with col_status:
        session = get_current_session()
        st.info(SESSION_LABEL.get(session, session))

    signals = get_today_signals()
    if not signals:
        st.caption("今日暂无信号。非交易时段或数据未刷新。")
        return

    # 统计
    high = sum(1 for s in signals if s['urgency'] == 'HIGH')
    mid = sum(1 for s in signals if s['urgency'] == 'MEDIUM')
    low = sum(1 for s in signals if s['urgency'] == 'LOW')
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("今日信号总数", len(signals))
    c2.metric("🔴 HIGH", high)
    c3.metric("🟡 MEDIUM", mid)
    c4.metric("⚪ LOW", low)

    st.divider()

    for sig in signals:
        icon = URGENCY_COLOR.get(sig['urgency'], '⚪')
        eod_icon = EOD_COLOR.get(sig['eod_action'], '⚪')
        title = (f"{icon} {sig['time']} | {sig['name']}({sig['code']}) | "
                 f"{sig['type']} | EOD={eod_icon}{sig['eod_action']}")
        with st.expander(title, expanded=(sig['urgency'] == 'HIGH')):
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("价格", f"¥{sig['price']:.2f}")
            chg = sig['change_pct']
            c2.metric("涨跌幅", f"{chg:+.2f}%", delta_color="normal")
            vr = sig['volume_ratio']
            c3.metric("量比", f"{vr:.2f}" if vr else "—")
            vd = sig['vwap_deviation']
            c4.metric("VWAP偏离", f"{vd:+.1f}%" if vd else "—")

            st.markdown(f"**推荐操作**：{sig['recommendation']}")
            st.caption(sig['reasoning'])


def _tab_overview():
    """Tab2：持仓概览（所有股票当前状态）"""
    col_r, _ = st.columns([1, 3])
    with col_r:
        if st.button("刷新全部", type="primary", use_container_width=True):
            with st.spinner("刷新中..."):
                refresh_all()
            st.rerun()

    states = get_all_states()
    if not states:
        st.caption("暂无数据，请先点击「刷新全部」。")
        return

    rows = []
    for code, s in states.items():
        chg = s.get('change_pct')
        vr = s.get('volume_ratio')
        vwap = s.get('vwap')
        price = s.get('last_price')
        vwap_dev = round((price - vwap) / vwap * 100, 2) if price and vwap and vwap > 0 else None
        eod = s.get('eod_action', 'UNKNOWN')
        sig_count = len(s.get('today_signals', []))
        high_count = sum(1 for sg in s.get('today_signals', []) if sg['urgency'] == 'HIGH')
        rows.append({
            '代码': code,
            '名称': s.get('name', ''),
            '现价': f"¥{price:.2f}" if price else '—',
            '涨跌%': f"{chg:+.2f}" if chg is not None else '—',
            '量比': f"{vr:.2f}" if vr else '—',
            'VWAP偏离%': f"{vwap_dev:+.1f}" if vwap_dev is not None else '—',
            'EOD': f"{EOD_COLOR.get(eod,'⚪')}{eod}",
            '今日信号': sig_count,
            '🔴HIGH': high_count,
            '更新时间': s.get('last_update', '—'),
        })

    import pandas as pd
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)


def _tab_scheduler():
    """Tab3：调度器设置"""
    status = intraday_scheduler.get_status()

    c1, c2, c3 = st.columns(3)
    c1.metric("调度状态", "运行中 ✅" if status['is_running'] else "已停止 ⏸")
    c2.metric("轮询间隔", f"{status['interval']}秒")
    c3.metric("当前时段", SESSION_LABEL.get(status['session'], status['session']))

    st.caption(f"上次运行：{status['last_run_time'] or '—'}　结果：{status['last_run_result'] or '—'}")

    st.divider()

    new_interval = st.number_input(
        "轮询间隔（秒，建议 300=5分钟）",
        min_value=60, max_value=1800,
        value=status['interval'], step=60
    )

    col_start, col_stop, col_now = st.columns(3)
    with col_start:
        if st.button("启动调度器", type="primary",
                     disabled=status['is_running'], use_container_width=True):
            intraday_scheduler.interval = new_interval
            intraday_scheduler.start()
            st.success("调度器已启动")
            st.rerun()
    with col_stop:
        if st.button("停止调度器", type="secondary",
                     disabled=not status['is_running'], use_container_width=True):
            intraday_scheduler.stop()
            st.success("调度器已停止")
            st.rerun()
    with col_now:
        if st.button("立即执行一次", use_container_width=True):
            msg = intraday_scheduler.run_now()
            st.success(msg)

    st.divider()
    st.markdown("""
**信号触发条件说明**

| 信号类型 | 触发条件 | 语义 |
|---|---|---|
| GAP | 开盘 vs 昨收 ≥ ±3% | 隔夜重大变化 |
| BREAKOUT | 价创日高 且 量比 ≥ 2 | 主力进场趋势加速 |
| VWAP_DEV | 价偏离VWAP ≥ ±3% | 偏离日内成本中枢 |
| PM_WEAK | 13:30后由正转负 | 午后无承接走弱 |
| TAIL_SURGE | 14:30–14:55，量比≥3且5min内±2% | 机构尾盘建仓/出货 |
""")


def display_intraday():
    st.title("📈 日内实时交易信号")
    st.caption("基于 TDX 实时报价 + 5分钟K线，结合 EOD 决策提供时机建议。EOD 决定做什么，日内决定什么时候做。")

    tab1, tab2, tab3 = st.tabs(["今日信号", "持仓概览", "调度设置"])
    with tab1:
        _tab_signals()
    with tab2:
        _tab_overview()
    with tab3:
        _tab_scheduler()
