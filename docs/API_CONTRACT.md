# API Contract

本地服务默认运行在：

```text
http://127.0.0.1:8016
```

## `/api/index`

用途：输出主要结果信息，供其他系统集成。

稳定路径：

```text
key_results.primary_output.items
```

每个 item 表示一只当前 `A可跟踪龙头`，包含代码、名称、主题、深研评分、候选龙头证据、行情估值快照、评分和页面链接。
`upstream_signal` 是 MyInvestTheme 主线环境与 MyInvestLeader 个股龙头信号的组合快照，本项目只做字段解释，不重复研究主线。

关键字段：

- `schema_version`: `myinveststock.index.v1`
- `source.leader_endpoint`: 个股候选入口固定为 `https://leader.okbbc.com/api/index`
- `source.leader_result_path`: 股票池固定路径 `key_results.primary_output.items`
- `source.theme_endpoint`: 主线环境入口固定为 `https://theme.okbbc.com/api/index`
- `source.theme_context_paths`: 主线环境引用路径 `mainline_ranking / legacy_theme_ranking / market`
- `source.source_policy`: Leader 决定股票池和龙头证据，Theme 提供主线环境，本项目不重新研究主线
- `key_results.primary_output.items`: 当前主结果股票列表
- `key_results.primary_output.items[].theme_context`: 与该股票主题匹配的 Theme 主线环境快照，可能为空
- `key_results.primary_output.items[].upstream_signal`: 上游信号快照，包含主题、主线强度、生命周期、周期阶段、ETF/板块趋势、拥挤度、风险偏好、龙头深研、证据质量、交易结构和风险提示
- `key_results.primary_output.items[].links.research_gateway`: 主动研究入口 `/research?stock={code}`
- `links.latest`: 研究成果接口 `/api/latest`

## `/api/latest`

用途：输出当前研究成果。

关键字段：

- `schema_version`: `myinveststock.research.v1`
- `summary.stock_count`: 当前主结果股票数量
- `summary.research_run_count`: 已入库研究记录数量
- `stocks[].leader`: 该股票的主结果信息
- `stocks[].research.latest`: 最新完整个股深研，`task_type=stock_research`
- `stocks[].research.history`: 历史完整个股深研记录
- `stocks[].research.valuation_history`: 历次估值区间
- `stocks[].decision_matrix`: `MyInvestTheme mainline environment + MyInvestLeader stock signal + MyInvestStock financial safety margin` 的矩阵结论

## `/research?stock={code}`

用途：从外部系统跳转到个股研究页面。

行为：

- 如果本地已有该股票页面、研究记录或队列任务，返回 `303` 跳转到 `/stocks/{code}`。
- 如果本地没有该股票，创建当天主动请求队列批次，来源标记为 `manual_request` / `其他请求`，再返回 `303` 跳转到 `/stocks/{code}?queued=1`。
- `name` 查询参数可选；未提供名称时，系统按“本地历史记录 -> Tushare `stock_basic` -> 股票代码兜底”的顺序补全队列名称。
- 不要求该股票出现在 `/api/index` 的 `key_results.primary_output.items`。
- 不直接执行深研，不绕过队列领取和单股单任务规则。

## `/api/queue`

用途：输出本地研究队列。

新增来源字段：

- `source_type`: `trackable_leader` 或 `manual_request`
- `source_label`: `可跟踪龙头` 或 `其他请求`
- `trigger_reason`: 本次入队原因，例如 `新进入可跟踪龙头`、`手工请求研究`、`估值中枢变化`

## `/api/stocks/{code}`

用途：输出单只股票页面数据。

关键字段：

- `leader`: 当前或历史最近一次可跟踪龙头记录，没有则为 `null`
- `leader_summary`: 与 `/api/index` item 同形态的摘要，没有则为 `null`
- `upstream_signal`: MyInvestTheme 主线环境与 MyInvestLeader 个股信号组合快照
- `decision_matrix`: 上游主线信号与本项目财务安全边际的矩阵结论
- `research_runs`: 研究历史
- `queue`: 该股票的队列状态
- `trackable_history`: 该股票曾被列为 `A可跟踪龙头` 的日期、评分和报告 ID

## 约束

两个接口都只读，不包含交易指令、现金金额或股数。
新系统只输出 `stock_research` 研究记录；旧任务类型和旧估值记录会在数据库初始化时清理。
