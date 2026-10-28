# 发票管家项目约定

通用规则见 `~/.claude/CLAUDE.md` 与 `D:\my-project\CLAUDE.md`；这里只写本项目特有内容。规则只锁底线，不锁做法。

## 北极星

每月报账，从导入到导出可提交的报销单 ≤ 15 分钟，且每张票可追溯。

## 护栏（不可谈判）

1. 源发票只读：不修改、不删除、不重命名；导出不写回源目录。
2. 财务数据只在本机：不上传云端，不进仓库、不进 CI。
3. 低置信度必须显式；每个识别结果可改、可追溯。
4. 没有测试/CI 证据不算完成；CI 红灯不合并。
5. 删数据、改源文件、花钱、合 `main`，先经 _ming 确认。

## 上下文预算与读法

- 一个模块 = 一个会话；新会话从 `PROGRESS.md` 冷启动，不 resume 巨大旧会话。
- 开头只读 `PROGRESS.md` + 当前任务相关的一篇文档；不读 README、不翻 journal、不扫全仓库。
- 上下文到 60% 开始收尾，70% 硬停；收尾前先写 handoff 再 compact。
- 跨 5 个文件以上的搜索/日志排查交给 subagent，主上下文只收 ≤20 行结论。
- 工具输出封顶：Bash 20k、MCP 10k；看 diff 先 `--stat`。
- 文档硬上限：`PROGRESS.md` 120 行、本文件 150 行、单篇 docs 300 行、journal 每条 15 行；超限即拆/删。

## 单一事实来源

| 信息 | 唯一位置 |
|---|---|
| 当前状态、下一步、风险 | `PROGRESS.md` |
| 计划、范围、验收 | `ROADMAP.md` |
| 为什么这么定 | `docs/DECISIONS.md` |
| 做过什么、证据 | `docs/journal/YYYY-MM.md` |
| 准确率、耗时、修正次数 | `docs/METRICS.md` |
| 被搁置的想法 | `docs/IDEAS.md` |
| 面向用户的说明 | `README.md` |

同一事实只写一处，其他位置只放链接。

## 完成定义（DoD）

验收达标 + 测试/CI 绿 + journal 总结已写 + `PROGRESS.md` 已更新 + 临时文件清理 + 源文件未动。缺一条就是没完成，不能开下一个模块。

每个 PR / 可交付增量必须写一条 ≤15 行总结到 `docs/journal/YYYY-MM.md`，包含：交付、决定、证据、缺口、下一步、成本。聊天里给 _ming 3 行摘要。

## 决策权

- **我自己定**：实现方案、护栏内的库选择、界面细节、测试策略、内部重构、性能取舍。
- **必须问**：删除/覆盖数据、改源发票、接云端或联网、花钱、合 `main`、改变核心工作流。
- **主动建议**：放 `docs/IDEAS.md`，每条“收益 / 成本 / 风险”；每个里程碑评审，不打断当前模块。

## 进化机制

- 坏例变 fixture：每次错识别/字段错都加测试样例和回归测试；黄金集只增不减。
- 重复手工修正 3 次 → 固化为规则或功能。
- 决策带“重新评估条件”（见 `docs/DECISIONS.md`），避免路径依赖。
- 流程规则连续两个里程碑没用上就删；本文件保留流程变更记录。
- 逃生口：规则挡住更优解时，可带一行理由临时绕过并记入 journal，下次复盘决定是否修规则。

## 常用命令

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m ruff check app tests
.venv/Scripts/python run.py
.venv/Scripts/python -m app.cli parse "D:\path\to\invoices" --json out\parsed.json
```

## 流程变更记录

- 2026-10-28：建立三层记忆、三条回路、完成即总结、token 预算；auto-pilot 暂不启用。
