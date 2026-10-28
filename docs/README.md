# 文档地图

> 目标：任何一次会话都能在 1 分钟内找到该读的东西，不扫全仓库。

## 按任务读

| 你要做什么 | 先读 | 再读 |
|---|---|---|
| 恢复上下文 / 开始新模块 | `PROGRESS.md` | 当前模块对应的 docs 一篇 |
| 改票面解析 | `PROGRESS.md` | `docs/PARSER.md` + 对应 parser 与 fixtures |
| 改数据/导出 | `PROGRESS.md` | `docs/ARCHITECTURE.md` 的数据流一节 |
| 改界面 | `PROGRESS.md` | `docs/ARCHITECTURE.md` 的 UI 一节 + 前端文件 |
| 查旧决定 | `docs/DECISIONS.md` | 相关 ADR 的“重新评估条件” |
| 查历史做了什么 | `docs/journal/YYYY-MM.md` | 只在需要追溯时读 |
| 规划下一阶段 | `PROGRESS.md` | `ROADMAP.md` + `docs/METRICS.md` + `docs/IDEAS.md` |

## 不要默认读

- `docs/journal/`（冷记录，只在查旧账时读）
- `legacy/`（归档旧脚本，不维护）
- `README.md`、`docs/IDEAS.md`（除非任务需要）
- 真实发票、导出文件、日志、构建产物

## 单一事实来源

- 当前状态 → `PROGRESS.md`
- 计划/验收 → `ROADMAP.md`
- 为什么 → `docs/DECISIONS.md`
- 证据/历史 → `docs/journal/`
- 指标 → `docs/METRICS.md`
- 想法 → `docs/IDEAS.md`

文档超预算（单篇 300 行）就拆分或删除，不靠继续追加。
