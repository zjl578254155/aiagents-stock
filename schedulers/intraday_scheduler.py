"""
日内实时交易信号 - 轮询调度器

交易时段内每5分钟触发 refresh_all()，非交易时段自动跳过。
"""

import logging
import threading
import time
from datetime import datetime
from typing import Optional

from services.intraday_signal_service import get_current_session, refresh_all

logger = logging.getLogger(__name__)


class IntradayScheduler:
    def __init__(self, interval_seconds: int = 300):
        self.interval = interval_seconds
        self._is_running = False
        self._thread: Optional[threading.Thread] = None
        self.last_run_time: Optional[str] = None
        self.last_run_result: Optional[str] = None

    @property
    def is_running(self) -> bool:
        return self._is_running

    def _loop(self):
        while self._is_running:
            session = get_current_session()
            if session not in ('CLOSED', 'AUCTION_CLOSE'):
                self._run_once()
            else:
                logger.debug(f"[IntradayScheduler] {session}，跳过")
            time.sleep(self.interval)

    def _run_once(self):
        now = datetime.now()
        logger.info(f"[IntradayScheduler] 刷新 {now.strftime('%H:%M:%S')}")
        self.last_run_time = now.strftime('%Y-%m-%d %H:%M:%S')
        try:
            states = refresh_all()
            total = sum(len(s.get('today_signals', [])) for s in states.values())
            self.last_run_result = f"已刷新 {len(states)} 只，今日累计信号 {total} 条"
            logger.info(f"[IntradayScheduler] {self.last_run_result}")
        except Exception as e:
            self.last_run_result = f"异常: {e}"
            logger.error(f"[IntradayScheduler] 异常: {e}")

    def start(self):
        if self._is_running:
            return
        self._is_running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name='IntradayScheduler'
        )
        self._thread.start()
        logger.info(f"[IntradayScheduler] 已启动，间隔 {self.interval}s")

    def stop(self):
        self._is_running = False
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        logger.info("[IntradayScheduler] 已停止")

    def run_now(self) -> str:
        """手动立即刷新一次（后台线程）"""
        def _bg():
            self._run_once()
        threading.Thread(target=_bg, daemon=True).start()
        return "日内数据刷新已在后台启动"

    def get_status(self) -> dict:
        return {
            'is_running': self._is_running,
            'interval': self.interval,
            'last_run_time': self.last_run_time,
            'last_run_result': self.last_run_result,
            'session': get_current_session(),
        }


intraday_scheduler = IntradayScheduler()
