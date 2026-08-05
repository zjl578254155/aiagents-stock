# aiagents-stock

A 股持仓分析与盯盘系统。程序侧只做**数据采集 + 调度刷缓存**，不内嵌 LLM 自动判断；四档决策（HOLD / TRIM / WATCH / EXIT）由 Claude 在会话中结合数据 + 联网归因 + v5 框架做出，写回 `eod_analysis_records` 表，Streamlit UI 展示。

## 做持仓分析前必读

@docs/持仓分析工作流SOP.md

上面这份 SOP 定义了标准五步闭环、**强制红线证伪检索**（2a）、**影子股/题材归属自检**（2b'）和诚实边界。里面每条"教训"都对应一次真实的漏报事故（五粮液董事长被查、金发×宇树影子股），**不是建议，是硬性步骤，不得跳过**。

用户说"分析持仓""按设计方案分析 XX""复盘"时，直接走这套闭环，或用 `/eod` 命令。

## 硬性纪律

1. **数据必须现取，并标注获取时间戳。** 禁止用记忆里或上一轮会话的旧行情——历史上被明确纠正过（"你的数据是不是不是最新的啊，今天已经涨了"）。开盘/盘中/收盘的数据完全不同，用户说"今日已开盘/已收盘"时必须重新拉。
2. **择时层已退役。** 只提供估值判断与完整参照系，择时决策权归用户。不要直接下"现在买/现在卖"的择时指令。
3. **决策必须带完整参照系**，缺一不可：当前价 / 昨收 / 持仓成本 / 破位起点 / 前高 / PE 与 ROE 的匹配度。
4. **已定策略执行到底。** 用户定了"分批买入"就按分批给方案，不要临时改成一次性建仓（原话："之前不是说分批买入吗，你就给我按照分批买入"）。
5. **拿不到的数据就说拿不到**，不要用估算值冒充实盘数据。

## 关键路径

| 用途 | 位置 |
|---|---|
| 分析工作流 SOP | `docs/持仓分析工作流SOP.md` |
| 复盘日志（追加，不覆盖） | `docs/持仓分析复盘日志.md` |
| 统一分析规范 | `docs/UNIFIED_ANALYSIS_SPEC.md` |
| 持仓台账表 | `eod_watch_stocks`（成交后需更新） |
| 结论落库表 | `eod_analysis_records` |
| EOD 采集服务 | `services/eod_analysis_service.py` |
| K线数据源（唯一来源） | 本地 docker 容器 `tdx-api`（http://127.0.0.1:8080），**按需启停**：用前 `docker start tdx-api`（加载代码表约 60 秒），**用完 `docker stop tdx-api`，不留空跑**；15:35 采集 cron 已内置启停。unless-stopped 只兜使用中崩溃，尊重手动 stop |
| 决策写库脚本（唯一入口） | `scripts/save_decisions.py`（JSON→幂等落库→回读校验，禁止会话内联写库）；**计划/流水/台账写入口已迁移** `~/.claude/skills/stock-portfolio-analysis/scripts/portfolio_ledger.py` |
| 盯盘时段配置 | `monitor_schedule_config.json` |
| 在场计划盘中盯价 | **已迁移** `~/.claude/skills/stock-portfolio-analysis/scripts/plan_watch.py`（cron 已指 skill；repo 侧 scripts/plan_watch.py 为冻结镜像） |
| 触价后高频跟踪判稳 | **已迁移** `~/.claude/skills/stock-portfolio-analysis/scripts/plan_track.py`（同上；日志在 skill data/） |

联网检索锁定国内财经源（`eastmoney.com` / `cninfo.com.cn` / `stcn.com` / `cs.com.cn` / `cnstock.com` / `sina.com.cn` / `10jqka.com.cn` / `jrj.com.cn`）；本机代理对国内站是**直连通、走代理反而失败**。
