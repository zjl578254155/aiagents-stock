"""
EOD持仓逻辑评估 - 定时调度器（仅刷数据）

每个交易日 15:30 收盘后触发：
  - 刷新所有持仓的 K线 + 基本面缓存，保持数据新鲜
  - 不做决策（四档判断由 Claude 会话结合快照做出并写回）
"""

import schedule
import threading
import time
import logging
from datetime import datetime
from typing import Optional

from services.eod_analysis_service import refresh_all_data, refresh_kline_cache, export_snapshots
from db.eod_analysis_db import eod_analysis_db

logger = logging.getLogger(__name__)


class EODScheduler:
    def __init__(self):
        self.schedule_time = "15:30"
        self._is_running = False
        self._thread: Optional[threading.Thread] = None
        self.last_run_time: Optional[str] = None
        self.last_run_result: Optional[str] = None

    @property
    def is_running(self) -> bool:
        return self._is_running

    def set_schedule_time(self, time_str: str):
        try:
            datetime.strptime(time_str, "%H:%M")
            self.schedule_time = time_str
            if self._is_running:
                self._reschedule()
            logger.info(f"[EODScheduler] 调度时间设置为 {time_str}")
        except ValueError:
            logger.error(f"[EODScheduler] 无效时间格式: {time_str}")

    def _reschedule(self):
        schedule.clear('eod')
        schedule.every().day.at(self.schedule_time).do(self._run_task).tag('eod')

    def _run_task(self):
        now = datetime.now()
        logger.info(f"[EODScheduler] 开始刷新 EOD 数据缓存 {now.strftime('%Y-%m-%d %H:%M')}")
        self.last_run_time = now.strftime('%Y-%m-%d %H:%M:%S')
        try:
            count = refresh_all_data()
            # 顺手清理超期记录
            eod_analysis_db.cleanup_old_records(days=1825)
            self.last_run_result = f"已刷新 {count} 只数据缓存"
            logger.info(f"[EODScheduler] 完成: {self.last_run_result}")
        except Exception as e:
            self.last_run_result = f"异常: {e}"
            logger.error(f"[EODScheduler] 运行异常: {e}")

    def _loop(self):
        self._reschedule()
        while self._is_running:
            schedule.run_pending()
            time.sleep(30)
        schedule.clear('eod')

    def start(self):
        if self._is_running:
            logger.info("[EODScheduler] 已在运行")
            return
        self._is_running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name='EODScheduler')
        self._thread.start()
        logger.info(f"[EODScheduler] 已启动，每日 {self.schedule_time} 执行")

    def stop(self):
        self._is_running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("[EODScheduler] 已停止")

    def run_now(self) -> str:
        """手动立即刷新数据并导出快照（在后台线程执行）"""
        def _bg():
            try:
                count = refresh_all_data()
                path = export_snapshots()
                self.last_run_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                self.last_run_result = f"已刷新 {count} 只，快照导出到 {path}"
            except Exception as e:
                self.last_run_result = f"手动触发异常: {e}"
                logger.error(f"[EODScheduler] 手动触发异常: {e}")
        t = threading.Thread(target=_bg, daemon=True)
        t.start()
        return "数据刷新+快照导出已在后台启动，完成后可让 Claude 分析快照"

    def refresh_kline_only(self) -> str:
        """仅刷新所有持仓K线缓存，不触发AI分析"""
        def _bg():
            stocks = eod_analysis_db.get_all_watch_stocks(enabled_only=True)
            for s in stocks:
                try:
                    refresh_kline_cache(s['code'])
                except Exception as e:
                    logger.error(f"[{s['code']}] K线缓存刷新失败: {e}")
        t = threading.Thread(target=_bg, daemon=True)
        t.start()
        return "K线缓存刷新已在后台启动"

    def get_status(self) -> dict:
        return {
            'is_running': self._is_running,
            'schedule_time': self.schedule_time,
            'last_run_time': self.last_run_time,
            'last_run_result': self.last_run_result,
        }


eod_scheduler = EODScheduler()
