# 发票管家 InvoiceDesk

本地离线的发票整理与报销单生成工具。把一叠 PDF 发票拖进来，自动解析、人工核对、一键导出可以直接提交的《费用报销支出明细表》和分类打印 PDF。

> 当前状态：**Phase 3 产品化中，含本地 OCR、自动备份与首次启动向导** — 解析引擎、SQLite 数据层、
> 导入去重与预检、发票级导出、报销行与报销单 Excel、桌面工作台、字段修正、规则管理、汇总看板、
> 图片发票 OCR、常用文件夹/表头记忆、待确认队列与报账向导、重复票审查（归档可恢复）、滚动备份与
> 恢复、首次启动向导（数据目录 + 导入引导）、PyInstaller 单目录包已实现（向导与预检待实机验收），
> 见 [ROADMAP.md](ROADMAP.md)。

## 解决什么问题

旧版脚本只做“从 PDF 抠数字”：
- 销售方、发票号基本提取不到，重复票查不出；
- 金额依赖兜底逻辑，遇到折扣/多行/红冲票不可靠；
- 分类和实际报销单不一致；
- 结果写回发票源文件夹，没有数据库、不能修改、没有“无票”费用；
- 每次重跑都从零开始。

新版围绕两张表工作：

1. **发票池**：每张 PDF 都是可编辑、可追溯、可去重的记录。
2. **报销单**：按现有 Excel 格式组织，支持有票行、无票行（里程/餐费/工资）和一行关联多张发票。

## 核心原则

- 源发票只读，导出不写回源目录。
- 财务数据只在本机；不上传云端，不进仓库、不进 CI。
- 低置信度必须显式标出，每个字段可人工修正。
- 没有测试/CI 证据不算完成。

## 计划技术栈

Python 3.11+（CI 用 3.12；OCR 依赖暂不支持 3.13）+ pywebview（WebView2）+ SQLite +
PyMuPDF + openpyxl + RapidOCR（本地 ONNX 模型）；前端 Vue 3 本地内置、无 Node 构建，
离线运行。Tabulator/ECharts 按需在后续阶段引入（ADR-012）。OCR 模型随程序打包，不上传
任何票面。

## 开发

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m ruff check app tests
```

启动桌面工作台：`.venv/Scripts/python run.py`（也可用 `python run.py`；本地 `python_env` 的
`_pth` 隔离了工作目录，故不从 `-m app` 启动）。

### 解析引擎 CLI（Phase 1a）

```bash
.venv/Scripts/python -m app.cli parse "D:\发票\2026年7月" --json out\parsed.json
```

输出逐张票的类型、日期、金额、类别、票号；低置信度/多票页面会标“待确认”。支持全电普票/专票、老版增值税电子普票、通行费、铁路客票、出租车/网约车；没有文本层的图片/扫描发票会用本地 OCR（RapidOCR）识别，结果明确标“待确认”并保留可编辑字段。已有库里的旧扫描记录可在工作台点“重新识别未识别”补识别，不会覆盖人工修正。

默认只打印汇总；`--verbose` 才显示逐张明细。JSON 默认只含文件名并隐藏买卖双方税号（`--include-tax-ids` 才输出完整值），且拒绝写入仓库内非 `out/`、`data/` 路径。真实发票输出不要提交，也不要把 stdout 重定向到仓库内文件。

### 导入与查询（Phase 1b）

```bash
# 解析文件夹并写入本地库；已导入文件按 SHA-256 跳过，
# 票号重复且日期/金额/销售方一致才跳过，不一致保留并标记“冲突”
.venv/Scripts/python -m app.cli import "D:\发票\2026年7月" --db data\invoicedesk.db --label 2026年7月

