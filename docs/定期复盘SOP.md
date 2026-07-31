# 定期复盘 SOP（周 / 月 / 季）

> 2026-07-31 建。目的：现有进化是事故驱动（漏报→SOP 打补丁），只能从"痛过的错"学习；定期复盘补上**主动对账**，从"没被注意到的错"里学习。触发：cron 飞书提醒（周五 16:30 / 每月 1 日 / 季度首月 1 日）+ 用户对 Claude 说"周复盘 / 月复盘 / 季度复盘"。所有产出进 repo 并 git commit，进化过程可审计。

## 一、周复盘（每周五收盘后）

### 1a. 决策对账（评级 vs 实际走势）

```sql
-- 上周（近 7 天）全部决策
SELECT id, code, name, trade_date, action_code, action, confidence, close_price
FROM eod_analysis_records
WHERE trade_date >= date('now', '-7 days')
ORDER BY code, trade_date;
```

对每条记录，用腾讯行情接口（直连，`proxies={'http': None, 'https': None}`）取当前价，算"决策日→今"涨跌幅，逐档评估：
- **HOLD**：后续走势是否击穿失效条件（invalidation_condition 列已随记录落库）
- **WATCH**：定义的升降级信号是否出现、出现后有没有跟进
- **TRIM/EXIT**：给出后是否执行、不执行的差价
产出一张命中率小表（档位 × 条数 × 方向正确数）。

### 1b. 执行对账（治"触发点漏执行"问题族 G）—— SQL 化（2026-07-31 起）

```sql
SELECT id, code, direction, trigger_price, qty, condition_text
FROM eod_action_plans WHERE status='open';
```

对每条 open 计划，用本周高/低价（kline 缓存）判断触发条件是否到达：
- 到达且用户已操作 → `portfolio_ledger.py plan close --status hit_executed`（若成交已录，trade add --plan-id 会自动关）
- 到达但未操作 → `plan close --status hit_missed --note "差价约X元"`——**漏执行差价必须量化**
- 条件已失效/剧本已改 → `expired/cancelled` 注明原因
新产出的条件动作当场 `plan add`。快捷对照：`portfolio_ledger.py report`（open 计划 × 实时价 × 距离）。

### 1c. 管线健康三指标

```sql
-- 本周落库覆盖天数（对照本周实际交易日数）
SELECT COUNT(DISTINCT trade_date) FROM eod_analysis_records WHERE trade_date >= date('now','-7 days');
-- 新闻信号分布（'警告' 应显著少于主体校验前的 45% 基线）
SELECT news_risk_level, COUNT(*) FROM eod_analysis_records
WHERE trade_date >= date('now','-7 days') GROUP BY news_risk_level;
```

外加：`ls data/eod_snapshots/` 本周快照天数；最新快照 `data_stale` 比例；`logs/eod_cron.log` 有无 FAIL。

### 产出

复盘日志追加 `## YYYY-MM-DD 周复盘` 节（命中率表 + 执行对账 + 管线健康 + 本周新教训）；发现新事故 → 当场给 SOP/纪律打补丁并 git commit；未完事项写入 Obsidian `待办/投资复盘待办.md`，按性质落位（格式见 `待办/README.md`）：**现在能拍板的 → 决策表 D 行**（选项显式枚举、留空待填）；**未来时点/条件触发的（观察触发点） → 提醒表 R 行 + 必须调 notify.py 排真实 macOS 提醒**（条件类定在关联日 09:15）；简单勾选类才用 checkbox。

## 二、月复盘（每月初）

1. **问题族追踪表逐项更新**：`docs/问题族追踪表.md` 的每个问题族标注本月状态（复发 N 次 / 无复发 / 已关闭 / 新增族），复发的要写清哪天、什么形态。
2. **SOP 补丁有效性**：检索本月复盘日志，统计 2d 必答模板拦截次数、"主体未核实"降级次数（= 主体校验消灭的误报数）、data_stale 触发校正次数——补丁没起作用的要么改要么撤。
3. **计划命中率/执行率**（2026-07-31 起）：
```sql
SELECT status, COUNT(*) FROM eod_action_plans
WHERE close_date >= date('now','start of month','-1 month') OR status='open' GROUP BY status;
```
执行率 = hit_executed / (hit_executed + hit_missed)；hit_missed 的差价合计就是问题族 G 的月度成本。
4. **四档分布漂移**：
```sql
SELECT action_code, COUNT(*) FROM eod_analysis_records
WHERE trade_date >= date('now','start of month','-1 month') AND trade_date < date('now','start of month')
GROUP BY action_code;
```
对照基线（2026-06/07：HOLD+WATCH 占 97.8%，TRIM 3 条 EXIT 1 条）——分布长期不动说明系统只敢观察不敢发信号，本身就是待修问题。

### 产出

更新 `docs/问题族追踪表.md` + 复盘日志"月复盘"节 + git commit。

## 三、季复盘（1/4/7/10 月初）

1. **v5 框架校准**：对照 `docs/持仓逻辑评估模块设计方案.md` 与本季实操的分裂点（已知三处：框架禁技术分析而实操用 MA/量价、框架不给仓位而实操给具体股数、框架限收盘后而实操大量盘中闭环）——逐条做收敛决策：改框架文档承认实操，或改实操回归框架。不允许继续"文档一套、实际一套"。
2. **组合 vs 基准**：组合季度收益 vs 沪深300 / 创业板指（行情接口取季度起止点），跑输要归因到结构（题材股占比）还是执行（触发点漏执行差价合计）。
3. **台账对账**：`eod_watch_stocks` vs 券商实际持仓逐只核对成本/数量；检查废弃库（portfolio_stocks.db 等）是否仍有混淆风险。

### 产出

修订 v5 设计方案文档（版本号 +1，变更记录写明）+ 复盘日志"季复盘"节 + git commit。
