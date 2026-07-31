"""
日内交易信号服务

基于 TDX 实时报价 + 5分钟K线，结合 EOD 决策上下文，
生成五种日内时机信号（GAP/BREAKOUT/VWAP_DEV/PM_WEAK/TAIL_SURGE），
辅助人工判断买卖时机。不做自动决策，不写 EOD 数据库。
"""

import csv
import logging
import os
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from data_sources.tdx_intraday_data import (
    calc_volume_ratio, calc_vwap, get_minute5_klines, get_realtime_quote,
)
from db.eod_analysis_db import eod_analysis_db

logger = logging.getLogger(__name__)

# 当日内存状态: {code: state_dict}，每日自动重置
_state: Dict[str, Dict] = {}
_state_date: Optional[str] = None


# ==================== 会话判断 ====================

def get_current_session() -> str:
    """
    返回当前 A 股交易时段:
    PRE | OPEN_UNSTABLE | MAIN | TAIL | AUCTION_CLOSE | CLOSED
    """
    t = datetime.now().hour * 60 + datetime.now().minute
    if t < 555:    # < 9:15
        return 'CLOSED'
    elif t < 570:  # 9:15-9:30
        return 'PRE'
    elif t < 600:  # 9:30-10:00
        return 'OPEN_UNSTABLE'
    elif t < 870:  # 10:00-14:30
        return 'MAIN'
    elif t < 897:  # 14:30-14:57
        return 'TAIL'
    elif t <= 900: # 14:57-15:00
        return 'AUCTION_CLOSE'
    else:
        return 'CLOSED'


# ==================== 状态管理 ====================

def _get_state(code: str) -> Dict:
    """获取或初始化当日状态，每天自动重置"""
    global _state, _state_date
    today = date.today().strftime('%Y-%m-%d')
    if _state_date != today:
        _state = {}
        _state_date = today
    if code not in _state:
        _state[code] = {
            'code': code,
            'name': '',
            'today_signals': [],
            'vwap': None,
            'volume_ratio': None,
            'last_price': None,
            'prev_close': None,
            'day_high': None,
            'change_pct': None,
            'last_update': None,
            'eod_action': 'UNKNOWN',
            # 单次触发标志
            'gap_fired': False,
            'pm_weak_fired': False,
            'prev_day_high': 0,
            'prev_change_pct': None,
            # BREAKOUT 冷却：30分钟内不重复触发
            'last_breakout_time': None,
        }
    return _state[code]


# ==================== EOD 协作矩阵 ====================

def _apply_eod_matrix(sig_type: str, eod_action: str, change_pct: float,
                      vwap_dev: Optional[float], vol_ratio: Optional[float],
                      session: str) -> Tuple[str, str, str]:
    """返回 (recommendation, urgency, reasoning)"""
    tail = "（尾盘窗口）" if session == 'TAIL' else ""

    if eod_action == 'HOLD':
        if sig_type == 'GAP' and change_pct >= 5:
            return ('考虑减1/3止盈再观察', 'MEDIUM',
                    f'高开{change_pct:.1f}%估值溢价，EOD=HOLD可先止盈部分观察承接')
        if change_pct <= -8:
            return ('触发-8%止损线，人工复核EOD逻辑', 'HIGH',
                    f'跌幅{change_pct:.1f}%，请核实持仓逻辑是否失效')
        if sig_type == 'BREAKOUT':
            return ('观望，持有不追高', 'LOW',
                    f'量比{vol_ratio}量价突破，EOD=HOLD维持原判不追高')
        return ('维持持有', 'LOW', f'EOD=HOLD，{sig_type}不构成操作依据')

    elif eod_action == 'TRIM':
        if sig_type == 'VWAP_DEV' and vwap_dev and vwap_dev > 3:
            return (f'✅ 此刻执行减仓{tail}', 'HIGH',
                    f'价高于VWAP {vwap_dev:.1f}%，流动性好；EOD=TRIM适合执行')
        if sig_type == 'BREAKOUT':
            return (f'✅ 量价配合减仓不压价{tail}', 'HIGH',
                    f'量比{vol_ratio}价创日高，卖出冲击成本低；EOD=TRIM')
        if change_pct <= -2:
            return ('等反弹窗口再减', 'LOW',
                    f'跌{change_pct:.1f}%弱势减仓冲击成本高，等价格回VWAP附近')
        if session == 'TAIL':
            return ('✅ 今日最后机会，可执行减仓', 'MEDIUM',
                    'EOD=TRIM，若今日未操作建议尾盘执行')
        return ('等更好时机', 'LOW', f'EOD=TRIM，{sig_type}时机一般，继续等待')

    elif eod_action == 'WATCH':
        if sig_type == 'PM_WEAK':
            return ('⚠️ WATCH升级候选，建议今晚复核', 'MEDIUM',
                    '午后转弱，走势与逻辑恶化方向一致，建议重新评估EOD')
        if sig_type == 'TAIL_SURGE' and change_pct < 0:
            return ('⚠️ 尾盘抛售，考虑明日升级为EXIT', 'HIGH',
                    f'尾盘量比{vol_ratio}且价跌，机构出货迹象')
        return ('观望不操作', 'LOW', f'EOD=WATCH等基本面确认，{sig_type}不触发操作')

    elif eod_action == 'EXIT':
        return (f'📣 择机执行清仓{tail}', 'HIGH',
                f'EOD=EXIT，寻找流动性窗口；涨跌{change_pct:.1f}%量比{vol_ratio}')

    return ('无EOD决策，仅记录信号', 'LOW', f'{sig_type}信号，尚无EOD决策供参考')


