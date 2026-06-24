# 架构设计

## 目标

为每日 `A 可跟踪龙头` 提供独立个股页面，展示：

- 合理估值区间及历史叠加
- 行业地位
- 竞争格局
- 上下游公司
- 年增长率
- 五倍/十倍潜力
- 是否具备重仓研究资格
- 个股历史研究记录

## 数据流

```mermaid
flowchart LR
  A["leader.okbbc.com /api/index"] --> B["ingest_index.py"]
  B --> C["SQLite: leader_reports"]
  B --> D["SQLite: trackable_leaders"]
  B --> E["SQLite: research_queue"]
  D --> U["upstream_signal: MyInvestLeader 主线/龙头快照"]
  E --> F["generate_single_stock_prompt.py"]
  F --> G1["Codex 个股战略深研"]
  F --> G2["Codex 个股财务估值深研"]
  G1 --> H["SQLite: stock_research_runs"]
  G2 --> H
  H --> I["8016 Web 个股页"]
  D --> I
  U --> I
```

## 分层

- `myinveststock/leader_index.py`：读取 `/api/index`，只解析 `key_results.primary_output.items`。
- `myinveststock/db.py`：SQLite schema 和读写函数。
- `myinveststock/web.py`：只读 Web 页面和 JSON API。
- `scripts/ingest_index.py`：每日发现队列。
- `scripts/generate_single_stock_prompt.py`：一次只生成一只股票、一个阶段的深研提示词。
- `scripts/run_web.py`：启动 8016 本地 Web。

## 上游主线信号边界

MyInvestLeader 负责主线、ETF、行业热度和龙头确认。MyInvestStock 不重新研究主线强弱，只把 `/api/index` 中已经入库的主题、龙头证据、主题绑定、交易结构和风险提示整理为 `upstream_signal`。

MyInvestStock 的确定性估值只回答“财务安全边际是否足够”。页面和 `/api/latest` 使用 `decision_matrix` 组合两类信号：

- 上游主线强 + 财务安全高：核心候选研究。
- 上游主线强 + 财务安全低：主线弹性跟踪，不按安全边际重仓。
- 上游主线弱 + 财务安全高：价值观察，等待催化。
- 上游主线弱 + 财务安全低：风险释放优先。

## Web 路由

- `/`：今日 A 可跟踪龙头列表。
- `/api/index`：主要结果接口，面向其他系统集成。
- `/api/latest`：研究成果接口，面向研究结果消费。
- `/stocks/{code}`：个股深研页面。
- `/api/stocks`：最新股票列表 JSON。
- `/api/stocks/{code}`：个股页面数据 JSON。
- `/api/queue`：当前研究队列 JSON。

## 页面约束

所有页面底部统一加载：

```html
<script src="https://invest.okbbc.com/footer.js" defer></script>
```

Web 侧不写入数据库，所有入库都由脚本或自动化任务完成。

## 两阶段研究

- `strategic`：战略深研，关注行业空间、竞争格局、上下游、长期壁垒和五倍/十倍潜力。默认只做一次，除非长期逻辑发生重大变化。
- `financial`：财务估值深研，关注财务质量、增长率、估值区间、当前价格位置和重仓研究资格。可随财报、价格和主线变化多次刷新。
