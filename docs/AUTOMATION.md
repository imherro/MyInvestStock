# Codex 自动化设计

## 任务拆分

深研必须一次只研究一只股票。个股研究再拆成战略和财务两类：

1. `MyInvestStock 推荐龙头读取入队`
2. `MyInvestStock 个股深研队列消化`
3. `MyInvestStock 个股战略深研 {code} {name}`
4. `MyInvestStock 个股财务估值深研 {code} {name}`

## 推荐龙头读取入队自动化

用途：每天在市场研究、主线研究、龙头研究完成之后，读取 `/api/index`，更新今日待研队列，不做深研。

规则：

- 每只股票如果没有战略底稿，生成一条战略深研任务。
- 每次进入 `A可跟踪龙头` 时都可以生成财务估值深研任务。
- 战略深研默认只做一次；财务估值深研可以多次刷新。
- 本任务只做入队和状态汇总，不领取个股研究任务。

提示词：

```text
执行 MyInvestStock 推荐龙头读取入队。

触发前提：这是市场研究、主线研究、龙头研究完成之后的下游入口更新任务。先检查 https://leader.okbbc.com/api/index 是否已经更新完成。只接受 /api/index 中 report.basis_date 已经是最新完整数据可用的交易日，且 key_results.primary_output.items 非空的结果。如果 /api/index 未更新、为空、请求失败，或 report_id 与上一轮完全相同且本地队列已处理过，则停止并汇报原因。

只读取 https://leader.okbbc.com/api/index，并且只使用 key_results.primary_output.items 作为今日 A可跟踪龙头研究对象。禁止从 /api/latest 的 themes[].stock_leaders 扩展股票池。

运行 python scripts/ingest_index.py 更新本地 SQLite 队列。只做入队和状态汇总，不领取 research_queue 任务，不生成个股深研正文，不调用个股战略或财务深研提示词。

随后运行 python scripts/update_stock_prices.py --all-system 刷新系统内相关股票的近期 K 线缓存，覆盖最新可跟踪龙头、历史可跟踪龙头、研究队列和已有研究记录。K 线只用于个股页“合理估值区间历史”的价格参照层，不参与估值计算，不改变研究结论。

完成后验证 http://127.0.0.1:8016/api/index 和 http://127.0.0.1:8016/api/latest，汇报 report_id、basis_date、入库股票数量、股票代码和名称，以及生成或保持的 strategic / financial 队列数量。不要输出 .env 内容，不要提交 .env、data/local/*.sqlite、data/raw/*.json。
```

## 个股深研队列消化自动化

用途：高频运行，持续消化本地待研究队列。每次运行只处理一条任务，处理完即结束；靠下一次自动化继续领取下一条任务。

规则：

- 只从本地 `research_queue` 领取 pending 任务，不重新扩展股票池。
- 队列来源可以是 `可跟踪龙头` 或 `/research?stock={code}` 的 `其他请求`，但领取和执行规则完全相同。
- 领取任务时把状态标记为 `in_progress`，避免高频自动化重复处理同一项。
- 同时把系统级 `task_queue` 状态从 `PENDING` 切到 `RUNNING`；入库完成后切到 `DONE`。
- `RUNNING` 超过 30 分钟会恢复为 `FAILED`，重新入队时经 `RETRY` 回到 `PENDING`。
- `task_queue` 是唯一状态源；`research_queue` 只保存 prompt/projection/UI 字段，禁止写业务状态。
- 每次只研究一只股票、一个任务类型。
- strategic 任务只做战略和竞争研究，不写估值区间。
- financial 任务必须依赖已有 strategic 底稿，可以多次刷新财务输入和确定性估值报告。
- strategic 底稿未完成时，不提前领取对应 financial 任务。
- 主线、ETF、行业热度和龙头确认只引用 MyInvestLeader `/api/index` 已入库的上游信号，不在本项目重新研究主线强弱。
- financial 的 `高估暂缓` 只代表财务安全边际不足，最终页面用上游主线信号和财务安全边际矩阵解释参与类型。
- `run_id` 由 `stock_code + task_type + research_date + schema_version` 计算，数据库唯一，防止重复研究。
- financial 估值区间和 signal 必须由 `core/valuation` 的确定性估值引擎生成；LLM 只负责构建 assembly_input 和解释，不负责计算估值数值。
- financial 最终报告必须由 `core/report.build_stock_report(...)` 或 `scripts/build_research_report.py` 生成，禁止手写 dict 拼装最终结构。
- financial 生成报告时必须开启旁路 trace，使用 `scripts/build_research_report.py --audit-db data/local/myinveststock.sqlite ...` 或等价的 `TraceRecorder + record_trace_events`。
- 如果队列为空，汇报队列为空，不生成研究正文。

