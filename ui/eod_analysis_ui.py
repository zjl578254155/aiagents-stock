"""
EOD持仓逻辑评估 UI

4个Tab：
  Tab1 - 最新分析总览（仪表盘）
  Tab2 - 观察股票管理（增删/买入逻辑）
  Tab3 - 历史分析记录（单股走势图）
  Tab4 - 调度器设置
"""

import streamlit as st
import pandas as pd
from datetime import datetime

from db.eod_analysis_db import eod_analysis_db
from schedulers.eod_scheduler import eod_scheduler

ACTION_ICONS = {'HOLD': '🟢', 'TRIM': '🔵', 'WATCH': '🟡', 'EXIT': '🔴'}
MOAT_TYPES = ['品牌', '成本领先', '网络效应', '转换成本', '牌照/特许', '其他']


def _action_badge(code: str) -> str:
    return f"{ACTION_ICONS.get(code, '⚪')} {code}"


# ==================== Tab1: 最新分析总览 ====================

def _tab_dashboard():
    st.subheader("持仓逻辑评估 — 最新结论")

    col_refresh, col_kline = st.columns([1, 1])
    with col_refresh:
        if st.button("刷新数据并导出快照", type="primary", use_container_width=True):
            msg = eod_scheduler.run_now()
            st.success(msg + "  完成后请让 Claude 读取快照分析并写回决策。")
    with col_kline:
        if st.button("仅刷新K线缓存", use_container_width=True):
            msg = eod_scheduler.refresh_kline_only()
            st.info(msg)

    st.markdown("---")

    records = eod_analysis_db.get_all_latest_records()
    watch_stocks = {s['code']: s for s in eod_analysis_db.get_all_watch_stocks(enabled_only=False)}

    if not records:
        stocks = eod_analysis_db.get_all_watch_stocks()
        if not stocks:
            st.info("暂无观察股票，请在「股票管理」Tab添加。")
        else:
            st.info(f"已添加 {len(stocks)} 只股票，点击「手动触发分析」生成首次分析结果。")
        return

    # 统计摘要
    action_counts = {}
    for r in records:
        ac = r.get('action_code', '?')
        action_counts[ac] = action_counts.get(ac, 0) + 1

    col1, col2, col3, col4 = st.columns(4)
    for col, key in zip([col1, col2, col3, col4], ['HOLD', 'TRIM', 'WATCH', 'EXIT']):
        with col:
            cnt = action_counts.get(key, 0)
            st.metric(f"{ACTION_ICONS.get(key, '')} {key}", cnt)

    st.markdown("---")

    # 详情卡片
    for r in records:
        code = r['code']
        stock = watch_stocks.get(code, {})
        action = r.get('action_code', '?')
        icon = ACTION_ICONS.get(action, '⚪')
        pnl = r.get('pnl_pct')
        pnl_str = f"{pnl:+.1f}%" if pnl is not None else "N/A"
        net_margin = r.get('net_margin')
        vs_ma250 = r.get('vs_ma250_pct')

        with st.expander(f"{icon} {r['name']}（{code}）— {action}  盈亏: {pnl_str}", expanded=(action in ('EXIT', 'WATCH'))):
            c1, c2, c3 = st.columns(3)
            with c1:
                st.metric("当前价", f"¥{r.get('close_price', 0):.2f}")
                st.metric("触发类型", r.get('trigger_type', '-'))
            with c2:
                st.metric("净利率", f"{net_margin:.1f}%" if net_margin else "N/A")
                st.metric("ROE趋势", r.get('roe_trend', 'N/A'))
            with c3:
                st.metric("vs MA250", f"{vs_ma250:+.1f}%" if vs_ma250 is not None else "N/A")
                st.metric("安全边际", r.get('margin_of_safety', 'N/A'))

            st.markdown(f"**判断（置信度 {r.get('confidence', 0):.1f}/10）：** {r.get('action', '')}")
            st.markdown(f"**理由：** {r.get('reasoning', '')}")

            if r.get('event_summary'):
                news_level = r.get('news_risk_level', '无')
                level_icon = {'警告': '🔴', '关注': '🟡', '利好': '🟢', '无': ''}.get(news_level, '')
                st.markdown(f"**事件：** {level_icon} {r['event_summary']}")

            st.caption(f"分析时间: {r.get('trade_date')}  买入逻辑: {stock.get('thesis', '未填写')[:60]}")

            # 单股快照导出按钮（导出后由 Claude 分析写回）
            if st.button(f"导出 {code} 快照", key=f"export_{code}"):
                from services.eod_analysis_service import export_snapshots
                with st.spinner(f"正在采集 {r['name']} 快照..."):
                    path = export_snapshots([code])
                st.success(f"快照已导出: {path}  请让 Claude 读取分析并写回决策。")


# ==================== Tab2: 股票管理 ====================

