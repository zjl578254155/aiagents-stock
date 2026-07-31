"""
EOD持仓逻辑评估服务（数据采集层）

本模块只负责采集数据：刷新K线/基本面缓存、扫描新闻、判断触发条件，
输出自包含的「持仓快照」(build_snapshot/export_snapshots)。
四档决策(HOLD/TRIM/WATCH/EXIT)由 Claude Code 会话结合快照 + v5 框架做出，
再通过 save_decision 写回 eod_analysis_records 表供 UI 展示。
"""

import json
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import requests

from db.eod_analysis_db import eod_analysis_db
from core.config import TDX_CONFIG

logger = logging.getLogger(__name__)

# 快照导出目录（复用 data/ 目录，不新增表）
SNAPSHOT_DIR = "data/eod_snapshots"

# ==================== 触发阈值 ====================
SURGE_THRESHOLD = 8.0    # 单日涨幅 ≥ 8% → 估值透支检查
DROP_THRESHOLD = -8.0    # 单日跌幅 ≤ -8% → 加仓安全边际重估
TRIM_PE_RATIO = 1.3      # 估值偏高分界线（相对历史均值倍数，近似用MA60偏离）
EXIT_PE_RATIO = 1.9      # 估值严重透支分界线

# MA60距离作为估值偏离代理指标（缺PE时用）
TRIM_MA60_PCT = 30.0     # 价格高于MA60 30% → 偏高
EXIT_MA60_PCT = 60.0     # 价格高于MA60 60% → 透支

# 新闻关键词
# 命中任一即定级 '警告'。覆盖三类红线：①监管/违法 ②高管异动/落马 ③财报诚信存疑。
# 教训(2026-06-08 五粮液)：'免去董事长'+'303亿差错更正' 这类治理/诚信事件曾因词表缺失
# 只被定级到 '关注'，必须把高管落马、财报追溯调减、审计非标等显性红线词纳入。
HIGH_RISK_KEYWORDS = [
    # ① 监管/违法
    '监管', '处罚', '立案', '立案调查', '被立案', '违规', '预亏', '业绩大幅下滑',
    '大股东减持', '质押爆仓', '退市风险', '证监会', '交易所问询', '问询函', '关注函',
    # ② 高管异动/落马（'免去/解任/辞职'多为高管职务变动，宁可多报）
    '主要负责人变动', '免去', '解任', '辞职', '被查', '留置', '双开', '协助调查',
    # ③ 财报诚信存疑
    '差错更正', '追溯调整', '追溯调减', '财报延期', '延期披露', '业绩变脸',
    '非标', '保留意见', '无法表示意见',
]
POSITIVE_KEYWORDS = [
    '大股东增持', '回购股份', '股权激励', '业绩超预期',
    '分红提升', '管理层增持',
]

# 复用全项目统一的 TDX 配置（core.config.TDX_CONFIG，默认 http://192.168.1.222:8181），
# 避免本模块私有默认值与其他服务连到不同端口
TDX_BASE_URL = TDX_CONFIG['base_url']


# ==================== K线 & 缓存 ====================

def _fetch_full_kline_from_tdx(code: str) -> List[Dict]:
    """从TDX拉取全量日K线，返回标准化列表（价格已转元）"""
    try:
        resp = requests.get(
            f"{TDX_BASE_URL}/api/kline",
            params={'code': code, 'type': 'day'},
            timeout=15,
        )
        result = resp.json()
        if result.get('code') != 0:
            logger.warning(f"TDX kline error for {code}: {result.get('message')}")
            return []
        data = result.get('data', {})
        raw_list = data.get('List', [])
        records = []
        for item in raw_list:
            trade_date = item.get('Time', '')[:10]  # "2024-01-02T..."
            if not trade_date:
                continue
            records.append({
                'trade_date': trade_date,
                'open': round(item.get('Open', 0) / 1000, 3),
                'high': round(item.get('High', 0) / 1000, 3),
                'low': round(item.get('Low', 0) / 1000, 3),
                'close': round(item.get('Close', 0) / 1000, 3),
                'volume': item.get('Volume', 0),
                'data_source': 'tdx',
            })
        return records
    except Exception as e:
        logger.error(f"TDX fetch failed for {code}: {e}")
        return []


