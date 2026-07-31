#!/usr/bin/env python3
"""
EOD 快照采集兜底任务（独立于 Streamlit 进程）

背景：6 个调度器都跑在 Streamlit 的 daemon 线程里，Streamlit 一退定时任务静默消亡，
2026-06~07 采集覆盖率仅约 50%。本脚本由 cron 每交易日 15:35 独立执行，
成功标准 = 当日快照文件生成且非空；失败推飞书 webhook 告警（需 .env WEBHOOK_ENABLED=true）。

cron 示例:
    35 15 * * 1-5 cd <项目根> && .venv/bin/python scripts/eod_export_cron.py >> logs/eod_cron.log 2>&1

局限：仅按周一~周五判断，法定节假日不跳过（当日会生成一份与前一交易日相同的快照，无害）。
"""
import json
import os
import sys
from datetime import datetime

os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.insert(0, '.')


def _ensure_tdx() -> bool:
    """TDX 预检自愈：探测不通则拉起本地 docker 容器 tdx-api（服务加载代码表约需 1 分钟），仍不通返回 False"""
    import subprocess
    import time
    import requests
    from services.eod_analysis_service import TDX_BASE_URL

    def probe() -> bool:
        r = requests.get(f"{TDX_BASE_URL}/api/kline", params={'code': '600143', 'type': 'day'},
                         timeout=5, proxies={'http': None, 'https': None})
        return r.json().get('code') == 0

    try:
        if probe():
            return True
    except Exception:
        pass
    subprocess.run(['docker', 'start', 'tdx-api'], capture_output=True)
    for _ in range(14):
        time.sleep(5)
        try:
            if probe():
                return True
        except Exception:
            continue
    return False


def main():
    stamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if datetime.now().weekday() >= 5:
        print(f"[{stamp}] 周末，跳过")
        return
    from services.eod_analysis_service import export_snapshots, _send_webhook
    if not _ensure_tdx():
        _send_webhook(f"🔴 TDX 服务(docker tdx-api)自愈拉起失败 {stamp}，本次采集将使用陈旧K线缓存(标stale)")
        print(f"[{stamp}] WARN TDX 自愈失败，继续采集（K线将为陈旧缓存）")
    today = datetime.now().strftime('%Y%m%d')
    try:
        path = export_snapshots()
        size = os.path.getsize(path)
        with open(path, encoding='utf-8') as f:
            snaps = json.load(f)
        stale = [s['code'] for s in snaps if s.get('data_stale')]
        if today not in os.path.basename(path) or size < 2000 or not snaps:
            raise RuntimeError(f"快照产出异常: path={path} size={size} 只数={len(snaps)}")
        msg = f"[{stamp}] OK {path} {len(snaps)}只 {size}B"
        if stale:
            msg += f"  ⚠️ K线滞后(未含今日bar): {','.join(stale)}"
            _send_webhook(f"⚠️ EOD 快照已生成但 K 线滞后 {stamp}\n滞后: {','.join(stale)}\n"
                          f"TDX 日线可能未落地，分析前需行情接口校正收盘价")
        print(msg)
    except Exception as e:
        print(f"[{stamp}] FAIL {e!r}")
        _send_webhook(f"🔴 EOD 快照兜底采集失败 {stamp}\n{e!r}\n请检查 TDX 服务(192.168.1.222:8181)与网络")
        sys.exit(1)


if __name__ == '__main__':
    main()
