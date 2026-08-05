#!/usr/bin/env python3
# ⚠️ 已迁移：本文件为 2026-08-05 冻结镜像，现行版本在 ~/.claude/skills/stock-portfolio-analysis/scripts/，不要再改这里。
"""
触价后高频跟踪判稳器（plan_watch 的续段，2026-08-05 比亚迪#8 复盘产出）

背景：ADD 计划触发（价格从上方跌进买入带）后若继续下跌，触发价成交=买在带内高点；
TRIM 对称（尖峰冲过卖点后回落，等更高会卖飞）。plan_watch 2 分钟一轮只发一张触价卡，
覆盖不了触发后的快变阶段。本脚本由 plan_watch 触价时分离拉起，每 10 秒采样一次，
用确定性判据出"企稳可执行 / 仍在恶化暂缓 / 超时收尾"卡——是机械规则，不是 AI 判断。

判据（ADD 买向；TRIM 为镜像）：
  企稳: 现价较跟踪期最低点回升 ≥REBOUND_PCT 且最低点 ≥STALL_SEC 未刷新 → 绿卡，结束
  恶化: 较触发价再跌 ≥ESCALATE_PCT → 黄卡一次（额度冻结观察），继续跟踪
  超时: WINDOW_MIN 分钟或到场次收盘仍无企稳 → 收尾卡
每次跟踪至多 2 张卡（恶化 1 + 企稳/收尾 1）。EXIT 方向不跟踪（D1=B 收盘确认口径）。

用法: plan_track.py --plan-id 9 --code 002594 --name 比亚迪 --direction ADD \
                    --trigger-price 85.0 [--dry]
"""
import argparse
import os
import sys
import time
from datetime import datetime

os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.insert(0, '.')

from scripts.plan_watch import _fetch_quotes, _send  # noqa: E402  (--dry 经 sys.argv 透传生效)

INTERVAL_SEC = 10       # 采样间隔
WINDOW_MIN = 30         # 最长跟踪窗
REBOUND_PCT = 0.4       # 企稳：距极值回升/回落幅度（首周按实盘校准）
STALL_SEC = 180         # 企稳：极值未刷新的最短时长
ESCALATE_PCT = 1.0      # 恶化：较触发价继续不利变动幅度


class Tracker:
    """纯状态机，采样驱动，便于沙盒注入伪造序列测试。"""

    def __init__(self, direction: str, trigger: float):
        assert direction in ('ADD', 'TRIM')
        self.buy = direction == 'ADD'
        self.trigger = trigger
        self.extreme = None       # ADD 跟踪最低价 / TRIM 跟踪最高价
        self.extreme_ts = None
        self.escalated = False
        self.samples = 0

    def update(self, px: float, ts: float):
        """喂入一个采样。返回 None 或 ('stable'|'escalate', 描述文本)。"""
        self.samples += 1
        better = self.extreme is None or (px < self.extreme if self.buy else px > self.extreme)
        if better:
            self.extreme, self.extreme_ts = px, ts    # 刷新极值后不 return：恶化判据仍需检查
        drift = (px / self.extreme - 1) * 100          # ADD 为回升(+)，TRIM 为回落(-)
        stalled = ts - self.extreme_ts >= STALL_SEC
        if not better and stalled and (drift >= REBOUND_PCT if self.buy else drift <= -REBOUND_PCT):
            word = '止跌企稳' if self.buy else '冲高衰竭'
            return ('stable', f"{word}：极值 {self.extreme:.2f} 已 {int((ts - self.extreme_ts) / 60)} 分钟未刷新，"
                              f"现价 {px:.2f}（距极值 {drift:+.2f}%），样本 {self.samples} 个")
        adverse = (px / self.trigger - 1) * 100        # ADD 越负越糟，TRIM 越正越"还在涨"
        if not self.escalated and (adverse <= -ESCALATE_PCT if self.buy else adverse >= ESCALATE_PCT):
            self.escalated = True
            word = '仍在下行' if self.buy else '仍在上行'
            return ('escalate', f"{word}：较触发价 {self.trigger:g} 已再{'跌' if self.buy else '涨'} "
                                f"{abs(adverse):.1f}%（现价 {px:.2f}）")
        return None


def _session_deadline(now: datetime) -> datetime:
    end = now.replace(hour=11, minute=30, second=0) if now.hour < 12 else \
        now.replace(hour=15, minute=0, second=0)
    return min(end, now.replace(microsecond=0) + __import__('datetime').timedelta(minutes=WINDOW_MIN))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--plan-id', required=True)
    ap.add_argument('--code', required=True)
    ap.add_argument('--name', required=True)
    ap.add_argument('--direction', required=True, choices=['ADD', 'TRIM'])
    ap.add_argument('--trigger-price', type=float, required=True)
    ap.add_argument('--dry', action='store_true')
    a = ap.parse_args()

    tr = Tracker(a.direction, a.trigger_price)
    deadline = _session_deadline(datetime.now())
    act = '买' if a.direction == 'ADD' else '卖'
    print(f"[{datetime.now():%H:%M:%S}] track start plan#{a.plan_id} {a.name} "
          f"{a.direction}@{a.trigger_price:g} 截止 {deadline:%H:%M}")
    last_px = None
    while datetime.now() < deadline:
        try:
            q = _fetch_quotes([a.code]).get(a.code)
        except Exception as e:
            print(f"[{datetime.now():%H:%M:%S}] WARN 采样失败 {e!r}")
            q = None
        if q:
            last_px = q[0]
            ev = tr.update(q[0], time.time())
            if ev:
                kind, msg = ev
                if kind == 'stable':
                    _send(f"**计划#{a.plan_id}** {a.direction} {act}点触发后跟踪 {tr.samples} 个采样（每{INTERVAL_SEC}s）\n"
                          f"{msg}\n---\n**你需要做什么**：判稳判据命中，可考虑按计划执行；判据是机械规则，最终由你决定\n"
                          f"{datetime.now():%Y-%m-%d %H:%M:%S}",
                          title=f"✅ 判稳 · {a.name} {msg[:4]}", level='info')
                    print(f"[{datetime.now():%H:%M:%S}] stable → 结束"); return
                _send(f"**计划#{a.plan_id}** 触发后走势恶化\n{msg}\n---\n"
                      f"**你需要做什么**：暂缓执行，剩余额度冻结观察；企稳信号出现会再推\n"
                      f"{datetime.now():%Y-%m-%d %H:%M:%S}",
                      title=f"🟡 暂缓 · {a.name} {msg[:4]}", level='warn')
        time.sleep(INTERVAL_SEC)
    lo_hi = f"极值 {tr.extreme:.2f}" if tr.extreme else "无有效采样"
    _send(f"**计划#{a.plan_id}** 跟踪窗结束（{WINDOW_MIN}分钟/场次收盘），未出现企稳判据\n"
          f"{lo_hi}｜最新 {last_px if last_px else '?'}｜样本 {tr.samples} 个\n---\n"
          f"**你需要做什么**：走势仍未判稳，按原计划纪律自行决断或等下一轮触发\n"
          f"{datetime.now():%Y-%m-%d %H:%M:%S}",
          title=f"⏱ 跟踪收尾 · {a.name}", level='info')
    print(f"[{datetime.now():%H:%M:%S}] timeout → 结束")


if __name__ == '__main__':
    main()
