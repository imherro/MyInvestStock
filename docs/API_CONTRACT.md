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

关键字段：

- `schema_version`: `myinveststock.index.v1`
- `source.upstream_endpoint`: 上游固定为 `https://leader.okbbc.com/api/index`
- `key_results.primary_output.items`: 当前主结果股票列表
- `key_results.primary_output.items[].links.research_gateway`: 主动研究入口 `/research?stock={code}`
- `links.latest`: 研究成果接口 `/api/latest`

## `/api/latest`

用途：输出当前研究成果。

关键字段：

- `schema_version`: `myinveststock.research.v1`
- `summary.stock_count`: 当前主结果股票数量
- `summary.research_run_count`: 已入库研究记录数量
- `stocks[].leader`: 该股票的主结果信息
- `stocks[].research.strategic`: 最新战略深研底稿
- `stocks[].research.financial`: 最新财务估值深研
- `stocks[].research.valuation_history`: 历次估值区间

## `/research?stock={code}`

用途：从外部系统跳转到个股研究页面。

行为：

- 如果本地已有该股票页面、研究记录或队列任务，返回 `303` 跳转到 `/stocks/{code}`。
- 如果本地没有该股票，创建当天主动请求队列批次，来源标记为 `manual_request` / `其他请求`，再返回 `303` 跳转到 `/stocks/{code}?queued=1`。
- 不要求该股票出现在 `/api/index` 的 `key_results.primary_output.items`。
- 不直接执行深研，不绕过队列领取和单股单任务规则。

## `/api/queue`

用途：输出本地研究队列。

新增来源字段：

- `source_type`: `trackable_leader` 或 `manual_request`
- `source_label`: `可跟踪龙头` 或 `其他请求`

## `/api/stocks/{code}`

用途：输出单只股票页面数据。

关键字段：

- `leader`: 当前或历史最近一次可跟踪龙头记录，没有则为 `null`
- `research_runs`: 研究历史
- `queue`: 该股票的队列状态
- `trackable_history`: 该股票曾被列为 `A可跟踪龙头` 的日期、评分和报告 ID

## 约束

两个接口都只读，不包含交易指令、现金金额或股数。
