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

## 约束

两个接口都只读，不包含交易指令、现金金额或股数。
