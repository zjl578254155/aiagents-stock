"""
TDX 日内数据源：实时报价 + 5分钟K线 + VWAP + 量比
"""

import logging
import requests
from datetime import datetime, date
from typing import Optional, List, Dict

from core.config import TDX_CONFIG

logger = logging.getLogger(__name__)

TDX_BASE_URL = TDX_CONFIG['base_url']


def get_realtime_quote(code: str) -> Optional[Dict]:
    """实时报价，返回标准化字典（价格单位：元）"""
    try:
        resp = requests.get(f"{TDX_BASE_URL}/api/quote", params={'code': code}, timeout=10)
        result = resp.json()
        if result.get('code') != 0:
            logger.error(f"[{code}] TDX quote 失败: {result.get('message')}")
            return None
        data_list = result.get('data', [])
        if not data_list:
            return None
        d = data_list[0]
        k = d.get('K', {})
        prev_close = k.get('Last', 0) / 1000
        current = k.get('Close', 0) / 1000
        open_price = k.get('Open', 0) / 1000
        high = k.get('High', 0) / 1000
        low = k.get('Low', 0) / 1000
        change_pct = round((current - prev_close) / prev_close * 100, 2) if prev_close else 0.0
        return {
            'code': code,
            'name': d.get('name', ''),
            'price': current,
            'prev_close': prev_close,
            'open': open_price,
            'high': high,
            'low': low,
            'change_pct': change_pct,
            'total_hand': d.get('TotalHand', 0),
            'amount': d.get('Amount', 0) / 1000,
            'server_time': d.get('ServerTime', 0),
        }
    except requests.exceptions.Timeout:
        logger.error(f"[{code}] TDX quote 超时")
    except requests.exceptions.ConnectionError:
        logger.error(f"[{code}] TDX 连接失败: {TDX_BASE_URL}")
    except Exception as e:
        logger.error(f"[{code}] TDX quote 异常: {type(e).__name__}: {e}")
    return None


def get_minute5_klines(code: str) -> List[Dict]:
    """5分钟K线，只返回今日记录（价格单位：元）"""
    try:
        resp = requests.get(
            f"{TDX_BASE_URL}/api/kline",
            params={'code': code, 'type': 'minute5'},
            timeout=15
        )
        result = resp.json()
        if result.get('code') != 0:
            logger.error(f"[{code}] TDX minute5 失败: {result.get('message')}")
            return []
        kline_list = result.get('data', {}).get('List', [])
        today_str = date.today().strftime('%Y-%m-%d')
        bars = []
        for item in kline_list:
            t = item.get('Time', '')
            if not t.startswith(today_str):
                continue
            bars.append({
                'time': t,
                'open': item.get('Open', 0) / 1000,
                'high': item.get('High', 0) / 1000,
                'low': item.get('Low', 0) / 1000,
                'close': item.get('Close', 0) / 1000,
                'volume': item.get('Volume', 0),
                'amount': item.get('Amount', 0) / 1000,
            })
        return bars
    except requests.exceptions.Timeout:
        logger.error(f"[{code}] TDX minute5 超时")
    except requests.exceptions.ConnectionError:
        logger.error(f"[{code}] TDX 连接失败: {TDX_BASE_URL}")
    except Exception as e:
        logger.error(f"[{code}] TDX minute5 异常: {type(e).__name__}: {e}")
    return []


def calc_vwap(klines: List[Dict]) -> Optional[float]:
    """从5分钟K线计算VWAP（典型价加权）"""
    total_vol = sum(b['volume'] for b in klines)
    if total_vol == 0:
        return None
    vwap = sum(((b['high'] + b['low'] + b['close']) / 3) * b['volume'] for b in klines) / total_vol
    return round(vwap, 3)


def calc_volume_ratio(code: str, total_hand: int) -> Optional[float]:
    """
    量比 = 今日成交量 / (近5日均量 × 已过交易时间比例)
    近5日均量从 eod_kline_cache 读取
    """
    from db.eod_analysis_db import eod_analysis_db
    records = eod_analysis_db.get_kline_records(code, limit=10)
    today_str = date.today().strftime('%Y-%m-%d')
    completed = [r for r in records if r['trade_date'] != today_str]
    if len(completed) < 3:
        return None
    recent5 = completed[-5:]
    avg_vol = sum(r['volume'] for r in recent5) / len(recent5)
    if avg_vol == 0:
        return None
    elapsed = _calc_elapsed_trading_minutes()
    if elapsed <= 0:
        return None
    return round(total_hand / (avg_vol * elapsed / 240), 2)


def _calc_elapsed_trading_minutes() -> int:
    """当日已过交易分钟数（不含午休，上午120 + 下午120 = 240分钟）"""
    now = datetime.now()
    t = now.hour * 60 + now.minute
    OPEN = 570    # 9:30
    NOON = 690    # 11:30
    PM = 780      # 13:00
    CLOSE = 900   # 15:00

    if t < OPEN:
        return 0
    elif t <= NOON:
        return t - OPEN
    elif t < PM:
        return 120
    elif t <= CLOSE:
        return 120 + (t - PM)
    else:
        return 240
