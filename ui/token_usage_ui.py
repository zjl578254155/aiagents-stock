"""
Token用量分析仪表盘
展示AI API调用的Token消耗、费用统计和趋势分析
"""

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from services.token_usage_service import token_usage_service


def display_token_usage():
    """Token用量分析主页面"""
    st.title("📊 Token 用量分析")
    st.caption("追踪AI API调用的Token消耗和预估费用")

    # 顶部汇总指标
    _display_summary_metrics()

    # 4个Tab页
    tab1, tab2, tab3, tab4 = st.tabs(["📈 趋势分析", "🤖 模型分布", "🧩 功能分布", "📋 明细记录"])

    with tab1:
        _display_trend_analysis()
    with tab2:
        _display_model_breakdown()
    with tab3:
        _display_feature_breakdown()
    with tab4:
        _display_recent_records()


def _display_summary_metrics():
    """顶部汇总指标卡片"""
    stats = token_usage_service.get_total_stats()

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("总调用次数", f"{stats.get('total_calls', 0):,}")
    with col2:
        total_tokens = stats.get('total_tokens', 0)
        if total_tokens >= 1_000_000:
            st.metric("总Token数", f"{total_tokens / 1_000_000:.2f}M")
        elif total_tokens >= 1_000:
            st.metric("总Token数", f"{total_tokens / 1_000:.1f}K")
        else:
            st.metric("总Token数", f"{total_tokens:,}")
    with col3:
        cost = stats.get('total_cost', 0)
        st.metric("预估总费用", f"¥{cost:.4f}")
    with col4:
        st.metric("统计天数", f"{stats.get('days_tracked', 0)} 天")


def _display_trend_analysis():
    """趋势分析Tab"""
    days = st.selectbox("时间范围", [7, 30, 90], index=1, format_func=lambda x: f"最近 {x} 天", key="trend_days")
    daily = token_usage_service.get_daily_summary(days)

    if not daily:
        st.info("暂无数据，使用AI功能后将自动记录Token用量。")
        return

    df = pd.DataFrame(daily)
    df['date'] = pd.to_datetime(df['date'])

    # 双轴图：Token + 费用
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=df['date'], y=df['total_tokens'], name='Token数',
        marker_color='#636EFA', opacity=0.7, yaxis='y'
    ))
    fig.add_trace(go.Scatter(
        x=df['date'], y=df['total_cost'], name='费用(CNY)',
        line=dict(color='#EF553B', width=2), mode='lines+markers', yaxis='y2'
    ))
    fig.update_layout(
        title='每日Token消耗与费用趋势',
        xaxis_title='日期',
        yaxis=dict(title='Token数', side='left'),
        yaxis2=dict(title='费用(CNY)', side='right', overlaying='y'),
        legend=dict(orientation='h', y=1.12),
        height=400, margin=dict(t=60, b=40)
    )
    st.plotly_chart(fig, use_container_width=True)

    # 调用次数趋势
    fig2 = px.bar(df, x='date', y='call_count', title='每日调用次数',
                  labels={'date': '日期', 'call_count': '调用次数'},
                  color_discrete_sequence=['#00CC96'])
    fig2.update_layout(height=300, margin=dict(t=40, b=40))
    st.plotly_chart(fig2, use_container_width=True)

    # 小时分布
    hourly = token_usage_service.get_hourly_distribution(days)
    if hourly:
        df_h = pd.DataFrame(hourly)
        fig3 = px.bar(df_h, x='hour', y='call_count', title='调用时段分布',
                      labels={'hour': '小时', 'call_count': '调用次数'},
                      color_discrete_sequence=['#AB63FA'])
        fig3.update_layout(height=280, margin=dict(t=40, b=40),
                           xaxis=dict(dtick=1))
        st.plotly_chart(fig3, use_container_width=True)


