# 个股深研结构

`stock_research_runs` 是个股页面的历史记录来源。

新入库研究结果必须先通过 `core/schema/stock_report.py` 中的 `StockResearchReport` 校验。旧数据可以继续保留在库中，但新写入路径不再接受 raw dict。

## 强 Schema

统一研究 JSON 的顶层字段：

- `schema_version`：固定为 `stock_research_report.v1`。
- `run_id`：本次研究运行 ID；缺省时由 schema 根据 report、股票、任务类型、日期和标题生成。
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
- `heavy_position_view` 必须等于 `conclusion.grade`。
- `strategic` 不允许写入估值区间。
- `financial` 必须写入完整估值区间，且 `low <= mid <= high`。
- DB 写入函数 `insert_research_run` 只接受通过校验的 `StockResearchReport`，不接受 raw dict。

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
- `industry_position`：行业地位。
- `competition_landscape`：竞争格局。
- `upstream_downstream`：上下游公司和议价关系。
- `annual_growth`：年增长率与质量。
- `multi_bagger_potential`：五倍/十倍潜力。
- `heavy_position_view`：重仓研究资格标签。
- `risks_json`：由 `risk.invalidation_conditions` 映射。
- `raw_json`：保存 `StockResearchReport.model_dump(mode="json")`，不保存未校验原始 dict。

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
