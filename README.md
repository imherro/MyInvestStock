# MyInvestStock

MyInvestStock 是一个 A 股个股深研工作台，用来承接上游龙头研究结果，对 `A 可跟踪龙头` 做行业地位、竞争格局、上下游、财务增长、合理估值区间、五倍十倍潜力和重仓研究资格判断。

系统定位是“研究与展示”，不是交易系统。页面和接口均为只读输出，不生成买卖指令、不输出现金金额、不输出股数。

## 一句话逻辑

每天从 `https://leader.okbbc.com/api/index` 读取 `key_results.primary_output.items` 中的 `A 可跟踪龙头`，把每只股票拆成独立研究任务；战略深研一般只做一次，财务估值深研可以多次刷新，并在个股页叠加历史估值区间。

## 核心边界

- 唯一上游入口：`https://leader.okbbc.com/api/index`。
- 唯一股票池路径：`key_results.primary_output.items`。
- 禁止从上游 `/api/latest` 的 `themes[].stock_leaders`、`stock_deep_research.stocks` 或其他候选矩阵扩展股票池。
- 深研必须一次只研究一只股票。
- 个股研究分为 `strategic` 和 `financial` 两类。
- `strategic` 是战略、行业、竞争和长期潜力底稿，默认只做一次。
- `financial` 是财务质量、增长率、估值区间和价格位置研究，可以随着新数据多次刷新。
- `financial` 必须依赖已完成的 `strategic` 底稿；战略未完成时不提前领取财务任务。
- 新研究结果必须符合 `core/schema/stock_report.py` 的 Pydantic schema，入库前强制校验。
- 队列任务使用 `core/task/state.py` 的状态机：`PENDING -> RUNNING -> DONE/FAILED/BLOCKED`，失败任务经 `RETRY -> PENDING` 后才能重跑。
- `run_id` 是幂等真值，由 `stock_code + task_type + research_date + schema_version` 计算，数据库强制唯一。
- Web 默认端口固定为 `8016`。
- 页面 footer 统一加载 `https://invest.okbbc.com/footer.js`。
- `.env`、本地 SQLite、原始抓取 JSON 和临时产物不提交、不打包给外部审计。

## 系统架构

```text
上游龙头研究 /api/index
        |
        v
scripts/ingest_index.py
        |
        v
SQLite:
  leader_reports
  trackable_leaders
  task_queue
  research_queue
  stock_research_runs
        |
        +--> scripts/generate_single_stock_prompt.py
        |       每次领取一条可研究任务
        |
        +--> scripts/import_research_run.py
        |       导入 Codex 深研 JSON
        |
        v
myinveststock/web.py
  /                  Web 首页
  /stocks/{code}     个股页
  /api/index         对外主结果
  /api/latest        对外研究成果
  /api/queue         本地队列
```

## 自动化设计

Codex 自动化拆成两步，避免“每日入口更新”和“长耗时深研”互相阻塞。

### 1. 推荐龙头读取入队

自动化名称：`MyInvestStock 推荐龙头读取入队`

运行节奏：工作日晚上，在市场研究、主线研究、龙头研究完成后运行一次。

职责：

- 检查 `https://leader.okbbc.com/api/index` 是否已经有最新完整数据。
- 只读取 `key_results.primary_output.items`。
- 运行 `python scripts/ingest_index.py` 更新本地队列。
- 只做入队和状态汇总，不做任何个股深研。

### 2. 个股深研队列消化

自动化名称：`MyInvestStock 个股深研队列消化`

运行节奏：高频运行，当前配置为每小时一次。

职责：

- 使用 `python scripts/generate_single_stock_prompt.py --next --claim` 领取下一条依赖已满足的任务。
- 领取后立即把任务状态标记为 `in_progress`，避免重复研究同一任务。
- 同步把 `task_queue` 状态从 `PENDING` 切到 `RUNNING`；导入完成后切到 `DONE`。
- `RUNNING` 超过 30 分钟会恢复为 `FAILED`，后续重新入队时增加重试计数并回到 `PENDING`。
- 每次只研究一只股票、一个任务类型。
- 研究完成后输出结构化 JSON，并用 `scripts/import_research_run.py` 入库。
- 如果队列为空或前置战略深研未完成，本次结束，不扩展股票池。

## 研究任务规则

### 战略深研 strategic

用途：形成长期底稿。

必须覆盖：

- 行业位置：公司所处细分赛道、产业链环节、国内或全球地位。
- 市场空间：未来 3-5 年扩容逻辑、政策或技术驱动。
- 竞争格局：直接竞争者、替代者、潜在进入者、集中度变化。
- 上下游公司：关键供应商、客户、平台、渠道和议价关系。
- 战略壁垒：技术、品牌、渠道、成本、客户锁定、牌照或生态。
- 五倍十倍潜力：只判断战略条件，不用股价目标代替逻辑。
- 战略证伪条件：哪些行业或竞争数据出现后说明长期逻辑错了。

