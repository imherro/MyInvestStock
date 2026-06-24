# Codex 自动化设计

## 任务拆分

深研必须一次只研究一只股票。个股研究再拆成战略和财务两类：

1. `MyInvestStock A可跟踪龙头发现队列`
2. `MyInvestStock 个股战略深研 {code} {name}`
3. `MyInvestStock 个股财务估值深研 {code} {name}`

## 发现队列任务

用途：读取 `/api/index`，更新今日待研队列，不做深研。

规则：

- 每只股票如果没有战略底稿，生成一条战略深研任务。
- 每次进入 `A可跟踪龙头` 时都可以生成财务估值深研任务。
- 战略深研默认只做一次；财务估值深研可以多次刷新。

提示词：

```text
在 C:\Users\kunpeng\Documents\MyInvestStock 中运行每日发现队列。

只读取 https://leader.okbbc.com/api/index，并且只使用 key_results.primary_output.items 作为今日 A可跟踪龙头研究对象。禁止从 /api/latest 的 themes[].stock_leaders 扩展股票池。

运行 scripts/ingest_index.py 更新本地 SQLite 队列。完成后汇报 report_id、basis_date、入库股票数量、股票代码和名称，以及生成了哪些 strategic / financial 任务。不要输出 .env 内容。
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
```

## 建议节奏

每日主线龙头更新后：

1. 运行发现队列。
2. 按 `deep_score` 从高到低领取任务。
3. 如果没有战略底稿，先做战略深研。
4. 战略底稿已存在后，多次做财务估值深研。
5. 再领取下一条任务，直到队列完成或达到当日研究预算。
