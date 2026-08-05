"""
Token用量追踪服务
可插拔式设计：通过类级回调钩子接入AI客户端，不改变现有调用链
"""

import inspect
import logging
from datetime import datetime
from typing import Dict, Tuple

from db.token_usage_db import token_usage_db

logger = logging.getLogger(__name__)


class TokenUsageService:
    """Token用量追踪服务（单例）"""

    # 模型定价（CNY / 百万Token）
    MODEL_PRICING = {
        # DeepSeek 官方
        'deepseek-chat': {'input': 1.0, 'output': 2.0},
        'deepseek-reasoner': {'input': 4.0, 'output': 16.0},
        # 阿里百炼 Qwen
        'qwen-plus': {'input': 0.8, 'output': 2.0},
        'qwen-plus-latest': {'input': 0.8, 'output': 2.0},
        'qwen-flash': {'input': 0.0, 'output': 0.0},
        'qwen-turbo': {'input': 0.3, 'output': 0.6},
        'qwen3-max': {'input': 2.0, 'output': 6.0},
        'qwen-max': {'input': 2.0, 'output': 6.0},
        'qwen-long': {'input': 0.5, 'output': 2.0},
        # 硅基流动（免费模型）
        'deepseek-ai/DeepSeek-R1-0528-Qwen3-8B': {'input': 0.0, 'output': 0.0},
        'Qwen/Qwen2.5-7B-Instruct': {'input': 0.0, 'output': 0.0},
        # 硅基流动（付费模型）
        'Pro/deepseek-ai/DeepSeek-V3.1-Terminus': {'input': 2.0, 'output': 8.0},
        'deepseek-ai/DeepSeek-R1': {'input': 4.0, 'output': 16.0},
        'Qwen/Qwen3-235B-A22B-Thinking-2507': {'input': 4.0, 'output': 12.0},
        # 兜底
        '_default': {'input': 2.0, 'output': 6.0},
    }

    # 调用栈 → feature 映射
    FEATURE_MAP = {
        'smart_monitor': 'smart_monitor',
        'longhubang': 'longhubang',
        'macro_cycle': 'macro_cycle',
        'news_flow': 'news_flow',
        'sector_strategy': 'sector_strategy',
        'main_force': 'main_force',
        'ai_agents': 'stock_analysis',
        'deepseek_client': 'stock_analysis',
    }

    # feature → 中文标签
    FEATURE_LABELS = {
        'stock_analysis': '股票分析',
        'longhubang': '龙虎榜',
        'news_flow': '新闻流量',
        'sector_strategy': '板块策略',
        'macro_cycle': '宏观周期',
        'main_force': '主力选股',
        'smart_monitor': 'AI盯盘',
        'unknown': '其他',
    }

    def __init__(self):
        self._db = token_usage_db

    def on_usage(self, usage, model: str, source: str = 'openai_sdk'):
        """
        API调用完成后的回调函数

        Args:
            usage: OpenAI SDK的Usage对象 或 dict（requests模式）
            model: 模型名称
            source: 'openai_sdk' 或 'requests_raw'
        """
        try:
            if source == 'openai_sdk':
                prompt_tokens = getattr(usage, 'prompt_tokens', 0) or 0
                completion_tokens = getattr(usage, 'completion_tokens', 0) or 0
                total_tokens = getattr(usage, 'total_tokens', 0) or 0
            else:
                prompt_tokens = usage.get('prompt_tokens', 0) or 0
                completion_tokens = usage.get('completion_tokens', 0) or 0
                total_tokens = usage.get('total_tokens', 0) or 0

            feature, caller = self._detect_feature_and_caller()
            cost = self._calculate_cost(model, prompt_tokens, completion_tokens)

            self._db.record_usage(
                timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                model=model,
                feature=feature,
                caller=caller,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                estimated_cost=cost,
                success=True,
            )
        except Exception as e:
            logger.warning(f"Token用量追踪失败: {e}")

    def _detect_feature_and_caller(self) -> Tuple[str, str]:
        """通过调用栈自动识别功能模块和调用方法"""
        try:
            stack = inspect.stack()
            for frame_info in stack[2:]:  # 跳过 on_usage 和 call_api
                filename = frame_info.filename
                func_name = frame_info.function
                for key, feat in self.FEATURE_MAP.items():
                    if key in filename:
                        return feat, func_name
        except Exception:
            pass
        return 'unknown', 'unknown'

    def _calculate_cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        """计算预估费用（CNY）"""
        pricing = self.MODEL_PRICING.get(model, self.MODEL_PRICING['_default'])
        cost = (prompt_tokens / 1_000_000 * pricing['input'] +
                completion_tokens / 1_000_000 * pricing['output'])
        return round(cost, 6)

    # ===== 查询接口（代理到DB层）=====

    def get_daily_summary(self, days: int = 30):
        return self._db.get_daily_summary(days)

    def get_model_breakdown(self, days: int = 30):
        return self._db.get_model_breakdown(days)

    def get_feature_breakdown(self, days: int = 30):
        return self._db.get_feature_breakdown(days)

    def get_recent_records(self, limit: int = 100):
        return self._db.get_recent_records(limit)

    def get_total_stats(self):
        return self._db.get_total_stats()

    def get_hourly_distribution(self, days: int = 7):
        return self._db.get_hourly_distribution(days)

    def get_feature_label(self, feature: str) -> str:
        """获取功能的中文标签"""
        return self.FEATURE_LABELS.get(feature, feature)

    def cleanup(self, days_to_keep: int = 90) -> int:
        return self._db.cleanup_old_records(days_to_keep)


# 全局实例
token_usage_service = TokenUsageService()
