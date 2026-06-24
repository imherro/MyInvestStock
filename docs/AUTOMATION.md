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

完成后验证 http://127.0.0.1:8016/api/index 和 http://127.0.0.1:8016/api/latest，汇报 report_id、basis_date、入库股票数量、股票代码和名称，以及生成或保持的 strategic / financial 队列数量。不要输出 .env 内容，不要提交 .env、data/local/*.sqlite、data/raw/*.json。
```

## 个股深研队列消化自动化

用途：高频运行，持续消化本地待研究队列。每次运行只处理一条任务，处理完即结束；靠下一次自动化继续领取下一条任务。

规则：

- 只从本地 `research_queue` 领取 pending 任务，不重新扩展股票池。
- 领取任务时把状态标记为 `in_progress`，避免高频自动化重复处理同一项。
- 每次只研究一只股票、一个任务类型。
- strategic 任务只做战略和竞争研究，不写估值区间。
- financial 任务必须依赖已有 strategic 底稿，可以多次刷新估值和财务结论。
- strategic 底稿未完成时，不提前领取对应 financial 任务。
- 如果队列为空，汇报队列为空，不生成研究正文。

提示词：

```text
执行 MyInvestStock 个股深研队列消化。

核心原则：每次自动化运行只处理一条 pending 队列任务，只研究一只股票。不要一次研究多只股票，不要在一个提示词里混合多个股票。不要重新扩展股票池，不要从上游 /api/latest 或其他候选矩阵添加股票。

执行步骤：
1. 在本地队列中领取下一条 pending 任务，优先使用 python scripts/generate_single_stock_prompt.py --next --claim 生成本次唯一研究提示词，并把任务标记为 in_progress。
2. 如果没有待研究任务，验证 http://127.0.0.1:8016/api/index 和 http://127.0.0.1:8016/api/latest 可用后，汇报“队列为空”，本次结束。
3. 如果领取到 strategic 任务：只研究这一只股票的行业位置、市场空间、竞争格局、上下游、战略壁垒、五倍/十倍潜力和战略证伪条件；不写估值区间，不写买卖建议；输出 task_type='strategic' 的结构化 JSON，并通过 scripts/import_research_run.py 入库。
4. 如果领取到 financial 任务：先确认该股票已有 task_type='strategic' 的战略底稿；如果没有战略底稿，把该 financial 任务标记为 blocked，并停止；如果有战略底稿，只研究财务质量、增长率、估值方法、合理估值区间、当前价格位置、五倍/十倍潜力财务校验、重仓研究资格和财务证伪条件；输出 task_type='financial' 的结构化 JSON，并通过 scripts/import_research_run.py 入库。

数据原则：
- Tushare 是 A 股结构化主源，使用本地 .env，但不要输出任何 token。
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

## 个股财务估值深研提示词

用途：一次只研究一只股票的财务、估值和价格位置，并把估值区间叠加入库。

```text
在 C:\Users\kunpeng\Documents\MyInvestStock 中执行个股财务估值深研。

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
- 本任务专注财务质量、增长质量、估值区间和重仓研究资格，不重复写泛行业故事。
- 不输出交易指令、不输出现金金额、不输出股数。

必须覆盖：
- 财务质量：收入、利润、毛利率、净利率、ROE、现金流、负债质量。
- 年增长率：近年收入/利润增速、未来增长假设、增长可信度。
- 估值方法：按行业属性选择 PE、PEG、PB、PS、DCF 或分部估值。
- 合理估值区间：保守、合理、乐观三档，必须说明关键假设和触发条件。
- 当前价格位置：只判断高估、合理、低估或观察，不给买卖指令。
- 五倍/十倍潜力校验：用财务和估值条件验证战略深研中的潜力判断。
- 重仓资格：只能写研究标签，如 不具备、观察、可跟踪、核心仓研究资格、高估暂缓。
- 财务证伪条件：什么财务或估值数据出现后说明判断错了。

完成后输出结构化 JSON，并通过 `scripts/import_research_run.py` 入库为 task_type='financial'，保证 /stocks/{code} 能看到估值区间历史叠加。
JSON 必须符合 `core/schema/stock_report.py` 的 `StockResearchReport`：`task_type` 为 `financial`，`valuation.intrinsic_value_low/mid/high` 必须全部为数字且 `low <= mid <= high`，禁止额外字段。
```

## 建议节奏

每日主线龙头更新后：

1. 运行 `MyInvestStock 推荐龙头读取入队`，只更新队列。
2. `MyInvestStock 个股深研队列消化` 高频运行，每次只领取一条任务。
3. 如果没有战略底稿，先做战略深研。
4. 战略底稿已存在后，多次做财务估值深研。
5. 研究完一条就结束，下一次自动化再领取下一条，直到队列为空。