def refresh_kline_cache(code: str) -> int:
    """
    增量刷新K线缓存。
    - 全量拉取TDX数据（约3600条）
    - 只写入比缓存中最新日期更新的记录
    返回新增条数。
    """
    all_records = _fetch_full_kline_from_tdx(code)
    if not all_records:
        return 0

    latest_cached = eod_analysis_db.get_latest_kline_date(code)
    if latest_cached:
        new_records = [r for r in all_records if r['trade_date'] > latest_cached]
    else:
        new_records = all_records

    if new_records:
        eod_analysis_db.upsert_kline_records(code, new_records)
        logger.info(f"[{code}] K线缓存更新 +{len(new_records)} 条，共 {eod_analysis_db.get_kline_count(code)} 条")
    return len(new_records)


def _compute_ma(closes: List[float], period: int) -> Optional[float]:
    if len(closes) < period:
        return None
    return round(sum(closes[-period:]) / period, 3)


def get_technical_snapshot(code: str) -> Dict:
    """从缓存读取最近300条K线，计算技术指标快照"""
    rows = eod_analysis_db.get_kline_records(code, limit=300)
    if not rows:
        return {}
    closes = [r['close'] for r in rows]
    latest = rows[-1]
    prev = rows[-2] if len(rows) >= 2 else latest
    day_chg_pct = round((latest['close'] - prev['close']) / prev['close'] * 100, 2) if prev['close'] else 0

    ma60 = _compute_ma(closes, 60)
    ma250 = _compute_ma(closes, 250)
    vs_ma250_pct = round((latest['close'] - ma250) / ma250 * 100, 2) if ma250 else None

    return {
        'trade_date': latest['trade_date'],
        'close': latest['close'],
        'day_chg_pct': day_chg_pct,
        'ma60': ma60,
        'ma250': ma250,
        'vs_ma250_pct': vs_ma250_pct,
    }


# ==================== 基本面 ====================

def refresh_fundamental_cache(code: str) -> bool:
    """通过akshare更新基本面缓存"""
    try:
        import akshare as ak
        df = ak.stock_financial_analysis_indicator(symbol=code, start_year="2020")
        if df is None or df.empty:
            return False

        # akshare stock_financial_analysis_indicator 实际列名为「日期」「摊薄每股收益(元)」
        # 「每股净资产_调整后(元)」「净资产收益率(%)」——原代码假设的「报告期/基本每股收益/每股净资产」并不存在
        date_col = '报告期' if '报告期' in df.columns else '日期'
        df = df.sort_values(date_col, ascending=True)
        for _, row in df.iterrows():
            report_date = str(row.get(date_col, ''))
            if not report_date or report_date == 'nan':
                continue

            def _safe(col):
                try:
                    v = row.get(col)
                    return float(v) if v is not None and str(v) not in ('', 'nan', '--') else None
                except Exception:
                    return None

            net_margin_raw = _safe('销售净利率(%)')
            debt_ratio_raw = _safe('资产负债率(%)')
            gross_margin_raw = _safe('销售毛利率(%)')
            eps_raw = _safe('摊薄每股收益(元)')
            bvps_raw = _safe('每股净资产_调整后(元)')

            # ROE 优先用 akshare 原生「净资产收益率(%)」（更准确，且避免送转致 BVPS 骤降使近似失真）；
            # 缺失时回退 EPS/BVPS 近似
            roe_native = _safe('净资产收益率(%)')
            if roe_native is not None:
                roe_approx = roe_native
            elif eps_raw is not None and bvps_raw and bvps_raw > 0:
                roe_approx = round(eps_raw / bvps_raw * 100, 2)
            else:
                roe_approx = None

            eod_analysis_db.upsert_fundamental(
                code=code,
                report_date=report_date,
                eps=eps_raw,
                bvps=bvps_raw,
                net_margin=net_margin_raw,
                debt_ratio=debt_ratio_raw,
                gross_margin=gross_margin_raw,
                roe_approx=roe_approx,
            )
        return True
    except Exception as e:
        logger.error(f"Fundamental refresh failed for {code}: {e}")
        return False