限制：

- 不写估值区间。
- 不写买卖建议。
- 不输出现金金额或股数。

### 财务估值深研 financial

用途：刷新财务质量、估值区间和价格位置。

必须覆盖：

- 财务质量：收入、利润、毛利率、净利率、ROE、现金流、负债质量。
- 年增长率：近年收入和利润增速、未来增长假设、增长可信度。
- 估值方法：按行业属性选择 PE、PEG、PB、PS、DCF 或分部估值。
- 合理估值区间：保守、合理、乐观三档，并说明关键假设。
- 当前价格位置：只判断高估、合理、低估或观察，不给交易指令。
- 五倍十倍潜力校验：用财务和估值条件验证战略判断。
- 重仓研究资格：只能是研究标签，如 `不具备`、`观察`、`可跟踪`、`核心仓研究资格`、`高估暂缓`。
- 财务证伪条件：哪些财务或估值数据出现后说明判断错了。

## 数据原则

- Tushare 是 A 股结构化主源，通过本地 `.env` 读取 token。
- 网络资料只作为补充证据，必须记录来源、日期和用途。
- 不在日志、页面、接口、审计包中输出 token。
- 本地数据库在 `data/local/`，默认不提交。
- 原始接口快照在 `data/raw/`，默认不提交。

## 快速开始

安装依赖：

```powershell
python -m pip install -r requirements.txt
```

更新队列：

```powershell
python scripts/ingest_index.py
```

启动 Web：

```powershell
python scripts/run_web.py
```

打开：

```text
http://127.0.0.1:8016/
```

## 常用命令

查看下一条待研究任务但不领取：

```powershell
python scripts/generate_single_stock_prompt.py --next
```

领取下一条可研究任务并标记为处理中：

```powershell
python scripts/generate_single_stock_prompt.py --next --claim
```

导入一条研究 JSON：

```powershell
python scripts/import_research_run.py path\to\research.json
```

运行项目检查：

```powershell
python scripts/project_check.py
```

运行测试：

```powershell
python -m pytest tests -q
```

## Web 与接口

- `/`：A 可跟踪龙头首页，显示当前股票、评分、市场摘要和个股深研队列。
- `/stocks/{code}`：个股页，显示估值区间历史、行业地位、竞争格局、上下游、增长率、五倍十倍潜力和重仓研究资格。
- `/api/index`：对外主结果接口，供其他系统集成，主结果路径为 `key_results.primary_output.items`。
- `/api/latest`：对外研究成果接口，输出个股战略、财务和估值历史。
- `/api/queue`：本地研究队列接口。
- `/api/stocks`：当前股票列表。
- `/api/stocks/{code}`：单只股票研究数据。

## 主要目录

```text
myinveststock/       核心数据、队列、入库和 Web 代码
scripts/             本地运行、入库、检查和提示词生成脚本
tests/               接口与约束测试
web/static/          页面样式
docs/                数据源、架构、自动化、接口和研究结构说明
data/local/          本地 SQLite 数据库，默认不提交
data/raw/            上游接口原始快照，默认不提交
research/            可选研究产物，不含密钥
temp/                临时文件和审计打包目录，默认不提交
```

## 审计关注点

建议审计者重点看：

- `docs/API_CONTRACT.md`：`/api/index` 与 `/api/latest` 的接口契约。
- `docs/AUTOMATION.md`：两步自动化和单股单任务提示词。
- `docs/ARCHITECTURE.md`：系统数据流和模块边界。
- `docs/RESEARCH_SCHEMA.md`：研究 JSON 入库结构。
- `core/schema/stock_report.py`：强类型研究报告 schema 和 validation gate。
- `core/task/state.py`：任务状态机、合法状态转换和 run_id 生成规则。
- `myinveststock/leader_index.py`：只从 `/api/index` 的 `key_results.primary_output.items` 入队。
- `myinveststock/db.py`：队列表结构、依赖判断和任务领取逻辑。
- `myinveststock/web.py`：只读页面和对外 API。
- `scripts/project_check.py`：项目约束检查。
- `tests/test_contracts.py`：关键契约测试。

## 审计包安全边界

给外部审计的 zip 包应包含源码、文档、测试、`.env.example` 和必要的非敏感研究样例；应排除：

- `.env`
- `.git/`
- `.pytest_cache/`
- `__pycache__/`
- `data/local/*.sqlite`
- `data/raw/*.json`
- `temp/`
- `*.log`
- 任何包含 token、secret、password、key 的真实文件

打包后应检查文件清单和敏感文件名扫描结果，再交给审计。