def _tab_stock_management():
    st.subheader("观察股票管理")

    # 添加新股票
    with st.expander("添加新股票", expanded=False):
        with st.form("add_stock_form"):
            col1, col2 = st.columns(2)
            with col1:
                new_code = st.text_input("股票代码 *", placeholder="如: 002594")
                new_name = st.text_input("股票名称 *", placeholder="如: 比亚迪")
                cost_price = st.number_input("持仓成本价（元）", min_value=0.0, value=0.0, step=0.01)
                quantity = st.number_input("持仓数量（股）", min_value=0, value=0, step=100)
            with col2:
                moat_type = st.selectbox("护城河类型", MOAT_TYPES)
                thesis = st.text_input("买入核心逻辑（30字内）", placeholder="如: 新能源汽车全球化布局受益")
                valuation_basis = st.text_input("估值基准", placeholder="如: 15xPE < 历史30分位")
                main_risk = st.text_input("主要风险（20字内）", placeholder="如: 价格战导致利润率下滑")

            invalidation = st.text_input("逻辑失效条件（触发即考虑EXIT）",
                                         placeholder="如: 净利率连续2季度低于2% 或 市占率下降>5%")

            submitted = st.form_submit_button("添加", type="primary")
            if submitted:
                if not new_code or not new_name:
                    st.error("股票代码和名称为必填项")
                else:
                    try:
                        eod_analysis_db.add_watch_stock(
                            code=new_code.strip(),
                            name=new_name.strip(),
                            cost_price=cost_price if cost_price > 0 else None,
                            quantity=int(quantity) if quantity > 0 else None,
                            moat_type=moat_type,
                            thesis=thesis,
                            valuation_basis=valuation_basis,
                            main_risk=main_risk,
                            invalidation_condition=invalidation,
                        )
                        st.success(f"已添加: {new_code} {new_name}")
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))

    st.markdown("---")

    # 已有股票列表
    stocks = eod_analysis_db.get_all_watch_stocks(enabled_only=False)
    if not stocks:
        st.info("暂无观察股票")
        return

    for stock in stocks:
        code = stock['code']
        enabled = bool(stock.get('enabled', 1))
        status_icon = "✅" if enabled else "⏸"

        with st.expander(f"{status_icon} {stock['name']}（{code}）  成本: ¥{stock.get('cost_price') or '-'}"):
            with st.form(f"edit_form_{code}"):
                c1, c2 = st.columns(2)
                with c1:
                    edit_cost = st.number_input("成本价", value=float(stock.get('cost_price') or 0), min_value=0.0, step=0.01)
                    edit_qty = st.number_input("数量", value=int(stock.get('quantity') or 0), min_value=0, step=100)
                    edit_moat = st.selectbox("护城河", MOAT_TYPES,
                                             index=MOAT_TYPES.index(stock.get('moat_type', '其他'))
                                             if stock.get('moat_type') in MOAT_TYPES else len(MOAT_TYPES)-1)
                with c2:
                    edit_thesis = st.text_input("买入逻辑", value=stock.get('thesis', ''))
                    edit_val = st.text_input("估值基准", value=stock.get('valuation_basis', ''))
                    edit_risk = st.text_input("主要风险", value=stock.get('main_risk', ''))

                edit_inv = st.text_input("失效条件", value=stock.get('invalidation_condition', ''))
                edit_enabled = st.checkbox("启用监控", value=enabled)

                col_save, col_del = st.columns([3, 1])
                with col_save:
                    if st.form_submit_button("保存", type="primary"):
                        eod_analysis_db.update_watch_stock(
                            code=code,
                            cost_price=edit_cost if edit_cost > 0 else None,
                            quantity=int(edit_qty) if edit_qty > 0 else None,
                            moat_type=edit_moat,
                            thesis=edit_thesis,
                            valuation_basis=edit_val,
                            main_risk=edit_risk,
                            invalidation_condition=edit_inv,
                            enabled=1 if edit_enabled else 0,
                        )
                        st.success("已保存")
                        st.rerun()
                with col_del:
                    if st.form_submit_button("删除", type="secondary"):
                        eod_analysis_db.delete_watch_stock(code)
                        st.success(f"已删除 {code}")
                        st.rerun()

            # K线缓存状态
            count = eod_analysis_db.get_kline_count(code)
            latest_date = eod_analysis_db.get_latest_kline_date(code)
            st.caption(f"K线缓存: {count} 条  最新: {latest_date or '未缓存'}")


# ==================== Tab3: 历史记录 ====================