def get_fundamental_snapshot(code: str) -> Dict:
    """
    返回最新财报期数据 + 口径统一后的趋势判断。
    口径原则（贴合 v5）：长期水平/趋势看年报序列(12-31)，最新动能看季度同比。
    akshare 季度值为 YTD 累计口径，不可直接与年度门槛比较，故年化不取、同比直接比。
    """
    records = eod_analysis_db.get_fundamentals(code, limit=24)
    if not records:
        return {}

    latest = records[-1]
    latest_date = latest['report_date']
    latest_period_type = '年报' if latest_date.endswith('-12-31') else '季报'

    # 年报序列（仅取 12-31），用于趋势/门槛判定
    annual = [r for r in records if r['report_date'].endswith('-12-31')]
    annual_roe = [(r['report_date'][:4], r['roe_approx']) for r in annual if r['roe_approx'] is not None]
    annual_nm = [(r['report_date'][:4], r['net_margin']) for r in annual if r['net_margin'] is not None]

    # ROE 趋势：基于年报 ROE 序列（避免单季 YTD 误判）
    roe_trend = '数据不足'
    roe_vals = [v for _, v in annual_roe]
    if len(roe_vals) >= 2:
        last_roe = roe_vals[-1]
        debt_ratio = latest.get('debt_ratio') or 0
        if debt_ratio > 60 and last_roe > 15:
            roe_trend = '杠杆驱动(高负债)'
        elif last_roe >= 15 and roe_vals[-1] >= roe_vals[-2]:
            roe_trend = '稳定(≥15%)'
        elif last_roe < 12:
            roe_trend = '低位(<12%)'
        else:
            roe_trend = '下滑中'
    elif len(roe_vals) == 1:
        roe_trend = '年报数据不足(单年)'

    # 同比：找与最新期同月日、年份-1 的记录
    yoy_net_margin = None
    yoy_roe = None
    prev_year_period = None
    if len(latest_date) == 10:
        prev_year_date = f"{int(latest_date[:4]) - 1}{latest_date[4:]}"
        prev = next((r for r in records if r['report_date'] == prev_year_date), None)
        if prev:
            prev_year_period = prev_year_date
            yoy_net_margin = prev.get('net_margin')
            yoy_roe = prev.get('roe_approx')

    return {
        'report_date': latest_date,
        'latest_period_type': latest_period_type,
        'net_margin': latest.get('net_margin'),
        'debt_ratio': latest.get('debt_ratio'),
        'roe_approx': latest.get('roe_approx'),
        'roe_trend': roe_trend,
        'gross_margin': latest.get('gross_margin'),
        'eps': latest.get('eps'),
        # 口径统一新增字段（供 prompt 使用，不入库）
        'annual_roe_series': annual_roe[-4:],          # [(年,ROE)] 近4个年报
        'annual_net_margin_series': annual_nm[-4:],
        'yoy_net_margin': yoy_net_margin,
        'yoy_roe': yoy_roe,
        'prev_year_period': prev_year_period,
    }


# ==================== 新闻 ====================

def _flatten_news_items(payload: Optional[Dict], limit: int) -> List[str]:
    """从 {news_data/announcement_data: {items:[...]}} 结构提取文本。
    item 字段是中文列名(标题/内容/...)，拼接所有非空字符串值，兼容不同数据源列名。"""
    out: List[str] = []
    for block_key in ('news_data', 'announcement_data'):
        block = (payload or {}).get(block_key)
        if not block:
            continue
        for item in (block.get('items') or [])[:limit]:
            parts = [str(v) for v in item.values() if v and str(v) not in ('无法解析', 'None')]
            if parts:
                out.append(' '.join(parts))
    return out


def _fetch_news_text(code: str) -> str:
    """聚合新闻+公告文本，供关键词扫描和分析输入。

    注意：两个数据源的获取函数是【类实例方法】，必须先实例化再调用；
    任一源失败记 warning（不再 except: pass 静默吞掉，否则新闻层瞎了也无感知）。"""
    texts: List[str] = []
    try:
        from data_sources.qstock_news_data import QStockNewsDataFetcher
        payload = QStockNewsDataFetcher().get_stock_news(code)
        texts.extend(_flatten_news_items(payload, 10))
    except Exception as e:
        logger.warning("qstock 新闻抓取失败 code=%s: %r", code, e)
    try:
        from data_sources.news_announcement_data import NewsAnnouncementDataFetcher
        payload = NewsAnnouncementDataFetcher().get_news_and_announcements(code)
        texts.extend(_flatten_news_items(payload, 5))
    except Exception as e:
        logger.warning("公告抓取失败 code=%s: %r", code, e)
    return '\n'.join(texts)


