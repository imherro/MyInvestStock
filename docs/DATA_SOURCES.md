# 数据源

## 主入口

每日研究对象固定来自：

```text
https://leader.okbbc.com/api/index
```

唯一研究对象路径：

```text
key_results.primary_output.items
```

该路径表示页面中的 `A 可跟踪龙头`，当前用于生成个股深研队列。不要从 `/api/latest` 的 `themes[].stock_leaders` 扩展研究池。

主线环境只读自：

```text
https://theme.okbbc.com/api/index
```

可引用字段包括 `mainline_ranking`、`legacy_theme_ranking` 和 `market`。这些字段只作为已入库上游信号使用；MyInvestStock 不重新研究主线强弱、ETF/板块趋势、拥挤度或风险偏好。

## 结构化金融数据

- Tushare：A 股结构化主源，使用本地 `.env` 中的 `TUSHARE_TOKEN`。
- BaoStock：A 股交叉验证和备用数据源。
- QMT：盘中价格和真实持仓只读导入，后续按需接入。

## 补充证据

- FRED：宏观序列，使用本地 `.env` 中的 `FRED_API_KEY`。
- yfinance：海外市场、海外可比公司补充。
- 网络公开资料：只作补充证据，必须记录来源和日期。

## 密钥规则

- `.env` 不提交。
- `.env.example` 只放变量名和空值。
- 报告和页面不得输出真实 token。
