# MyInvestStock

MyInvestStock 是一个 A 股龙头个股深研工作台。它只从 `https://leader.okbbc.com/api/index` 的 `key_results.primary_output.items` 读取每日 `A 可跟踪龙头`，再把每只股票拆成独立的单股深研任务。

## 当前边界

- 研究入口固定为 `/api/index`。
- 深研任务一次只研究一只股票。
- 单股研究分两类：战略深研默认只做一次，财务估值深研可随数据多次刷新。
- Web 页面为只读展示，不生成交易指令。
- `.env` 只保留在本地，GitHub 只提交 `.env.example`。
- 页面 footer 统一加载 `https://invest.okbbc.com/footer.js`。

## 快速开始

```powershell
python scripts/ingest_index.py
python scripts/run_web.py
```

打开：

```text
http://127.0.0.1:8016/
```

## 单股深研流程

1. 运行 `python scripts/ingest_index.py` 更新今日队列。
2. 运行 `python scripts/generate_single_stock_prompt.py --next` 领取一条待研任务。
3. 把输出提示词交给 Codex 单独研究这一只股票的一个阶段。
4. 将 Codex 输出的结构化 JSON 保存后运行 `python scripts/import_research_run.py <json文件>` 入库。
5. 战略深研完成后形成长期底稿；财务估值深研完成后叠加估值区间历史。

## 主要目录

```text
myinveststock/       核心数据、入库和 Web 代码
scripts/             本地运行、入库、检查和提示词生成脚本
web/static/          页面样式
docs/                数据源、架构和自动化说明
data/local/          本地 SQLite 数据库，默认不提交
data/raw/            接口原始快照，默认不提交
```