def scan_news_risk(code: str, _text: Optional[str] = None) -> Tuple[str, str, bool]:
    """
    返回 (news_risk_level, event_summary, has_major_event)
    news_risk_level: 利好 | 无 | 关注 | 警告 | 未抓到

    '未抓到' 与 '无' 必须区分：
      - '无'   = 确实抓到了新闻，但未命中任何风险/利好/公告关键词；
      - '未抓到' = 新闻源没返回任何文本(可能是源故障/接口失效)，新闻信号不可信。
    区分二者，分析侧才能识别"这只票需人工联网兜底"，避免把"源坏了"误读成"没坏消息"。

    '警告' 要求命中行内含本股简称：名称近似的其他主体新闻（如科博达×ST柯利达、
    恒顺醋业×恒顺众昇两例张冠李戴）会命中关键词但不含本股名，降级为'关注'待人工核实，
    避免误报把'警告'级别淹没（6-7月实测 45% 记录被打警告，信号已失效）。

    _text: 预先拉取的新闻文本，传入后跳过内部 _fetch_news_text，避免 build_snapshot 二次拉取。
    """
    text = _text if _text is not None else _fetch_news_text(code)
    if not text.strip():
        logger.warning("scan_news_risk: code=%s 未抓到任何新闻/公告，新闻信号置为'未抓到'", code)
        return '未抓到', '新闻源未返回数据，需人工联网核实', False

    stock_name = (eod_analysis_db.get_watch_stock(code) or {}).get('name', '')
    lines = [ln for ln in text.split('\n') if ln.strip()]
    risk_hits = []          # 关键词命中且行内含本股简称
    unattributed_hits = []  # 关键词命中但行内无本股简称（主体串扰高发区）
    for kw in HIGH_RISK_KEYWORDS:
        matched = [ln for ln in lines if kw in ln]
        if not matched:
            continue
        if not stock_name or any(stock_name in ln for ln in matched):
            risk_hits.append(kw)
        else:
            unattributed_hits.append(kw)
    pos_hits = [kw for kw in POSITIVE_KEYWORDS if kw in text]

    if risk_hits:
        level = '警告'
        summary = '风险信号: ' + ', '.join(risk_hits[:3])
        return level, summary, True
    if unattributed_hits:
        return '关注', '疑似风险(主体未核实): ' + ', '.join(unattributed_hits[:3]), True
    if pos_hits:
        level = '利好'
        summary = '正面信号: ' + ', '.join(pos_hits[:3])
        return level, summary, True
    # 判断是否有重大公告（文本中出现公告关键词但不含风险词）
    # 注意：'公告'/'重大' 过于宽泛（几乎每条财经新闻都含），会导致所有股票被误标'关注'
    announcement_kw = ['重组', '并购', '分红方案', '配股', '定向增发', '股权转让', '重大事项']
    ann_hits = [kw for kw in announcement_kw if kw in text]
    if ann_hits:
        return '关注', '有公告: ' + ', '.join(ann_hits[:3]), True
    return '无', '', False


# ==================== 估值三字段（docs/快照估值字段补全方案.md T2） ====================

def _tencent_code(code: str) -> Optional[str]:
    if code.startswith('6'):
        return f'sh{code}'
    if code.startswith(('0', '3')):
        return f'sz{code}'
    return None


