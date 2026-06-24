# 个股深研结构

`stock_research_runs` 是个股页面的历史记录来源。

新入库研究结果必须先通过 `core/schema/stock_report.py` 中的 `StockResearchReport` 校验。旧数据可以继续保留在库中，但新写入路径不再接受 raw dict。

## 强 Schema

统一研究 JSON 的顶层字段：

- `schema_version`：固定为 `stock_research_report.v1`。
- `report_version`：确定性报告组装器版本，新报告由 `core/report` 填充。
- `report_hash`：确定性回放 hash，新报告由 `core/report` 填充。
- `run_id`：本次研究运行 ID；缺省时由 schema 根据 `stock_code + task_type + research_date + schema_version` 生成。
- `stock_code`：股票代码，例如 `600519.SH`。
- `stock_name`：股票名称。
- `source_report_id`：上游 `/api/index` 的 `report_id`。
- `task_type`：`strategic` 或 `financial`。
- `research_date`：`YYYY-MM-DD`。
- `status`：`complete`、`draft` 或 `blocked`。
- `title`、`summary`：研究标题和摘要。
- `industry_position`、`competition_landscape`、`upstream_downstream`、`annual_growth`、`multi_bagger_potential`、`heavy_position_view`。
- `fundamentals`：财务质量子模型。
- `valuation`：估值子模型。
- `peer_comparison`：竞争和相对估值子模型。
- `risk`：风险和证伪条件子模型。
- `conclusion`：最终研究标签、置信度和结论摘要。
- `evidence`：证据对象数组。
- `assumptions`：假设字符串数组。

强约束：

- schema 禁止额外字段。
- `report_hash` 如果提供，必须是 64 位小写 sha256 hex。
- `heavy_position_view` 必须等于 `conclusion.grade`。
- 如果显式提供 `run_id`，必须等于系统计算值，否则拒绝入库。
- `strategic` 不允许写入估值区间。
- `financial` 必须写入完整估值区间，且 `low <= mid <= high`。
- DB 写入函数 `insert_research_run` 只接受通过校验的 `StockResearchReport`，不接受 raw dict。

## 任务状态机

`task_queue` 是系统级任务控制表，也是唯一状态源。展示用 `research_queue` 只作为 prompt/projection/UI 表，通过 `run_id` 关联 `task_queue`，不保存业务状态。

字段：

- `task_id`：由 `run_id` 派生的任务 ID。
- `run_id`：全局唯一，数据库强制 `UNIQUE(run_id)`。
- `stock_code`、`task_type`：任务对象。
- `status`：`PENDING`、`RUNNING`、`DONE`、`FAILED`、`BLOCKED`、`RETRY`。
- `retry_count`：失败重试次数。
- `created_at`、`updated_at`。
- `error_message`：失败或恢复原因。

合法状态转换：

```text
PENDING -> RUNNING
PENDING -> BLOCKED
RUNNING -> DONE
RUNNING -> FAILED
RUNNING -> BLOCKED
FAILED -> RETRY
RETRY -> PENDING
BLOCKED -> PENDING
BLOCKED -> FAILED
```

禁止直接 `PENDING -> DONE`。`RUNNING` 超过 30 分钟会被恢复为 `FAILED`，重新入队时按 `FAILED -> RETRY -> PENDING` 增加 `retry_count` 后重新领取。

`research_queue` 不包含 `status` 字段。`/api/queue` 中看到的 `status` 是由 `task_queue.status` 映射生成：

- `PENDING` / `RETRY` -> `pending`
- `RUNNING` -> `in_progress`
- `DONE` -> `complete`
- `FAILED` / `BLOCKED` -> `blocked`

`research_queue` 可以包含展示来源字段，但这些字段不参与状态机：

- `source_type='trackable_leader'`：来自上游 `/api/index` 的 `A可跟踪龙头`。
- `source_type='manual_request'`：来自 `/research?stock={code}` 的主动研究请求。
- `/api/queue` 会额外输出 `source_label`：`可跟踪龙头` 或 `其他请求`。

