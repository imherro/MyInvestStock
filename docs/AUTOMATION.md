# Codex 自动化设计

## 任务拆分

新系统只保留一种研究任务：

```text
MyInvestStock 个股深研 {code} {name}
```

`task_type` 固定为 `stock_research`。不再生成旧的两阶段任务；数据库初始化会清理旧队列、旧任务状态和旧研究记录。

## 推荐龙头读取入队自动化

用途：每天在市场研究、主线研究、龙头研究完成之后，读取 Leader `/api/index` 的可跟踪龙头，并同步 Theme `/api/index` 的主线环境快照。

规则：

- 只使用 `https://leader.okbbc.com/api/index -> key_results.primary_output.items` 作为今日股票入口。
- 同步读取 `https://theme.okbbc.com/api/index -> mainline_ranking / legacy_theme_ranking / market` 作为主线环境。
- 新股票只生成一条 `stock_research` 任务，`trigger_reason=新进入可跟踪龙头`。
- 已有 `stock_research` 报告或待处理队列的股票不重复入队。
- 本任务只做入队和状态汇总，不领取个股研究任务。

提示词：

```text
执行 MyInvestStock 推荐龙头读取入队。

先检查 https://leader.okbbc.com/api/index 是否已经更新完成。只接受 Leader /api/index 中 report.basis_date 已经是最新完整数据可用的交易日，且 key_results.primary_output.items 非空的结果。

只使用 key_results.primary_output.items 作为今日 A可跟踪龙头研究对象。禁止从 /api/latest 的 themes[].stock_leaders 扩展股票池。

同时读取 https://theme.okbbc.com/api/index，只引用 mainline_ranking、legacy_theme_ranking 和 market 作为主线环境快照。

运行 python scripts/ingest_index.py 更新本地 SQLite 队列。新任务统一为 task_type='stock_research'，trigger_reason='新进入可跟踪龙头'。

随后运行 python scripts/update_stock_prices.py --all-system 刷新系统内相关股票从 2024-09-24 起的收盘价缓存。

完成后验证 http://127.0.0.1:8016/api/index 和 http://127.0.0.1:8016/api/latest，汇报 leader report_id、theme report_id、basis_date、入库股票数量、股票代码和名称，以及生成或保持的 stock_research 队列数量。不要输出 .env 内容，不要提交 .env、data/local/*.sqlite、data/raw/*.json。
```

## 个股重研触发监测自动化

用途：每日检查是否需要重新做完整个股深研。它只判断是否入队，不直接研究。

当前已落地的触发：

- 没有任何 `stock_research` 报告：`新进入可跟踪龙头`
- 当前价格明显偏离上一版合理估值区间：`估值中枢变化`

后续可接入同一字段的触发：

- 财报更新
- 重大事件
- 龙头证据变化
- 主线阶段明显变化
- 定期复核

命令：

```powershell
python scripts/monitor_research_triggers.py
```

只看不入队：

```powershell
python scripts/monitor_research_triggers.py --dry-run
```

## 个股深研队列消化自动化

用途：高频运行，持续消化本地待研究队列。每次运行只处理一条任务，处理完即结束。

规则：

- 只从本地 `research_queue` 领取 pending 任务，不重新扩展股票池。
- 每次只研究一只股票。
- 每条任务的 `task_type` 必须是 `stock_research`。
- `trigger_reason` 只解释本次为什么重研，不拆分不同流程。
- 合理估值区间和 valuation signal 必须由 `core/valuation` 的确定性估值引擎生成。
- 最终报告必须由 `core/report.build_stock_report(...)` 或 `scripts/build_research_report.py` 生成。
- LLM 只负责构建结构化输入和解释，不能重新计算估值或评分。
- 所有研究 JSON 必须符合 `core/schema/stock_report.py` 的 `StockResearchReport`。

提示词：