def fetch_valuation_batch(codes: List[str]) -> Dict[str, Dict]:
    """腾讯行情批量取 pe_ttm / pb / market_cap_yi（一次请求全部代码，按 code 分发）。
    字段位 f[39]=PE(TTM，亏损为负) f[45]=总市值(亿) f[46]=PB，仅经 2026-06-11/12 实测，
    附合理性断言（PB∈(0,100)、市值>0）兜底；接口失败三字段置 None，不阻塞快照导出。"""
    result: Dict[str, Dict] = {c: {'pe_ttm': None, 'pb': None, 'market_cap_yi': None} for c in codes}
    prefixed = [p for p in (_tencent_code(c) for c in codes) if p]
    if not prefixed:
        return result
    try:
        # 国内站必须直连：本机全局代理对 qt.gtimg.cn 会 SSL 失败，显式绕过代理
        resp = requests.get(f"https://qt.gtimg.cn/q={','.join(prefixed)}", timeout=10,
                            proxies={'http': None, 'https': None})
        resp.encoding = 'gbk'
        for line in resp.text.strip().split(';'):
            line = line.strip()
            if '=' not in line:
                continue
            head, body = line.split('=', 1)
            f = body.strip('"').split('~')
            if len(f) < 47:
                continue
            code = head.split('_')[-1][2:]

            def _num(s):
                try:
                    return float(s)
                except (TypeError, ValueError):
                    return None

            pe, cap, pb = _num(f[39]), _num(f[45]), _num(f[46])
            if pb is not None and not (0 < pb < 100):
                logger.warning(f"[{code}] PB={pb} 未过合理性断言，置None（疑字段位漂移）")
                pb = None
            if cap is not None and cap <= 0:
                cap = None
            if code in result:
                result[code] = {'pe_ttm': pe, 'pb': pb, 'market_cap_yi': cap}
    except Exception as e:
        logger.warning(f"估值三字段批量获取失败（置None不阻塞）: {e}")
    return result


# ==================== 触发判断 ====================

def check_trigger(code: str, tech: Dict, is_weekend: bool = False,
                  has_major_event: bool = False) -> Optional[str]:
    """
    判断是否触发分析，返回 trigger_type 或 None。
    优先级: price_surge > price_drop > weekly > event > fundamental_weekly
    """
    chg = tech.get('day_chg_pct', 0)
    if chg >= SURGE_THRESHOLD:
        return 'price_surge'
    if chg <= DROP_THRESHOLD:
        return 'price_drop'
    if has_major_event:
        return 'event'
    if is_weekend:
        return 'weekly'
    return None


# ==================== 通知 ====================

def _send_webhook(content: str) -> bool:
    from dotenv import load_dotenv
    load_dotenv()
    webhook_url = os.getenv('WEBHOOK_URL', '')
    webhook_type = os.getenv('WEBHOOK_TYPE', 'feishu').lower()
    if not os.getenv('WEBHOOK_ENABLED', 'false').lower() == 'true' or not webhook_url:
        return False
    try:
        if webhook_type == 'feishu':
            payload = {'msg_type': 'text', 'content': {'text': content}}
        else:
            payload = {'msgtype': 'text', 'text': {'content': content}}
        resp = requests.post(webhook_url, json=payload, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"Webhook send failed: {e}")
        return False


ACTION_ICONS = {'HOLD': '🟢', 'TRIM': '🔵', 'WATCH': '🟡', 'EXIT': '🔴'}


def _format_notification(stock: Dict, record: Dict) -> str:
    icon = ACTION_ICONS.get(record.get('action_code', ''), '⚪')
    pnl = record.get('pnl_pct')
    pnl_str = f"{pnl:+.1f}%" if pnl is not None else "N/A"
    lines = [
        f"{icon} 【持仓逻辑评估】{record['name']}({record['code']})",
        f"决策: {record.get('action_code')} — {record.get('action')}",
        f"触发: {record.get('trigger_type')}  交易日: {record.get('trade_date')}",
        f"当前价: ¥{(record.get('close_price') or 0):.2f}  持仓盈亏: {pnl_str}",
        f"净利率: {str(round(record['net_margin'], 1)) + '%' if record.get('net_margin') else 'N/A'}  ROE趋势: {record.get('roe_trend', 'N/A')}",
        f"安全边际: {record.get('margin_of_safety', 'N/A')}  置信度: {(record.get('confidence') or 0):.1f}/10",
        f"理由: {record.get('reasoning', '')}",
    ]
    if record.get('event_summary'):
        lines.append(f"事件: {record['event_summary']}")
    return '\n'.join(lines)


# ==================== 数据采集：持仓快照 ====================