## 审计日志

`audit_log` 是旁路审计表，不参与任务领取、状态转换或报告计算。

字段：

- `run_id`：对应 `StockResearchReport.run_id`。
- `stage`：`feature`、`valuation`、`signal`、`report`。
- `input_hash`：stage 输入快照 hash。
- `output_hash`：stage 输出快照 hash。
- `diff_metrics`：用于审计和漂移检测的结构化指标 JSON。
- `timestamp`：trace 记录时间。

`core/observability.verify_run(conn, run_id)` 校验每个 stage 是否存在且 hash 没有分叉；`detect_basic_drift(conn, run_id)` 检查 PE/PB 极端值和 valuation signal 分布漂移。

## 核心字段

- `code`：由 `stock_code` 映射到数据库。
- `task_type`：`strategic` 或 `financial`。
- `research_date`：研究日期。
- `status`：`complete`、`draft` 或 `blocked`。
- `valuation_low`：由 `valuation.intrinsic_value_low` 映射。
- `valuation_mid`：由 `valuation.intrinsic_value_mid` 映射。
- `valuation_high`：由 `valuation.intrinsic_value_high` 映射。
- `valuation_method`：由 `valuation.method` 映射。
- `valuation_confidence`：由 `valuation.confidence` 映射。
- `valuation.engine_version`、`valuation.undervalued_score`、`valuation.growth_score`、`valuation.quality_score`、`valuation.risk_adjusted_score`：由 `core/valuation` 的确定性估值引擎生成。
- `industry_position`：行业地位。
- `competition_landscape`：竞争格局。
- `upstream_downstream`：上下游公司和议价关系。
- `annual_growth`：年增长率与质量。
- `multi_bagger_potential`：五倍/十倍潜力。
- `heavy_position_view`：重仓研究资格标签。
- `risks_json`：由 `risk.invalidation_conditions` 映射。
- `raw_json`：保存 `StockResearchReport.model_dump(mode="json")`，不保存未校验原始 dict。

## 确定性估值输出

财务估值深研中的估值数值不应由 LLM 临场生成。数值层由 `core/valuation` 输出：

- `features.py`：财务特征。
- `models.py`：PE、PB、轻量 DCF 和估值区间组合。
- `peer.py`：行业中位数和分位排名。
- `signal.py`：估值、增长、质量和风险调整分数。

LLM 可以解释这些 deterministic output，但不能替代公式计算。

## 确定性报告组装

财务报告最终结构由 `core/report.build_stock_report(input_data)` 生成：

- 输入：`financial_rows`、`valuation_inputs`、`peers`、`risk_signals` 和基础股票元数据。
- 输出：通过 Pydantic 校验的 `StockResearchReport`。
- `core/report/conclusion.py` 用固定规则从 valuation signal 生成 `heavy_position_view` 和 `conclusion`。
- `report_hash` 基于 `stock_code`、feature inputs、valuation outputs、peer outputs 计算，same input 必须得到 same hash。

命令行入口：

```powershell
python scripts/build_research_report.py path\to\assembly_input.json
```

财务深研可以用 LLM 搜集、解释证据，但最终 JSON 的估值、同业、风险调整分数和结论等级必须由 assembler 生成。

开启审计 trace 时，使用：

```powershell
python scripts/build_research_report.py --audit-db data\local\myinveststock.sqlite path\to\assembly_input.json
```

## 研究频率

- `strategic`：长期底稿，默认只做一次。
- `financial`：财务和估值刷新，可多次入库并叠加估值区间图示。

## 入库方式

```powershell
python scripts/import_research_run.py research/stocks/600519.SH/financial_2026-06-23.json
```

导入脚本会执行：

```python
report = StockResearchReport(**raw_output)
report.model_dump(mode="json")
```

校验失败会抛出异常，并输出 `run_id` 与 `stock_code` 便于定位。

## 重仓资格标签

只允许使用研究标签，不输出买卖指令：

- `不具备`
- `观察`
- `可跟踪`
- `核心仓研究资格`
- `高估暂缓`