```text
执行 MyInvestStock 个股深研队列消化。

核心原则：每次自动化运行只处理一条 pending 队列任务，只研究一只股票。不要一次研究多只股票，不要重新扩展股票池。

执行步骤：
1. 使用 python scripts/generate_single_stock_prompt.py --next --claim 领取下一条 pending 任务。
2. 如果没有待研究任务，验证 http://127.0.0.1:8016/api/index 和 http://127.0.0.1:8016/api/latest 可用后，汇报“队列为空”，本次结束。
3. 如果领取到任务，只研究这一只股票，输出完整个股深研结构化输入。
4. 运行 scripts/build_research_report.py 生成最终 StockResearchReport。
5. 运行 scripts/import_research_run.py 入库。

数据原则：
- Tushare 是 A 股结构化主源，使用本地 .env，但不要输出任何 token。
- Leader 只提供个股入口和龙头证据。
- Theme 只提供主线环境。
- 网络资料只作为补充证据，必须记录来源、日期和用途。
- 不输出交易指令，不输出现金金额，不输出股数。
- “重仓资格”只能是研究标签，例如 不具备、观察、可跟踪、核心仓研究资格、高估暂缓。

完成后：
- 验证 http://127.0.0.1:8016/api/index 返回 200。
- 验证 http://127.0.0.1:8016/api/latest 返回 200。
- 汇报本次处理的股票代码、股票名称、触发原因、入库状态、主要结论摘要。
- 不要提交 .env、data/local/*.sqlite、data/raw/*.json。
```

## 个股完整深研提示词

```text
在 C:\Users\kunpeng\Documents\MyInvestStock 中执行个股完整深研。

唯一研究对象：{code} {name}。
本次触发原因：{trigger_reason}。

入口信息：
- 个股入口：https://leader.okbbc.com/api/index
- 个股入口路径：key_results.primary_output.items
- 主线环境入口：https://theme.okbbc.com/api/index
- 主线环境路径：mainline_ranking / legacy_theme_ranking / market
- report_id：{report_id}
- theme_report_id：{theme_report_id}
- basis_date：{basis_date}
- 主题：{theme}

硬约束：
- 只研究这一只股票。
- task_type 固定为 stock_research。
- 先读取上一版 stock_research 报告；如果存在，说明哪些结论改变、哪些没有改变。
- 龙头确认只引用 MyInvestLeader 入库信号。
- 主线强度、生命周期、周期阶段、ETF/板块趋势、拥挤度和风险偏好只引用 MyInvestTheme 入库信号。
- 合理估值区间和评分必须来自确定性估值流水线。
- LLM 不得手写最终 StockResearchReport。
- 不输出交易指令、现金金额或股数。

必须覆盖：
- 行业位置
- 竞争格局
- 上下游公司
- 财务质量
- 年增长率
- 合理估值区间
- 数倍潜力
- 重仓研究资格
- 风险与证伪条件
- 本次相对上一版的变化

执行流程：
1. 构建 stock_research assembly_input。
2. 将 assembly_input 写入 temp/assembly_inputs/{code}_stock_research_{basis_date}.json。
3. 运行 python scripts/build_research_report.py --audit-db data/local/myinveststock.sqlite temp/assembly_inputs/{code}_stock_research_{basis_date}.json > temp/reports/{code}_stock_research_{basis_date}.json。
4. 用 python scripts/import_research_run.py temp/reports/{code}_stock_research_{basis_date}.json 入库。
```

## 报告解释器提示词

```text
你是 A 股研究报告解释器。

输入是已经通过 schema 校验的 StockResearchReport。你的任务是解释，不是计算。

禁止：
- 不得修改任何数值、估值区间、signal、grade、report_hash 或 run_id。
- 不得重新估值。
- 不得引入新外部数据。
- 不得给出新的买卖建议、现金金额或股数。
- 不得用自己的判断覆盖系统结论。

输出只做五件事：
1. 解释核心财务状态。
2. 解释已有估值区间和 signal 的含义。
3. 解释 risk 字段中的主要风险来源。
4. 解释系统为什么给出当前 heavy_position_view / conclusion.grade。
5. 用通俗语言总结结论。
```