def _display_model_breakdown():
    """模型分布Tab"""
    days = st.selectbox("时间范围", [7, 30, 90], index=1, format_func=lambda x: f"最近 {x} 天", key="model_days")
    data = token_usage_service.get_model_breakdown(days)

    if not data:
        st.info("暂无数据。")
        return

    df = pd.DataFrame(data)

    col1, col2 = st.columns(2)
    with col1:
        fig = px.pie(df, values='total_tokens', names='model', title='Token用量占比（按模型）',
                     hole=0.4)
        fig.update_layout(height=380, margin=dict(t=40, b=20))
        st.plotly_chart(fig, use_container_width=True)
    with col2:
        fig2 = px.bar(df, x='model', y='total_cost', title='费用分布（按模型）',
                      labels={'model': '模型', 'total_cost': '费用(CNY)'},
                      color='model')
        fig2.update_layout(height=380, margin=dict(t=40, b=20), showlegend=False)
        st.plotly_chart(fig2, use_container_width=True)

    # 明细表格
    display_df = df[['model', 'total_tokens', 'total_cost', 'call_count']].copy()
    display_df.columns = ['模型', 'Token总量', '费用(CNY)', '调用次数']
    display_df['费用(CNY)'] = display_df['费用(CNY)'].apply(lambda x: f"¥{x:.4f}")
    display_df['Token总量'] = display_df['Token总量'].apply(lambda x: f"{x:,}")
    st.dataframe(display_df, use_container_width=True, hide_index=True)


def _display_feature_breakdown():
    """功能分布Tab"""
    days = st.selectbox("时间范围", [7, 30, 90], index=1, format_func=lambda x: f"最近 {x} 天", key="feature_days")
    data = token_usage_service.get_feature_breakdown(days)

    if not data:
        st.info("暂无数据。")
        return

    df = pd.DataFrame(data)
    # 中文标签
    df['feature_label'] = df['feature'].apply(token_usage_service.get_feature_label)

    col1, col2 = st.columns(2)
    with col1:
        fig = px.pie(df, values='total_tokens', names='feature_label', title='Token用量占比（按功能）',
                     hole=0.4)
        fig.update_layout(height=380, margin=dict(t=40, b=20))
        st.plotly_chart(fig, use_container_width=True)
    with col2:
        fig2 = px.bar(df, x='feature_label', y='call_count', title='调用次数（按功能）',
                      labels={'feature_label': '功能', 'call_count': '调用次数'},
                      color='feature_label')
        fig2.update_layout(height=380, margin=dict(t=40, b=20), showlegend=False)
        st.plotly_chart(fig2, use_container_width=True)

    # 明细表格
    display_df = df[['feature_label', 'total_tokens', 'total_cost', 'call_count']].copy()
    display_df.columns = ['功能', 'Token总量', '费用(CNY)', '调用次数']
    display_df['费用(CNY)'] = display_df['费用(CNY)'].apply(lambda x: f"¥{x:.4f}")
    display_df['Token总量'] = display_df['Token总量'].apply(lambda x: f"{x:,}")
    st.dataframe(display_df, use_container_width=True, hide_index=True)


def _display_recent_records():
    """明细记录Tab"""
    limit = st.selectbox("显示条数", [50, 100, 200, 500], index=1, key="records_limit")
    records = token_usage_service.get_recent_records(limit)

    if not records:
        st.info("暂无调用记录。")
        return

    df = pd.DataFrame(records)
    # 格式化
    df['feature_label'] = df['feature'].apply(token_usage_service.get_feature_label)
    df['estimated_cost'] = df['estimated_cost'].apply(lambda x: f"¥{x:.6f}")
    df['success'] = df['success'].apply(lambda x: '成功' if x else '失败')

    display_df = df[['timestamp', 'model', 'feature_label', 'caller',
                     'prompt_tokens', 'completion_tokens', 'total_tokens',
                     'estimated_cost', 'success']].copy()
    display_df.columns = ['时间', '模型', '功能', '调用方法',
                          '输入Token', '输出Token', '总Token',
                          '预估费用', '状态']
    st.dataframe(display_df, use_container_width=True, hide_index=True, height=500)

    # 数据管理
    with st.expander("数据管理"):
        col1, col2 = st.columns(2)
        with col1:
            keep_days = st.number_input("保留天数", min_value=7, max_value=365, value=90)
        with col2:
            if st.button("清理旧数据"):
                deleted = token_usage_service.cleanup(keep_days)
                st.success(f"已清理 {deleted} 条记录")
                st.rerun()
