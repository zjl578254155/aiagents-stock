"""
Token用量追踪数据库模块
记录每次AI API调用的Token消耗和预估费用
"""

import sqlite3
import os
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


class TokenUsageDB:
    """Token用量数据库"""

    def __init__(self, db_path: str = "data/token_usage.db"):
        self.db_path = db_path
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self._init_database()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_database(self):
        """初始化数据库表"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS token_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                model TEXT NOT NULL,
                feature TEXT NOT NULL,
                caller TEXT,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                estimated_cost REAL NOT NULL DEFAULT 0.0,
                success INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_token_usage_timestamp ON token_usage(timestamp)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_token_usage_model ON token_usage(model)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_token_usage_feature ON token_usage(feature)')
        conn.commit()
        conn.close()

    def record_usage(self, timestamp: str, model: str, feature: str, caller: str,
                     prompt_tokens: int, completion_tokens: int, total_tokens: int,
                     estimated_cost: float, success: bool = True):
        """记录一次API调用的Token用量"""
        try:
            conn = self._get_connection()
            conn.execute(
                '''INSERT INTO token_usage
                   (timestamp, model, feature, caller, prompt_tokens, completion_tokens,
                    total_tokens, estimated_cost, success)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (timestamp, model, feature, caller, prompt_tokens, completion_tokens,
                 total_tokens, estimated_cost, 1 if success else 0)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"记录Token用量失败: {e}")

    def get_daily_summary(self, days: int = 30) -> List[Dict]:
        """获取每日汇总数据"""
        conn = self._get_connection()
        since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        rows = conn.execute(
            '''SELECT DATE(timestamp) as date,
                      SUM(total_tokens) as total_tokens,
                      SUM(prompt_tokens) as prompt_tokens,
                      SUM(completion_tokens) as completion_tokens,
                      SUM(estimated_cost) as total_cost,
                      COUNT(*) as call_count
               FROM token_usage
               WHERE DATE(timestamp) >= ?
               GROUP BY DATE(timestamp)
               ORDER BY date''',
            (since,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_model_breakdown(self, days: int = 30) -> List[Dict]:
        """按模型统计"""
        conn = self._get_connection()
        since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        rows = conn.execute(
            '''SELECT model,
                      SUM(total_tokens) as total_tokens,
                      SUM(estimated_cost) as total_cost,
                      COUNT(*) as call_count
               FROM token_usage
               WHERE DATE(timestamp) >= ?
               GROUP BY model
               ORDER BY total_tokens DESC''',
            (since,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_feature_breakdown(self, days: int = 30) -> List[Dict]:
        """按功能统计"""
        conn = self._get_connection()
        since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        rows = conn.execute(
            '''SELECT feature,
                      SUM(total_tokens) as total_tokens,
                      SUM(estimated_cost) as total_cost,
                      COUNT(*) as call_count
               FROM token_usage
               WHERE DATE(timestamp) >= ?
               GROUP BY feature
               ORDER BY total_tokens DESC''',
            (since,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_recent_records(self, limit: int = 100) -> List[Dict]:
        """获取最近的调用记录"""
        conn = self._get_connection()
        rows = conn.execute(
            '''SELECT timestamp, model, feature, caller,
                      prompt_tokens, completion_tokens, total_tokens,
                      estimated_cost, success
               FROM token_usage
               ORDER BY id DESC
               LIMIT ?''',
            (limit,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_total_stats(self) -> Dict:
        """获取总体统计"""
        conn = self._get_connection()
        row = conn.execute(
            '''SELECT COALESCE(SUM(total_tokens), 0) as total_tokens,
                      COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
                      COALESCE(SUM(completion_tokens), 0) as completion_tokens,
                      COALESCE(SUM(estimated_cost), 0) as total_cost,
                      COUNT(*) as total_calls,
                      MIN(DATE(timestamp)) as first_date,
                      MAX(DATE(timestamp)) as last_date
               FROM token_usage'''
        ).fetchone()
        conn.close()

        result = dict(row)
        if result['first_date'] and result['last_date']:
            first = datetime.strptime(result['first_date'], '%Y-%m-%d')
            last = datetime.strptime(result['last_date'], '%Y-%m-%d')
            result['days_tracked'] = max((last - first).days + 1, 1)
        else:
            result['days_tracked'] = 0
        return result

    def get_hourly_distribution(self, days: int = 7) -> List[Dict]:
        """获取按小时分布"""
        conn = self._get_connection()
        since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        rows = conn.execute(
            '''SELECT CAST(strftime('%H', timestamp) AS INTEGER) as hour,
                      COUNT(*) as call_count,
                      SUM(total_tokens) as total_tokens
               FROM token_usage
               WHERE DATE(timestamp) >= ?
               GROUP BY hour
               ORDER BY hour''',
            (since,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def cleanup_old_records(self, days_to_keep: int = 90) -> int:
        """清理旧记录"""
        conn = self._get_connection()
        cutoff = (datetime.now() - timedelta(days=days_to_keep)).strftime('%Y-%m-%d')
        cursor = conn.execute('DELETE FROM token_usage WHERE DATE(timestamp) < ?', (cutoff,))
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        return deleted


# 全局实例
token_usage_db = TokenUsageDB()