def build_snapshot(code: str, trigger_type: Optional[str] = None,
                   force: bool = True) -> Optional[Dict]:
    """
    采集单只股票的「持仓快照」，供 Claude 会话结合 v5 框架做四档判断。
    流程：刷新K线→tech、刷新基本面→fundamental、扫新闻、判断触发、算盈亏。
    不做任何决策、不落库、不推送。
    返回自包含 dict；未在观察列表/取不到技术数据时返回 None。
    force=True（默认）即使未命中触发条件也返回快照（手动分析场景）。
    """
    stock = eod_analysis_db.get_watch_stock(code)
    if not stock or not stock.get('enabled'):
        logger.info(f"[{code}] 未在观察列表或已禁用，跳过")
        return None

    # 1. 刷新K线 → 技术面
    refresh_kline_cache(code)
    tech = get_technical_snapshot(code)
    if not tech:
        logger.warning(f"[{code}] 无法获取技术数据，跳过")
        return None

    # 2. 刷新基本面（含年报序列/同比/趋势口径）
    refresh_fundamental_cache(code)
    fundamental = get_fundamental_snapshot(code)

    # 3. 新闻扫描（只拉取一次，传入 scan_news_risk 避免重复 HTTP 请求）
    raw_news_text = _fetch_news_text(code)
    news_risk_level, event_summary, has_major_event = scan_news_risk(code, _text=raw_news_text)
    news_text = raw_news_text if has_major_event else ''

    # 4. 触发判断（仅作为标签提示，不影响是否返回）
    is_weekend = datetime.now().weekday() >= 5
    if trigger_type is None:
        trigger_type = check_trigger(code, tech, is_weekend, has_major_event)
    if trigger_type is None:
        trigger_type = 'manual' if force else None

    # 5. 盈亏
    cost = stock.get('cost_price', 0) or 0
    close = tech.get('close', 0) or 0
    pnl_pct = round((close - cost) / cost * 100, 2) if cost > 0 else None

    return {
        'code': code,
        'name': stock['name'],
        'trade_date': tech.get('trade_date', datetime.now().strftime('%Y-%m-%d')),
        'trigger_type': trigger_type,
        # 买入逻辑（评估锚点）
        'moat_type': stock.get('moat_type'),
        'thesis': stock.get('thesis'),
        'valuation_basis': stock.get('valuation_basis'),
        'invalidation_condition': stock.get('invalidation_condition'),
        'main_risk': stock.get('main_risk'),
        'cost_price': cost,
        # 技术面
        'close_price': close,
        'pnl_pct': pnl_pct,
        'day_chg_pct': tech.get('day_chg_pct'),
        'ma60': tech.get('ma60'),
        'ma250': tech.get('ma250'),
        'vs_ma250_pct': tech.get('vs_ma250_pct'),
        # 基本面（三口径，口径纪律已固化在这些字段里）
        'latest_report_date': fundamental.get('report_date'),
        'latest_period_type': fundamental.get('latest_period_type'),
        'net_margin': fundamental.get('net_margin'),
        'debt_ratio': fundamental.get('debt_ratio'),
        'roe_approx': fundamental.get('roe_approx'),
        'roe_trend': fundamental.get('roe_trend'),
        'gross_margin': fundamental.get('gross_margin'),
        'annual_roe_series': fundamental.get('annual_roe_series'),
        'annual_net_margin_series': fundamental.get('annual_net_margin_series'),
        'yoy_net_margin': fundamental.get('yoy_net_margin'),
        'yoy_roe': fundamental.get('yoy_roe'),
        'prev_year_period': fundamental.get('prev_year_period'),
        # 新闻/事件
        'has_major_event': has_major_event,
        'news_risk_level': news_risk_level,
        'event_summary': event_summary,
        'news_text': news_text,
        # 数据新鲜度：trade_date 为 K 线缓存最新交易日；工作日快照滞后于今日即标 stale，
        # 分析侧见 True 必须用行情接口取实时/收盘价校正，禁止直接采信 close_price
        'snapshot_generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'data_stale': (datetime.now().weekday() < 5
                       and tech.get('trade_date', '') < datetime.now().strftime('%Y-%m-%d')),
        # 口径提醒（供阅读者遵循）
        'caliber_note': '季报为YTD累计值，不可与年度门槛(如ROE≥15%)直接比较；长期水平看年报序列，最新动能看同比。',
    }