# ==================== 信号检测 ====================

def _build_signal(sig_type: str, quote: Dict, vwap: Optional[float],
                  vol_ratio: Optional[float], eod_action: str, session: str) -> Dict:
    price = quote['price']
    change_pct = quote['change_pct']
    vwap_dev = round((price - vwap) / vwap * 100, 2) if vwap and vwap > 0 else None
    rec, urgency, reasoning = _apply_eod_matrix(
        sig_type, eod_action, change_pct, vwap_dev, vol_ratio, session
    )
    return {
        'time': datetime.now().strftime('%H:%M'),
        'type': sig_type,
        'price': price,
        'change_pct': change_pct,
        'volume_ratio': vol_ratio,
        'vwap': vwap,
        'vwap_deviation': vwap_dev,
        'eod_action': eod_action,
        'recommendation': rec,
        'urgency': urgency,
        'reasoning': reasoning,
    }


def _detect_signals(code: str, quote: Dict, klines: List[Dict],
                    vwap: Optional[float], vol_ratio: Optional[float],
                    session: str, eod_action: str) -> List[Dict]:
    """检测本轮新信号，返回新增列表"""
    state = _get_state(code)
    new_signals = []

    price = quote['price']
    change_pct = quote['change_pct']
    prev_close = quote['prev_close']
    open_price = quote['open']
    day_high = quote['high']

    def emit(sig_type: str):
        sig = _build_signal(sig_type, quote, vwap, vol_ratio, eod_action, session)
        new_signals.append(sig)
        state['today_signals'].append(sig)

    # 1. GAP — 只在开盘不稳定期触发一次
    if session == 'OPEN_UNSTABLE' and not state['gap_fired']:
        if prev_close > 0 and open_price > 0:
            gap_pct = (open_price - prev_close) / prev_close * 100
            if abs(gap_pct) >= 3:
                emit('GAP')
                state['gap_fired'] = True

    # 2. BREAKOUT — 价创日高且量比>=2，30分钟内只触发一次（避免趋势股每5分钟刷出）
    if session in ('OPEN_UNSTABLE', 'MAIN', 'TAIL'):
        prev_high = state.get('prev_day_high', 0)
        last_bt = state.get('last_breakout_time')
        bt_cooldown_ok = (last_bt is None or
                          (datetime.now() - last_bt).total_seconds() >= 1800)
        if vol_ratio and vol_ratio >= 2 and day_high > 0 and prev_high > 0 \
                and day_high > prev_high and bt_cooldown_ok:
            emit('BREAKOUT')
            state['last_breakout_time'] = datetime.now()
    state['prev_day_high'] = day_high

    # 3. VWAP_DEV — 偏离VWAP >=±3%，同方向不连续重复；反方向偏离视为独立信号
    if session in ('MAIN', 'TAIL') and vwap and vwap > 0:
        vwap_dev = (price - vwap) / vwap * 100
        if abs(vwap_dev) >= 3:
            cur_up = vwap_dev > 0
            recent_same_dir = [
                s for s in state['today_signals'][-6:]
                if s['type'] == 'VWAP_DEV'
                and (s.get('vwap_deviation', 0) > 0) == cur_up
            ]
            if not recent_same_dir:
                emit('VWAP_DEV')

    # 4. PM_WEAK — 13:30后由正转负，只触发一次
    if session in ('MAIN', 'TAIL') and not state['pm_weak_fired']:
        now_t = datetime.now().hour * 60 + datetime.now().minute
        if now_t >= 810:  # 13:30
            prev_chg = state.get('prev_change_pct')
            if prev_chg is not None and prev_chg > 0 and change_pct <= 0:
                emit('PM_WEAK')
                state['pm_weak_fired'] = True

    state['prev_change_pct'] = change_pct

    # 5. TAIL_SURGE — 尾盘量比>=3且5分钟内涨跌>=±2%
    if session == 'TAIL' and vol_ratio and vol_ratio >= 3 and len(klines) >= 3:
        recent = klines[-1]['close']
        base = klines[-3]['close']
        if base > 0 and abs((recent - base) / base * 100) >= 2:
            already = [s for s in state['today_signals'] if s['type'] == 'TAIL_SURGE']
            if not already:
                emit('TAIL_SURGE')

    return new_signals