提示词：

```text
执行 MyInvestStock 个股深研队列消化。

核心原则：每次自动化运行只处理一条 pending 队列任务，只研究一只股票。不要一次研究多只股票，不要在一个提示词里混合多个股票。不要重新扩展股票池，不要从上游 /api/latest 或其他候选矩阵添加股票。

执行步骤：
1. 在本地队列中领取下一条 pending 任务，优先使用 python scripts/generate_single_stock_prompt.py --next --claim 生成本次唯一研究提示词，并把任务标记为 in_progress。
2. 如果没有待研究任务，验证 http://127.0.0.1:8016/api/index 和 http://127.0.0.1:8016/api/latest 可用后，汇报“队列为空”，本次结束。
3. 如果领取到 strategic 任务：只研究这一只股票的行业位置、市场空间、竞争格局、上下游、战略壁垒、五倍/十倍潜力和战略证伪条件；不写估值区间，不写买卖建议；输出 task_type='strategic' 的结构化 JSON，并通过 scripts/import_research_run.py 入库。
4. 如果领取到 financial 任务：先确认该股票已有 task_type='strategic' 的战略底稿；如果没有战略底稿，把该 financial 任务标记为 blocked，并停止；如果有战略底稿，只收集和整理财务、估值模型输入、同业样本、风险信号和证据，形成结构化 assembly_input；再通过 core/report.build_stock_report(...) 或 scripts/build_research_report.py 生成 task_type='financial' 的最终 JSON，并写入 audit_log trace，然后通过 scripts/import_research_run.py 入库。不要手写最终 StockResearchReport。

数据原则：
- Tushare 是 A 股结构化主源，使用本地 .env，但不要输出任何 token。
- 个股页 K 线缓存由 `python scripts/update_stock_prices.py --all-system` 或 `--code {code}` 刷新，只作为价格参照层。
- 网络资料只作为补充证据，必须记录来源、日期和用途。
- 不输出交易指令，不输出现金金额，不输出股数。
- “重仓资格”只能是研究标签，例如 不具备、观察、可跟踪、核心仓研究资格、高估暂缓。
- 所有研究 JSON 必须符合 `core/schema/stock_report.py` 的 `StockResearchReport`，入库前必须通过 Pydantic 校验；禁止输出 schema 以外字段。

完成后：
- 验证 http://127.0.0.1:8016/api/index 返回 200。
- 验证 http://127.0.0.1:8016/api/latest 返回 200。
- 汇报本次处理的任务类型、股票代码、股票名称、入库状态、主要结论摘要。
- 不要提交 .env、data/local/*.sqlite、data/raw/*.json。
```

## 个股战略深研提示词

用途：一次只研究一只股票的战略、行业、竞争和长期潜力，并把结果作为长期底稿入库。

```text
在 C:\Users\kunpeng\Documents\MyInvestStock 中执行个股战略深研。

唯一研究对象：{code} {name}。

入口信息：
- 数据入口：https://leader.okbbc.com/api/index
- 入口路径：key_results.primary_output.items
- report_id：{report_id}
- basis_date：{basis_date}
- 主题：{theme}

硬约束：
- 只研究这一只股票，禁止同时研究其他 A可跟踪龙头。
- 先读取 /api/index，只使用 key_results.primary_output.items 中匹配 {code} 的记录作为入口。
- 可使用 stock_deep_research.stocks 中匹配 {code} 的记录作为已有基础材料。
- 本任务只做战略、行业、竞争和长期潜力研究，不给最终估值区间。
- 网络资料可作补充证据，但要记录来源和日期。
- 不输出交易指令、不输出现金金额、不输出股数。

必须覆盖：
- 行业位置：公司所处细分赛道、产业链环节、国内/全球地位。
- 市场空间：当前空间、未来 3-5 年扩容逻辑、政策或技术驱动。
- 竞争格局：直接竞争者、替代者、潜在进入者、行业集中度变化。
- 上下游公司：关键供应商、客户、平台、渠道和议价关系。
- 战略壁垒：技术、品牌、渠道、成本、客户锁定、牌照或生态。
- 五倍/十倍潜力：只判断战略条件，不用股价目标代替逻辑。
- 战略证伪条件：什么行业或竞争数据出现后说明长期逻辑错了。

战略深研是长期底稿，默认只做一次；除非公司业务结构、行业格局或长期逻辑发生断层变化，不要每日重复生成。

完成后输出结构化 JSON，并通过 `scripts/import_research_run.py` 入库为 task_type='strategic'。战略 JSON 不允许写估值区间字段。
JSON 必须符合 `core/schema/stock_report.py` 的 `StockResearchReport`：`task_type` 为 `strategic`，`valuation.intrinsic_value_low/mid/high` 均为 `null`，禁止额外字段。
```

