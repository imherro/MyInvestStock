# 单股深研结构

`stock_research_runs` 是个股页面的历史记录来源。

## 核心字段

- `code`：股票代码。
- `task_type`：`strategic`、`financial` 或 `combined`。
- `research_date`：研究日期。
- `status`：`complete`、`draft` 或 `blocked`。
- `valuation_low`：保守估值。
- `valuation_mid`：合理估值。
- `valuation_high`：乐观估值。
- `valuation_method`：估值方法。
- `valuation_confidence`：估值置信度。
- `industry_position`：行业地位。
- `competition_landscape`：竞争格局。
- `upstream_downstream`：上下游公司和议价关系。
- `annual_growth`：年增长率与质量。
- `multi_bagger_potential`：五倍/十倍潜力。
- `heavy_position_view`：重仓研究资格标签。
- `risks_json`：风险和证伪条件。

## 研究频率

- `strategic`：长期底稿，默认只做一次。
- `financial`：财务和估值刷新，可多次入库并叠加估值区间图示。

## 入库方式

```powershell
python scripts/import_research_run.py research/stocks/600519.SH/financial_2026-06-23.json
```

战略深研 JSON 不允许写入 `valuation_low`、`valuation_mid`、`valuation_high`。

## 重仓资格标签

只允许使用研究标签，不输出买卖指令：

- `不具备`
- `观察`
- `可跟踪`
- `核心仓研究资格`
- `高估暂缓`