# 查看批次与合计
.venv/Scripts/python -m app.cli stats --db data\invoicedesk.db
```

数据库默认 `data/invoicedesk.db`（整目录 gitignore）。库内是财务数据，只在本机；路径守卫在数据层：仓库内只允许 `data/`、`out/`、`tests/local/`，UNC/网络盘/OneDrive 默认拒绝，CLI 需 `--force` 才例外。`import` 可重复执行：已导入文件不会再次解析；`--debug` 才显示非预期错误的堆栈。

### 导出（Phase 1b-2）

```bash
# 导出当前库：Excel（明细/分类汇总/公司汇总/导出信息）+ A4 二合一打印 PDF
.venv/Scripts/python -m app.cli export --db data\invoicedesk.db --out out\export

# 只导出某公司、某月的发票，并排除待确认记录
.venv/Scripts/python -m app.cli export --db data\invoicedesk.db --out out\export --buyer "示例公司" --from 2026-07-01 --to 2026-07-31 --exclude-review
```

Excel 明细含票型、低置信字段、待确认、识别警告和来源文件名+页码（不含本机路径，可直接提交；警告里若混入本机路径也会在导出前擦除）；待确认默认包含并用黄色标出。打印 PDF 每张 A4 放两页发票，页眉标注金额、分类、销售方与待确认状态；同一源页面即使对应多条记录也只打印一次。缺源文件会跳过并在终端警告，Excel 仍完整导出；如果本次没有任何可打印票面，会生成一页说明 PDF 覆盖旧文件，避免新旧数据混用。源发票只读，导出不写回源目录；符号链接目标拒绝写入。两个产物先写临时文件再替换，写失败不会破坏上一版导出。输出路径先解析再受守卫：仓库内只允许 `out/`、`data/`，网络盘/OneDrive 需 `--force`。

### 报销单（Phase 2a）

发票是证据，报销单是产物；一行可以关联 0..n 张发票，无票行和里程行是一等公民。

```bash
# 创建报销单（--title 可覆盖自动标题）
.venv/Scripts/python -m app.cli report create --db data\invoicedesk.db --company 示例公司 --reporter 张三 --month 2026-07 --department 示例部门

# 从发票池生成行：每张未关联发票一行，跳过已关联/缺金额/缺日期
.venv/Scripts/python -m app.cli report from-invoices --report 1 --batch 2 --project 示例项目

# 手动无票行、里程行（默认 1.5 元/km，可 --rate 调整）
.venv/Scripts/python -m app.cli report add-line --report 1 --date 2026-07-19 --category 转账 --amount -1000.00 --description "示例转账"
.venv/Scripts/python -m app.cli report add-mileage --report 1 --date 2026-07-10 --km 150 --description "示例行程" --project 示例项目二

# 关联/取消关联发票；有票但票据暂未入库时用 --has-invoice 标记
.venv/Scripts/python -m app.cli report link --report 1 --line 3 --invoice 12,13
.venv/Scripts/python -m app.cli report unlink --report 1 --line 3 --invoice 13

# 查看、改行、删行
.venv/Scripts/python -m app.cli report list
.venv/Scripts/python -m app.cli report show --report 1
.venv/Scripts/python -m app.cli report update-line --report 1 --line 1 --amount 200 --description "修正后的说明"
.venv/Scripts/python -m app.cli report remove-line --report 1 --line 2

