"""
EOD持仓逻辑评估模块 - 数据库管理
"""

import sqlite3
from datetime import datetime
from typing import List, Dict, Optional
import os

DB_PATH = "data/eod_analysis.db"


class EODAnalysisDB:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_database()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_database(self):
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            # 观察股票池：持仓 + 买入逻辑
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS eod_watch_stocks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    cost_price REAL,
                    quantity INTEGER,
                    moat_type TEXT DEFAULT '其他',
                    thesis TEXT,
                    valuation_basis TEXT,
                    main_risk TEXT,
                    invalidation_condition TEXT,
                    enabled INTEGER DEFAULT 1,
                    source TEXT DEFAULT 'manual',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # K线缓存：增量更新，首次约3600条，后续仅写新增
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS eod_kline_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume REAL,
                    data_source TEXT DEFAULT 'tdx',
                    UNIQUE(code, trade_date)
                )
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_kline_code_date
                ON eod_kline_cache(code, trade_date)
            ''')

            # 基本面缓存：每次更新用REPLACE INTO覆盖最新报告期
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS eod_fundamental_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    report_date TEXT NOT NULL,
                    eps REAL,
                    bvps REAL,
                    net_margin REAL,
                    debt_ratio REAL,
                    gross_margin REAL,
                    roe_approx REAL,
                    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(code, report_date)
                )
            ''')

            # 分析记录：每次触发保存一条，供历史回溯
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS eod_analysis_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    trigger_type TEXT NOT NULL,
                    close_price REAL,
                    cost_price REAL,
                    pnl_pct REAL,
                    ma60 REAL,
                    ma250 REAL,
                    vs_ma250_pct REAL,
                    latest_report_date TEXT,
                    net_margin REAL,
                    debt_ratio REAL,
                    roe_approx REAL,
                    roe_trend TEXT,
                    has_major_event INTEGER DEFAULT 0,
                    event_summary TEXT,
                    news_risk_level TEXT DEFAULT '无',
                    thesis_still_valid INTEGER DEFAULT 1,
                    action_code TEXT,
                    action TEXT,
                    confidence REAL,
                    margin_of_safety TEXT,
                    reasoning TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(code, trade_date, trigger_type)
                )
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_records_code_date
                ON eod_analysis_records(code, trade_date DESC)
            ''')

            # 增量迁移：决策输入可还原三列（2026-07-31，历史行为 NULL）
            existing_cols = {r[1] for r in cursor.execute(
                "PRAGMA table_info(eod_analysis_records)").fetchall()}
            for col in ('news_text', 'thesis', 'invalidation_condition'):
                if col not in existing_cols:
                    cursor.execute(f"ALTER TABLE eod_analysis_records ADD COLUMN {col} TEXT")

            # 买卖点计划表：条件动作的结构化真理源（周复盘执行对账/命中率统计/条件价提醒都读它）
            # 写入只走 scripts/portfolio_ledger.py
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS eod_action_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    plan_date TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    condition_text TEXT NOT NULL,
                    trigger_price REAL,
                    qty INTEGER,
                    status TEXT DEFAULT 'open',
                    close_date TEXT,
                    close_note TEXT,
                    record_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # 成交流水表：持仓台账变更的历史真相源（台账是快照，流水是历史）
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS eod_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    code TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    price REAL NOT NULL,
                    qty INTEGER NOT NULL,
                    amount REAL,
                    note TEXT,
                    plan_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            conn.commit()
            print(f"[OK] EOD数据库初始化成功: {self.db_path}")
        except Exception as e:
            conn.rollback()
            print(f"[ERROR] EOD数据库初始化失败: {e}")
            raise
        finally:
            conn.close()

    # ==================== 观察股票池 ====================

    def add_watch_stock(self, code: str, name: str, cost_price: Optional[float] = None,
                        quantity: Optional[int] = None, moat_type: str = '其他',
                        thesis: str = '', valuation_basis: str = '',
                        main_risk: str = '', invalidation_condition: str = '',
                        source: str = 'manual') -> int:
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO eod_watch_stocks
                (code, name, cost_price, quantity, moat_type, thesis, valuation_basis,
                 main_risk, invalidation_condition, source, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ''', (code, name, cost_price, quantity, moat_type, thesis, valuation_basis,
                  main_risk, invalidation_condition, source,
                  datetime.now(), datetime.now()))
            conn.commit()
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            raise ValueError(f"股票代码 {code} 已存在")
        finally:
            conn.close()

    def update_watch_stock(self, code: str, **kwargs) -> bool:
        allowed = ['name', 'cost_price', 'quantity', 'moat_type', 'thesis',
                   'valuation_basis', 'main_risk', 'invalidation_condition', 'enabled']
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return False
        fields['updated_at'] = datetime.now()
        set_clause = ', '.join(f"{k} = ?" for k in fields)
        conn = self._get_connection()
        try:
            conn.execute(
                f"UPDATE eod_watch_stocks SET {set_clause} WHERE code = ?",
                list(fields.values()) + [code]
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def delete_watch_stock(self, code: str) -> bool:
        conn = self._get_connection()
        try:
            cur = conn.execute("DELETE FROM eod_watch_stocks WHERE code = ?", (code,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def get_watch_stock(self, code: str) -> Optional[Dict]:
        conn = self._get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM eod_watch_stocks WHERE code = ?", (code,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_all_watch_stocks(self, enabled_only: bool = True) -> List[Dict]:
        conn = self._get_connection()
        try:
            if enabled_only:
                rows = conn.execute(
                    "SELECT * FROM eod_watch_stocks WHERE enabled = 1 ORDER BY created_at DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM eod_watch_stocks ORDER BY created_at DESC"
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ==================== K线缓存 ====================

    def get_latest_kline_date(self, code: str) -> Optional[str]:
        """返回缓存中该股票最新交易日，用于增量判断"""
        conn = self._get_connection()
        try:
            row = conn.execute(
                "SELECT MAX(trade_date) as max_date FROM eod_kline_cache WHERE code = ?",
                (code,)
            ).fetchone()
            return row['max_date'] if row else None
        finally:
            conn.close()

    def upsert_kline_records(self, code: str, records: List[Dict]) -> int:
        """批量upsert K线记录，返回插入/更新行数"""
        if not records:
            return 0
        conn = self._get_connection()
        try:
            cursor = conn.executemany('''
                INSERT OR REPLACE INTO eod_kline_cache
                (code, trade_date, open, high, low, close, volume, data_source)
                VALUES (:code, :trade_date, :open, :high, :low, :close, :volume, :data_source)
            ''', [{'code': code, **r} for r in records])
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def get_kline_records(self, code: str, limit: int = 300) -> List[Dict]:
        """获取最近N条K线，按日期升序（用于计算MA）"""
        conn = self._get_connection()
        try:
            rows = conn.execute('''
                SELECT * FROM eod_kline_cache
                WHERE code = ?
                ORDER BY trade_date DESC
                LIMIT ?
            ''', (code, limit)).fetchall()
            return list(reversed([dict(r) for r in rows]))
        finally:
            conn.close()

    def get_kline_count(self, code: str) -> int:
        conn = self._get_connection()
        try:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM eod_kline_cache WHERE code = ?", (code,)
            ).fetchone()
            return row['cnt']
        finally:
            conn.close()

    # ==================== 基本面缓存 ====================

    def upsert_fundamental(self, code: str, report_date: str, **kwargs) -> None:
        allowed = ['eps', 'bvps', 'net_margin', 'debt_ratio', 'gross_margin', 'roe_approx']
        fields = {k: kwargs[k] for k in allowed if k in kwargs}
        conn = self._get_connection()
        try:
            conn.execute('''
                INSERT OR REPLACE INTO eod_fundamental_cache
                (code, report_date, eps, bvps, net_margin, debt_ratio, gross_margin,
                 roe_approx, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (code, report_date,
                  fields.get('eps'), fields.get('bvps'), fields.get('net_margin'),
                  fields.get('debt_ratio'), fields.get('gross_margin'), fields.get('roe_approx'),
                  datetime.now()))
            conn.commit()
        finally:
            conn.close()

    def get_fundamentals(self, code: str, limit: int = 8) -> List[Dict]:
        """获取最近N期财报，按报告期升序"""
        conn = self._get_connection()
        try:
            rows = conn.execute('''
                SELECT * FROM eod_fundamental_cache
                WHERE code = ?
                ORDER BY report_date DESC
                LIMIT ?
            ''', (code, limit)).fetchall()
            return list(reversed([dict(r) for r in rows]))
        finally:
            conn.close()

    def get_latest_fundamental(self, code: str) -> Optional[Dict]:
        conn = self._get_connection()
        try:
            row = conn.execute('''
                SELECT * FROM eod_fundamental_cache
                WHERE code = ?
                ORDER BY report_date DESC
                LIMIT 1
            ''', (code,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # ==================== 分析记录 ====================

    def save_analysis_record(self, record: Dict) -> int:
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT OR REPLACE INTO eod_analysis_records
                (code, name, trade_date, trigger_type, close_price, cost_price, pnl_pct,
                 ma60, ma250, vs_ma250_pct, latest_report_date, net_margin, debt_ratio,
                 roe_approx, roe_trend, has_major_event, event_summary, news_risk_level,
                 thesis_still_valid, action_code, action, confidence, margin_of_safety,
                 reasoning, news_text, thesis, invalidation_condition, created_at)
                VALUES (:code, :name, :trade_date, :trigger_type, :close_price, :cost_price,
                        :pnl_pct, :ma60, :ma250, :vs_ma250_pct, :latest_report_date,
                        :net_margin, :debt_ratio, :roe_approx, :roe_trend, :has_major_event,
                        :event_summary, :news_risk_level, :thesis_still_valid, :action_code,
                        :action, :confidence, :margin_of_safety, :reasoning,
                        :news_text, :thesis, :invalidation_condition, :created_at)
            ''', {'news_text': '', 'thesis': '', 'invalidation_condition': '',
                  **record, 'created_at': record.get('created_at', datetime.now())})
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def get_latest_record(self, code: str) -> Optional[Dict]:
        conn = self._get_connection()
        try:
            row = conn.execute('''
                SELECT * FROM eod_analysis_records
                WHERE code = ?
                ORDER BY trade_date DESC, created_at DESC
                LIMIT 1
            ''', (code,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_records_by_code(self, code: str, limit: int = 30) -> List[Dict]:
        conn = self._get_connection()
        try:
            rows = conn.execute('''
                SELECT * FROM eod_analysis_records
                WHERE code = ?
                ORDER BY trade_date DESC, created_at DESC
                LIMIT ?
            ''', (code, limit)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_all_latest_records(self) -> List[Dict]:
        """每只股票取最新一条分析记录，供仪表盘展示"""
        conn = self._get_connection()
        try:
            rows = conn.execute('''
                SELECT r.*
                FROM eod_analysis_records r
                INNER JOIN (
                    SELECT code, MAX(trade_date || created_at) as max_key
                    FROM eod_analysis_records
                    GROUP BY code
                ) latest ON r.code = latest.code
                WHERE (r.trade_date || r.created_at) = latest.max_key
                ORDER BY r.trade_date DESC
            ''').fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def cleanup_old_records(self, days: int = 1825) -> int:
        """清理超过指定天数的分析记录（默认5年）"""
        conn = self._get_connection()
        try:
            cur = conn.execute('''
                DELETE FROM eod_analysis_records
                WHERE created_at < datetime('now', '-' || ? || ' days')
            ''', (days,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


eod_analysis_db = EODAnalysisDB()