def export_snapshots(codes: Optional[List[str]] = None, force: bool = True) -> str:
    """
    批量采集启用观察股票（或指定 codes）的快照，写 JSON 文件并返回路径。
    这是 Claude 会话读取分析的输入文件。
    """
    if codes:
        targets = [c for c in codes]
    else:
        targets = [s['code'] for s in eod_analysis_db.get_all_watch_stocks(enabled_only=True)]

    snapshots = []
    for code in targets:
        try:
            snap = build_snapshot(code, force=force)
            if snap:
                snapshots.append(snap)
        except Exception as e:
            logger.error(f"[{code}] 快照采集异常: {e}")

    # 估值三字段批量合并（一次请求，按 code 分发）
    valuations = fetch_valuation_batch([s['code'] for s in snapshots])
    for snap in snapshots:
        snap.update(valuations.get(snap['code'], {'pe_ttm': None, 'pb': None, 'market_cap_yi': None}))

    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    path = os.path.join(SNAPSHOT_DIR, f"snapshot_{datetime.now().strftime('%Y%m%d_%H%M')}.json")
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(snapshots, f, ensure_ascii=False, indent=2)
    logger.info(f"快照已导出 {len(snapshots)} 只 → {path}")
    return path


# ==================== 决策写回 ====================

def save_decision(code: str, snapshot: Dict, decision: Dict, push: bool = False) -> int:
    """
    把 Claude 给出的四档决策与快照数据合并写回 eod_analysis_records，供 UI 展示。
    decision 字段：action_code/action/confidence/margin_of_safety/thesis_still_valid/reasoning。
    push=True 且非 HOLD 时，复用飞书 webhook 推送（默认不推）。
    返回写入记录 id。
    """
    record = {
        'code': code,
        'name': snapshot.get('name', code),
        'trade_date': snapshot.get('trade_date', datetime.now().strftime('%Y-%m-%d')),
        'trigger_type': snapshot.get('trigger_type') or 'manual',
        'close_price': snapshot.get('close_price'),
        'cost_price': snapshot.get('cost_price'),
        'pnl_pct': snapshot.get('pnl_pct'),
        'ma60': snapshot.get('ma60'),
        'ma250': snapshot.get('ma250'),
        'vs_ma250_pct': snapshot.get('vs_ma250_pct'),
        'latest_report_date': snapshot.get('latest_report_date'),
        'net_margin': snapshot.get('net_margin'),
        'debt_ratio': snapshot.get('debt_ratio'),
        'roe_approx': snapshot.get('roe_approx'),
        'roe_trend': snapshot.get('roe_trend'),
        'has_major_event': 1 if snapshot.get('has_major_event') else 0,
        'event_summary': snapshot.get('event_summary'),
        'news_risk_level': snapshot.get('news_risk_level', '无'),
        'thesis_still_valid': 1 if decision.get('thesis_still_valid', True) else 0,
        'action_code': decision.get('action_code', 'WATCH'),
        'action': decision.get('action', ''),
        'confidence': decision.get('confidence', 5.0),
        'margin_of_safety': decision.get('margin_of_safety', '未知'),
        'reasoning': decision.get('reasoning', ''),
        # 决策输入可还原：Claude 决策时看到的关键输入随记录落库
        'news_text': snapshot.get('news_text', ''),
        'thesis': snapshot.get('thesis', ''),
        'invalidation_condition': snapshot.get('invalidation_condition', ''),
        'created_at': datetime.now(),
    }
    rec_id = eod_analysis_db.save_analysis_record(record)
    logger.info(f"[{code}] 决策已写回: {record['action_code']} — {record['reasoning'][:50]}")

    if push and record['action_code'] != 'HOLD':
        _send_webhook(_format_notification(record, record))
    return rec_id


# ==================== 调度辅助：仅刷数据 ====================

def refresh_all_data(codes: Optional[List[str]] = None) -> int:
    """
    批量刷新启用观察股票（或指定 codes）的 K线 + 基本面缓存，不做决策。
    供调度器每日收盘后保持数据新鲜。返回处理的股票数。
    """
    if codes:
        targets = [c for c in codes]
    else:
        targets = [s['code'] for s in eod_analysis_db.get_all_watch_stocks(enabled_only=True)]

    for code in targets:
        try:
            refresh_kline_cache(code)
            refresh_fundamental_cache(code)
        except Exception as e:
            logger.error(f"[{code}] 数据刷新异常: {e}")
    return len(targets)