# 导出《费用报销支出明细表》
.venv/Scripts/python -m app.cli report export --report 1 --out out\report --layout company
```

导出复刻现有表：标题、部门/报销人行、报销金额行、表头、细边框、A4 纵向、打印时每页重复表头；文件名形如
`示例公司费用报销支出明细表（张三）2607.xlsx`。版式 `project`（项目名称列，默认）、`company`（公司列，旧表版式）、
`plain`（仅备注列）；`--project-header 费用发生项目` 可替换默认表头。金额以整数分计算，报销单合计与源表一致；
用户输入文字不折叠全角标点。导出路径与发票导出同口径守卫，先写临时文件再替换，源目录不受影响。

### 桌面工作台（Phase 2b）

```bash
.venv/Scripts/python run.py
```

窗口内可以：按批次/日期/关键词筛选发票池（列表直接显示购买方公司和分类，点分类标签即可
修正，同样写审计并学习销售方规则），勾选发票直接生成报销行；行内编辑日期、费用名称、
金额、有票/附注、项目与备注；点击发票查看渲染后的票面、低置信字段和识别警告；直接修正票号/
日期/销售方/购买方/金额/分类/项目并查看修正记录；新增里程行；新建/编辑报销单表头；管理
销售方分类规则（停用、删除、先预览再回溯历史发票）；导入文件夹（原生选择或粘贴路径，已导入
文件按内容跳过）；导出报销单与发票证据包；打开汇总看板查看当前筛选口径的合计、待处理计数与
分类/月度分布；按票号聚合查看重复票/同号冲突，保留一张、归档其余（不删源文件，可随时恢复）。

数据仍只在本机：pywebview 加载本地 HTML，JS 只通过 `app/api.py` 调用 SQLite，不开本地端口；
票面预览由 PyMuPDF 在内存中渲染，不落盘。外观可自动跟随系统或手动切换浅色/深色，只影响本机
界面；`F1` 查看快捷键（切换发票池、汇总、导入、导出、焦点搜索等）。

尚未实现：费用额度看板（需要录入当月收入，排在 Phase 4）、多页/多票自动拆分、Tabulator/ECharts。
阈值与计划见 ROADMAP，决策见 docs/DECISIONS.md。

### Windows 打包版（Phase 3a）

```bash
python -m pip install -r requirements-build.txt
python tools/build_app.py
```

产物是 `dist/InvoiceDesk/InvoiceDesk.exe`（one-dir，约 64 MB，含 UI 与依赖），分发时要复制
整个 `dist\InvoiceDesk` 文件夹，不能只拷贝 exe；构建同时生成 `build/icon.ico`。构建脚本先用
标准库生成多尺寸图标，再跑 PyInstaller，最后用
`InvoiceDesk.exe --smoke` 在临时目录验证 UI 资源、WebView2 与数据库初始化；任一检查失败都会让
构建返回非零。`--skip-build` / `--skip-smoke` 可复用现有产物或只构建。

打包版要求 Windows 装有 **Microsoft Edge WebView2 Runtime**；缺失时程序弹窗给出下载地址并退出，
不会静默失败。数据默认放在 `%LOCALAPPDATA%\InvoiceDesk\`：`invoicedesk.db`、`out\` 导出目录、
`logs\invoicedesk.log`（滚动 1 MB × 3 份）。不再写入安装目录，升级/重装不会覆盖数据；可用
`INVOICEDESK_HOME` 改整个根目录，`INVOICEDESK_DB` / `INVOICEDESK_OUT` 单独覆盖数据库与导出目录。
源码运行时仍使用仓库内 `data/` 与 `out/`。

每次启动和每次有新增文件的导入后，数据库会自动快照到数据库旁的 `backups\`（最多保留 10 份，
只在本机）。「更多 → 数据备份…」里可以立即备份、打开备份目录或恢复某个快照；恢复前会先把当前
数据库另存为“恢复前”备份，源发票文件不受影响。数据库打不开时，启动会询问是否用最近一次备份恢复。

窗口版没有控制台；启动失败和未捕获异常会写入本地日志并弹窗。构建产物未签名，首次运行可能触发
SmartScreen 提示；只在本机/内网分发，不要把含真实发票的数据库或导出文件提交或上传。

## 文档

- [ROADMAP.md](ROADMAP.md) — 阶段与验收标准
- [PROGRESS.md](PROGRESS.md) — 当前状态与下一步（每次会话先读）
- [docs/README.md](docs/README.md) — 文档地图
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 架构
- [docs/PARSER.md](docs/PARSER.md) — 解析引擎设计
- [docs/DECISIONS.md](docs/DECISIONS.md) — 技术决策（ADR）
- [docs/METRICS.md](docs/METRICS.md) — 准确率与性能指标
- [docs/journal/2026-10.md](docs/journal/2026-10.md) — 完成总结（冷记录）

## 维护

AI 审查流水线需要仓库 Secret `DEEPSEEK_API_KEY`；只审查代码 diff，不接触任何发票数据。

## 许可证

MIT，见 [LICENSE](LICENSE)。
