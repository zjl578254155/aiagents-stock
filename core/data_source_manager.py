"""
数据源管理器
实现akshare和tushare的自动切换机制
"""

import os
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()


class DataSourceManager:
    """数据源管理器 - 实现多数据源自动切换（东方财富→新浪→腾讯→Tushare）"""
    
    def __init__(self):
        self.tushare_token = os.getenv('TUSHARE_TOKEN', '')
        self.tushare_available = False
        self.tushare_api = None
        
        # 初始化tushare
        if self.tushare_token:
            try:
                import tushare as ts
                ts.set_token(self.tushare_token)
                self.tushare_api = ts.pro_api()
                self.tushare_available = True
                print("✅ Tushare数据源初始化成功")
            except Exception as e:
                print(f"⚠️ Tushare数据源初始化失败: {e}")
                self.tushare_available = False
        else:
            print("ℹ️ 未配置Tushare Token，将仅使用Akshare数据源")
    
    def _convert_to_sina_code(self, symbol):
        """将6位代码转换为新浪/腾讯格式（sz000001 / sh600000）"""
        if not symbol or len(symbol) != 6:
            return symbol
        if symbol.startswith('6'):
            return f"sh{symbol}"
        else:
            return f"sz{symbol}"
    
    def get_stock_hist_data(self, symbol, start_date=None, end_date=None, adjust='qfq'):
        """
        获取股票历史数据（东方财富→新浪→腾讯→Tushare 四级降级）
        
        Args:
            symbol: 股票代码（6位数字）
            start_date: 开始日期（格式：'20240101'或'2024-01-01'）
            end_date: 结束日期
            adjust: 复权类型（'qfq'前复权, 'hfq'后复权, ''不复权）
            
        Returns:
            DataFrame: 包含日期、开盘、收盘、最高、最低、成交量等列
        """
        import akshare as ak
        
        # 标准化日期格式
        if start_date:
            start_date = start_date.replace('-', '')
        if end_date:
            end_date = end_date.replace('-', '')
        else:
            end_date = datetime.now().strftime('%Y%m%d')
        
        # === 数据源1: 东方财富（akshare stock_zh_a_hist） ===
        try:
            print(f"[Akshare-东方财富] 正在获取 {symbol} 的历史数据...")
            
            df = ak.stock_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust=adjust
            )
            
            if df is not None and not df.empty:
                df = df.rename(columns={
                    '日期': 'date',
                    '开盘': 'open',
                    '收盘': 'close',
                    '最高': 'high',
                    '最低': 'low',
                    '成交量': 'volume',
                    '成交额': 'amount',
                    '振幅': 'amplitude',
                    '涨跌幅': 'pct_change',
                    '涨跌额': 'change',
                    '换手率': 'turnover'
                })
                df['date'] = pd.to_datetime(df['date'])
                print(f"[Akshare-东方财富] ✅ 成功获取 {len(df)} 条数据")
                return df
        except Exception as e:
            print(f"[Akshare-东方财富] ❌ 获取失败: {e}")
        
        # === 数据源2: 新浪财经（akshare stock_zh_a_daily） ===
        try:
            sina_code = self._convert_to_sina_code(symbol)
            print(f"[Akshare-新浪] 正在获取 {symbol} ({sina_code}) 的历史数据...")
            
            sina_adjust = "" if adjust == "" else "qfq" if adjust in ("qfq", "1") else "hfq"
            df = ak.stock_zh_a_daily(
                symbol=sina_code,
                start_date=start_date,
                end_date=end_date,
                adjust=sina_adjust
            )
            
            if df is not None and not df.empty:
                df['date'] = pd.to_datetime(df['date'])
                for col in ['open', 'high', 'low', 'close', 'volume', 'amount']:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors='coerce')
                print(f"[Akshare-新浪] ✅ 成功获取 {len(df)} 条数据")
                return df
        except Exception as e:
            print(f"[Akshare-新浪] ❌ 获取失败: {e}")
        
        # === 数据源3: 腾讯财经（akshare stock_zh_a_hist_tx） ===
        try:
            sina_code = self._convert_to_sina_code(symbol)
            start_fmt = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}" if start_date else None
            end_fmt = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}" if end_date else None
            print(f"[Akshare-腾讯] 正在获取 {symbol} 的历史数据...")
            
            tx_adjust = "" if adjust == "" else "qfq" if adjust in ("qfq", "1") else "hfq"
            df = ak.stock_zh_a_hist_tx(
                symbol=sina_code,
                start_date=start_fmt,
                end_date=end_fmt,
                adjust=tx_adjust
            )
            
            if df is not None and not df.empty:
                if 'volume' not in df.columns and 'amount' in df.columns:
                    df = df.rename(columns={'amount': 'volume'})
                df['date'] = pd.to_datetime(df['date'])
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors='coerce')
                print(f"[Akshare-腾讯] ✅ 成功获取 {len(df)} 条数据")
                return df
        except Exception as e:
            print(f"[Akshare-腾讯] ❌ 获取失败: {e}")
        
        # === 数据源4: Tushare ===
        if self.tushare_available:
            try:
                print(f"[Tushare] 正在获取 {symbol} 的历史数据（备用数据源）...")
                
                ts_code = self._convert_to_ts_code(symbol)
                
                adj_dict = {'qfq': 'qfq', 'hfq': 'hfq', '': None}
                adj = adj_dict.get(adjust, 'qfq')
                
                df = self.tushare_api.daily(
                    ts_code=ts_code,
                    start_date=start_date,
                    end_date=end_date,
                    adj=adj
                )
                
                if df is not None and not df.empty:
                    df = df.rename(columns={
                        'trade_date': 'date',
                        'vol': 'volume',
                        'amount': 'amount'
                    })
                    df['date'] = pd.to_datetime(df['date'])
                    df = df.sort_values('date')
                    df['volume'] = df['volume'] * 100
                    df['amount'] = df['amount'] * 1000
                    
                    print(f"[Tushare] ✅ 成功获取 {len(df)} 条数据")
                    return df
            except Exception as e:
                print(f"[Tushare] ❌ 获取失败: {e}")
        
        print("❌ 所有数据源均获取失败")
        return None
    
    def get_stock_basic_info(self, symbol):
        """
        获取股票基本信息（优先akshare，失败时使用tushare）
        
        Args:
            symbol: 股票代码
            
        Returns:
            dict: 股票基本信息
        """
        info = {
            "symbol": symbol,
            "name": "未知",
            "industry": "未知",
            "market": "未知"
        }
        
        # 优先使用akshare
        try:
            import akshare as ak
            print(f"[Akshare] 正在获取 {symbol} 的基本信息...")
            
            stock_info = ak.stock_individual_info_em(symbol=symbol)
            if stock_info is not None and not stock_info.empty:
                for _, row in stock_info.iterrows():
                    key = row['item']
                    value = row['value']
                    
                    if key == '股票简称':
                        info['name'] = value
                    elif key == '所处行业':
                        info['industry'] = value
                    elif key == '上市时间':
                        info['list_date'] = value
                    elif key == '总市值':
                        info['market_cap'] = value
                    elif key == '流通市值':
                        info['circulating_market_cap'] = value
                
                print(f"[Akshare] ✅ 成功获取基本信息")
                return info
        except Exception as e:
            print(f"[Akshare] ❌ 获取失败: {e}")
        
        # akshare失败，尝试tushare
        if self.tushare_available:
            try:
                print(f"[Tushare] 正在获取 {symbol} 的基本信息（备用数据源）...")
                
                ts_code = self._convert_to_ts_code(symbol)
                df = self.tushare_api.stock_basic(
                    ts_code=ts_code,
                    fields='ts_code,name,area,industry,market,list_date'
                )
                
                if df is not None and not df.empty:
                    info['name'] = df.iloc[0]['name']
                    info['industry'] = df.iloc[0]['industry']
                    info['market'] = df.iloc[0]['market']
                    info['list_date'] = df.iloc[0]['list_date']
                    
                    print(f"[Tushare] ✅ 成功获取基本信息")
                    return info
            except Exception as e:
                print(f"[Tushare] ❌ 获取失败: {e}")
        
        return info
    
    def get_realtime_quotes(self, symbol):
        """
        获取实时行情数据（东方财富→最近历史数据降级）
        
        Args:
            symbol: 股票代码
            
        Returns:
            dict: 实时行情数据
        """
        import akshare as ak
        quotes = {}
        
        # 数据源1: 东方财富实时行情
        try:
            print(f"[Akshare-东方财富] 正在获取 {symbol} 的实时行情...")
            
            df = ak.stock_zh_a_spot_em()
            stock_df = df[df['代码'] == symbol]
            
            if not stock_df.empty:
                row = stock_df.iloc[0]
                quotes = {
                    'symbol': symbol,
                    'name': row['名称'],
                    'price': row['最新价'],
                    'change_percent': row['涨跌幅'],
                    'change': row['涨跌额'],
                    'volume': row['成交量'],
                    'amount': row['成交额'],
                    'high': row['最高'],
                    'low': row['最低'],
                    'open': row['今开'],
                    'pre_close': row['昨收']
                }
                print(f"[Akshare-东方财富] ✅ 成功获取实时行情")
                return quotes
        except Exception as e:
            print(f"[Akshare-东方财富] ❌ 获取实时行情失败: {e}")
        
        # 数据源2: 用最近历史数据模拟实时行情（新浪/腾讯源）
        try:
            print(f"[降级] 使用最近历史数据获取 {symbol} 的行情...")
            hist_df = self.get_stock_hist_data(
                symbol=symbol,
                start_date=(datetime.now() - timedelta(days=7)).strftime('%Y%m%d'),
                end_date=datetime.now().strftime('%Y%m%d'),
                adjust='qfq'
            )
            if hist_df is not None and not hist_df.empty:
                latest = hist_df.iloc[-1]
                quotes = {
                    'symbol': symbol,
                    'price': latest.get('close'),
                    'high': latest.get('high'),
                    'low': latest.get('low'),
                    'open': latest.get('open'),
                    'volume': latest.get('volume'),
                    'amount': latest.get('amount'),
                }
                if len(hist_df) > 1:
                    prev = hist_df.iloc[-2]
                    prev_close = prev.get('close')
                    if prev_close and prev_close != 0:
                        quotes['pre_close'] = prev_close
                        quotes['change_percent'] = round(((latest['close'] - prev_close) / prev_close) * 100, 2)
                        quotes['change'] = round(latest['close'] - prev_close, 2)
                print(f"[降级] ✅ 成功从历史数据获取行情")
                return quotes
        except Exception as e:
            print(f"[降级] ❌ 获取失败: {e}")
        
        # 数据源3: Tushare
        if self.tushare_available:
            try:
                print(f"[Tushare] 正在获取 {symbol} 的实时行情（备用数据源）...")
                
                ts_code = self._convert_to_ts_code(symbol)
                df = self.tushare_api.daily(
                    ts_code=ts_code,
                    start_date=datetime.now().strftime('%Y%m%d'),
                    end_date=datetime.now().strftime('%Y%m%d')
                )
                
                if df is not None and not df.empty:
                    row = df.iloc[0]
                    quotes = {
                        'symbol': symbol,
                        'price': row['close'],
                        'change_percent': row['pct_chg'],
                        'volume': row['vol'] * 100,
                        'amount': row['amount'] * 1000,
                        'high': row['high'],
                        'low': row['low'],
                        'open': row['open'],
                        'pre_close': row['pre_close']
                    }
                    print(f"[Tushare] ✅ 成功获取实时行情")
                    return quotes
            except Exception as e:
                print(f"[Tushare] ❌ 获取失败: {e}")
        
        return quotes
    
    def get_financial_data(self, symbol, report_type='income'):
        """
        获取财务数据（优先akshare，失败时使用tushare）
        
        Args:
            symbol: 股票代码
            report_type: 报表类型（'income'利润表, 'balance'资产负债表, 'cashflow'现金流量表）
            
        Returns:
            DataFrame: 财务数据
        """
        # 优先使用akshare
        try:
            import akshare as ak
            print(f"[Akshare] 正在获取 {symbol} 的财务数据...")
            
            if report_type == 'income':
                df = ak.stock_financial_report_sina(stock=symbol, symbol="利润表")
            elif report_type == 'balance':
                df = ak.stock_financial_report_sina(stock=symbol, symbol="资产负债表")
            elif report_type == 'cashflow':
                df = ak.stock_financial_report_sina(stock=symbol, symbol="现金流量表")
            else:
                df = None
            
            if df is not None and not df.empty:
                print(f"[Akshare] ✅ 成功获取财务数据")
                return df
        except Exception as e:
            print(f"[Akshare] ❌ 获取失败: {e}")
        
        # akshare失败，尝试tushare
        if self.tushare_available:
            try:
                print(f"[Tushare] 正在获取 {symbol} 的财务数据（备用数据源）...")
                
                ts_code = self._convert_to_ts_code(symbol)
                
                if report_type == 'income':
                    df = self.tushare_api.income(ts_code=ts_code)
                elif report_type == 'balance':
                    df = self.tushare_api.balancesheet(ts_code=ts_code)
                elif report_type == 'cashflow':
                    df = self.tushare_api.cashflow(ts_code=ts_code)
                else:
                    df = None
                
                if df is not None and not df.empty:
                    print(f"[Tushare] ✅ 成功获取财务数据")
                    return df
            except Exception as e:
                print(f"[Tushare] ❌ 获取失败: {e}")
        
        return None
    
    def _convert_to_ts_code(self, symbol):
        """
        将6位股票代码转换为tushare格式（带市场后缀）
        
        Args:
            symbol: 6位股票代码
            
        Returns:
            str: tushare格式代码（如：000001.SZ）
        """
        if not symbol or len(symbol) != 6:
            return symbol
        
        # 根据代码判断市场
        if symbol.startswith('6'):
            # 上海主板
            return f"{symbol}.SH"
        elif symbol.startswith('0') or symbol.startswith('3'):
            # 深圳主板和创业板
            return f"{symbol}.SZ"
        elif symbol.startswith('8') or symbol.startswith('4'):
            # 北交所
            return f"{symbol}.BJ"
        else:
            # 默认深圳
            return f"{symbol}.SZ"
    
    def _convert_from_ts_code(self, ts_code):
        """
        将tushare格式代码转换为6位代码
        
        Args:
            ts_code: tushare格式代码（如：000001.SZ）
            
        Returns:
            str: 6位股票代码
        """
        if '.' in ts_code:
            return ts_code.split('.')[0]
        return ts_code


# 全局数据源管理器实例
data_source_manager = DataSourceManager()

