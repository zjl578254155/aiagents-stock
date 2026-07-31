#!/bin/bash
# 定期复盘提醒（周/月/季）—— docs/定期复盘SOP.md 的 cron 触发端
# 主通道：项目飞书 webhook（.env WEBHOOK_ENABLED）；失败兜底：追加 Obsidian 待办/投资复盘待办.md
# 用法: review_reminder.sh weekly|monthly|quarterly
cd "$(dirname "$0")/.." || exit 1
TYPE="$1"
case "$TYPE" in
  weekly)    MSG="📅 周复盘提醒：决策对账+执行对账+管线健康。对 Claude 说「周复盘」即可，流程见 docs/定期复盘SOP.md" ;;
  monthly)   MSG="📅 月复盘提醒：问题族追踪表更新+SOP补丁有效性+四档分布漂移。对 Claude 说「月复盘」" ;;
  quarterly) MSG="📅 季复盘提醒：v5框架校准+组合vs基准+台账对账。对 Claude 说「季度复盘」" ;;
  *) echo "usage: $0 weekly|monthly|quarterly"; exit 1 ;;
esac

if .venv/bin/python - "$MSG" <<'PY' >/dev/null 2>&1
import sys
from services.eod_analysis_service import _send_webhook
sys.exit(0 if _send_webhook(sys.argv[1]) else 1)
PY
then
  echo "[$(date '+%F %T')] ${TYPE} 提醒已发飞书"
else
  TODO="/Users/lemon/Documents/ObsidianVault/待办/投资复盘待办.md"
  echo "- [ ] $(date +%F) ${TYPE} 复盘待跑（飞书提醒发送失败，cron 兜底写入）" >> "$TODO"
  echo "[$(date '+%F %T')] ${TYPE} 飞书发送失败，已兜底写入待办"
fi