## 个股财务估值深研输入构建提示词

用途：一次只研究一只股票的财务结构化输入，并把确定性系统生成的估值区间叠加入库。

```text
在 C:\Users\kunpeng\Documents\MyInvestStock 中执行个股财务估值深研输入构建。

唯一研究对象：{code} {name}。

入口信息：
- 数据入口：https://leader.okbbc.com/api/index
- 入口路径：key_results.primary_output.items
- report_id：{report_id}
- basis_date：{basis_date}
- 主题：{theme}

前置依赖：
- 先读取本地 stock_research_runs 中 {code} 的 task_type='strategic' 最新记录。
- 如果战略深研不存在，先停止并把本任务标记为 blocked，不要跳过前置依赖。

硬约束：
- 只研究这一只股票，禁止同时研究其他 A可跟踪龙头。
- Tushare 是 A 股财务、行情、估值结构化主源。
- 网络资料只用于补充财务口径、行业数据或管理层表述。
- 本任务可以多次重复执行，用最新财务、估值和价格数据刷新结论。
- 本任务只构建 deterministic report 所需的 assembly_input，不直接生成最终 StockResearchReport。
- LLM 只能负责搜集、清洗、归一化输入和解释脚本输出；不能重新计算估值，不能给出新的 grade。
- 估值区间和 valuation signal 必须来自 `core/valuation` 的 deterministic engine，不允许由 LLM 凭空估算。
- 最终 StockResearchReport 必须来自 `core/report.build_stock_report(...)`，由 assembler 生成 report_version、report_hash、valuation、peer_comparison、risk 和 conclusion。
- 报告生成必须记录 audit trace：feature、valuation、signal、report 四个 stage 均要有 input_hash/output_hash。
- 不重新判断主线、ETF 或行业热度；只引用 MyInvestLeader 已入库的上游信号，并说明财务安全边际与主线跟踪价值的区别。
- 不输出交易指令、不输出现金金额、不输出股数。

必须构建 assembly_input：
- 财务输入：收入、利润、毛利率、ROE、现金流、负债、market_cap 等结构化字段。
- 估值输入：current_price, stock_pe/pe, pb, eps, book_value_per_share, industry_pb, fcf_per_share, weights 等模型输入。
- 同业输入：同业 stock_code、pe、roe 和样本选择理由。
- 风险输入：financial_risk、industry_risk、sentiment_risk、invalidation_conditions。
- 解释性输入：行业地位、竞争格局、上下游、年增长率、五倍/十倍潜力校验可以作为文字输入，但不允许覆盖系统最终结论。

执行流程：
1. 收集 Tushare 和必要网络补充资料，形成 assembly_input JSON。
2. 将 assembly_input 写入 temp/assembly_inputs/{code}_financial_{basis_date}.json。
3. 运行 python scripts/build_research_report.py --audit-db data/local/myinveststock.sqlite temp/assembly_inputs/{code}_financial_{basis_date}.json > temp/reports/{code}_financial_{basis_date}.json。
4. 用 python scripts/import_research_run.py temp/reports/{code}_financial_{basis_date}.json 入库。
5. 导入成功后，汇报 run_id、report_hash、audit_log stage 覆盖、verify_run 结果和系统生成的主要结论摘要。
```

## 建议节奏

每日主线龙头更新后：

1. 运行 `MyInvestStock 推荐龙头读取入队`，只更新队列。
2. `MyInvestStock 个股深研队列消化` 高频运行，每次只领取一条任务。
3. 如果没有战略底稿，先做战略深研。
4. 战略底稿已存在后，多次做财务估值深研。
5. 研究完一条就结束，下一次自动化再领取下一条，直到队列为空。

## 报告解释器提示词

用途：把已经入库或已生成的 `StockResearchReport` 翻译成人类可读解释。解释器不参与计算。

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