# ==================== 主接口 ====================

def refresh_stock(code: str) -> Dict:
    """刷新单股日内状态，返回更新后的 state"""
    session = get_current_session()
    state = _get_state(code)

    if session in ('CLOSED', 'AUCTION_CLOSE'):
        state['last_update'] = datetime.now().strftime('%H:%M:%S')
        return state

    quote = get_realtime_quote(code)
    if not quote:
        state['last_update'] = datetime.now().strftime('%H:%M:%S')
        return state

    klines = get_minute5_klines(code)
    vwap = calc_vwap(klines) if klines else None
    vol_ratio = calc_volume_ratio(code, quote['total_hand'])

    rec = eod_analysis_db.get_latest_record(code)
    eod_action = rec['action_code'] if rec and rec.get('action_code') else 'UNKNOWN'

    new_signals = _detect_signals(code, quote, klines, vwap, vol_ratio, session, eod_action)
    if new_signals:
        _save_signals_csv(code, new_signals)

    state.update({
        'name': quote.get('name', state.get('name', '')),
        'vwap': vwap,
        'volume_ratio': vol_ratio,
        'last_price': quote['price'],
        'prev_close': quote['prev_close'],
        'change_pct': quote['change_pct'],
        'day_high': quote['high'],
        'last_update': datetime.now().strftime('%H:%M:%S'),
        'eod_action': eod_action,
    })
    return state


def refresh_all(codes: Optional[List[str]] = None) -> Dict[str, Dict]:
    """批量刷新，codes=None 时读观察股票池"""
    if codes is None:
        stocks = eod_analysis_db.get_all_watch_stocks(enabled_only=True)
        codes = [s['code'] for s in stocks]
    result = {}
    for code in codes:
        try:
            result[code] = refresh_stock(code)
        except Exception as e:
            logger.error(f"[{code}] refresh_stock 异常: {e}")
    return result


def get_all_states() -> Dict[str, Dict]:
    """返回当日所有缓存状态（不触发新请求）"""
    return dict(_state)


def get_today_signals(code: Optional[str] = None) -> List[Dict]:
    """返回今日所有信号，按时间倒序；可按 code 过滤"""
    signals = []
    for c, s in _state.items():
        if code and c != code:
            continue
        for sig in s.get('today_signals', []):
            signals.append({'code': c, 'name': s.get('name', c), **sig})
    return sorted(signals, key=lambda x: x['time'], reverse=True)


def _save_signals_csv(code: str, signals: List[Dict]):
    """追加写入当日 CSV 日志"""
    today = date.today().strftime('%Y%m%d')
    dir_path = 'data/intraday_signals'
    os.makedirs(dir_path, exist_ok=True)
    filepath = f'{dir_path}/{today}.csv'
    is_new = not os.path.exists(filepath)
    fields = ['code', 'time', 'type', 'price', 'change_pct', 'volume_ratio',
              'vwap', 'vwap_deviation', 'eod_action', 'recommendation', 'urgency', 'reasoning']
    with open(filepath, 'a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        if is_new:
            writer.writeheader()
        for sig in signals:
            writer.writerow({'code': code, **sig})