def _tab_history():
    st.subheader("历史分析记录")

    stocks = eod_analysis_db.get_all_watch_stocks(enabled_only=False)
    if not stocks:
        st.info("暂无股票")
        return

    code_options = {f"{s['name']}（{s['code']}）": s['code'] for s in stocks}
    selected_label = st.selectbox("选择股票", list(code_options.keys()))
    selected_code = code_options[selected_label]

    records = eod_analysis_db.get_records_by_code(selected_code, limit=50)
    if not records:
        st.info("暂无历史分析记录，请先触发分析")
        return

    # 决策时间线图
    df = pd.DataFrame(records)
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    df_sorted = df.sort_values('trade_date')

    action_order = {'HOLD': 1, 'TRIM': 2, 'WATCH': 3, 'EXIT': 4}
    df_sorted['action_num'] = df_sorted['action_code'].map(action_order).fillna(0)

    try:
        import plotly.graph_objects as go
        fig = go.Figure()

        color_map = {'HOLD': '#22c55e', 'TRIM': '#3b82f6', 'WATCH': '#eab308', 'EXIT': '#ef4444'}
        for ac, color in color_map.items():
            mask = df_sorted['action_code'] == ac
            if mask.any():
                sub = df_sorted[mask]
                fig.add_trace(go.Scatter(
                    x=sub['trade_date'], y=sub['close_price'],
                    mode='markers',
                    name=ac,
                    marker=dict(color=color, size=10, symbol='circle'),
                    text=sub['reasoning'].str[:60],
                    hovertemplate='%{x}<br>¥%{y:.2f}<br>%{text}',
                ))

        # 成本线
        stock_info = eod_analysis_db.get_watch_stock(selected_code)
        cost = stock_info.get('cost_price') if stock_info else None
        if cost and cost > 0:
            fig.add_hline(y=cost, line_dash='dash', line_color='gray',
                          annotation_text=f"成本 ¥{cost:.2f}")

        fig.update_layout(
            title=f"{selected_label} 决策历史",
            xaxis_title="日期",
            yaxis_title="收盘价（元）",
            height=380,
            legend=dict(orientation='h'),
        )
        st.plotly_chart(fig, use_container_width=True)
    except ImportError:
        st.warning("安装 plotly 以显示图表: pip install plotly")

    # 详细表格
    display_cols = ['trade_date', 'trigger_type', 'action_code', 'close_price',
                    'pnl_pct', 'net_margin', 'roe_trend', 'confidence', 'reasoning']
    df_show = df_sorted[display_cols].copy()
    df_show.columns = ['日期', '触发', '决策', '价格', '盈亏%', '净利率%', 'ROE趋势', '置信度', '理由']
    df_show = df_show.sort_values('日期', ascending=False)
    st.dataframe(df_show, use_container_width=True, hide_index=True)


# ==================== Tab4: 调度器设置 ====================

def _tab_scheduler():
    st.subheader("调度器设置")

    status = eod_scheduler.get_status()

    col1, col2 = st.columns(2)
    with col1:
        st.metric("调度状态", "运行中" if status['is_running'] else "已停止")
        st.metric("上次运行", status['last_run_time'] or "从未")
    with col2:
        st.metric("调度时间", status['schedule_time'])
        st.metric("上次结果", status['last_run_result'] or "-")

    st.markdown("---")

    new_time = st.text_input("调度时间（HH:MM）", value=status['schedule_time'],
                              help="每个交易日收盘后自动刷新数据缓存（不自动决策），推荐 15:30")
    col_start, col_stop, col_set = st.columns(3)
    with col_start:
        if st.button("启动调度器", type="primary", use_container_width=True,
                     disabled=status['is_running']):
            eod_scheduler.set_schedule_time(new_time)
            eod_scheduler.start()
            st.success(f"调度器已启动，每日 {new_time} 执行")
            st.rerun()
    with col_stop:
        if st.button("停止调度器", use_container_width=True,
                     disabled=not status['is_running']):
            eod_scheduler.stop()
            st.warning("调度器已停止")
            st.rerun()
    with col_set:
        if st.button("更新时间", use_container_width=True):
            eod_scheduler.set_schedule_time(new_time)
            st.success(f"已更新为 {new_time}")
            st.rerun()

    st.markdown("---")
    st.markdown("**触发类型说明**（快照中的 trigger_type 标签，供 Claude 判断侧重）")
    st.markdown("""
| 触发类型 | 条件 | 分析重点 |
|---|---|---|
| `price_surge` | 单日涨幅 ≥ 8% | 估值是否已透支，是否减仓 |
| `price_drop` | 单日跌幅 ≥ 8% | 买入逻辑是否依然成立，是否加仓 |
| `event` | 命中重大新闻关键词 | 事件对护城河/逻辑的影响 |
| `weekly` | 周末 | 综合评估本周基本面变化 |
| `manual` | 手动导出 | 全维度评估 |
""")


# ==================== 主入口 ====================

def display_eod_analysis():
    st.title("持仓逻辑评估")
    st.caption("数据由系统采集，四档决策由 Claude 结合 v5 框架分析写回 · HOLD / TRIM / WATCH / EXIT")

    tab1, tab2, tab3, tab4 = st.tabs(["最新分析", "股票管理", "历史记录", "调度设置"])

    with tab1:
        _tab_dashboard()
    with tab2:
        _tab_stock_management()
    with tab3:
        _tab_history()
    with tab4:
        _tab_scheduler()
