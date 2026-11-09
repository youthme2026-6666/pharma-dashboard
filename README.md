# 礼来财务看板

礼来（Eli Lilly）的季度财务看板：营收、利润、产品、价格与销量、资产负债表和重大事件。

- **自动更新版**：放进 GitHub 仓库后，每天由 GitHub Actions 自动检查 SEC 新财报、补新季度、用 GPT 更新重大事件，逐项核对通过后发布到 GitHub Pages。上线步骤见 `agent/README.md`，完整说明书见 `agent/SPEC.md`。
- **Claude 看板版**：https://claude.ai/artifact/YTFMjhHC46Xm6QFjV3m5XJ（默认私有）。它和自动更新版用同一份页面代码，是 2026Q3 数据的快照，不会自动更新；需要时可以让 Claude 把 `page/` 和 `data/` 同步过去。

## 目录

| 路径 | 内容 |
|---|---|
| `data/` | 7 份数据：季度/年度利润表、资产负债表、分产品收入、季度/年度价格·汇率·销量拆分（CSV，百万美元）和重大事件（JSON） |
| `page/` | 页面三个文件：`index.html`（结构与样式）、`calcs.js`（所有派生数字的计算）、`app.js`（所有图形与交互）；页面里不写死任何数字 |
| `agent/` | 自动维护程序、说明书、测试 |
| `.github/workflows/update.yml` | 每天运行的工作流 |
| `AGENTS.md` | 给编程助手的改代码规则 |
| `tools/extract_workbooks.js` | 当初从礼来 Financial Workbook 抽取 2021Q1~2026Q3 数据的浏览器脚本（留作记录） |

## 数据来源

- 2021Q1~2026Q3：礼来投资者关系网站的 Financial Workbook（利润表、分产品、价格/汇率/销量）和 10-Q/10-K（资产负债表），已逐项核对。
- 2026Q4 起：礼来向 SEC 提交的 10-Q、10-K 和 8-K 业绩新闻稿，由程序自动读取、核对。SEC 披露的产品明细比 Workbook 粗，具体差别见 `agent/SPEC.md` 第 12 节。
- 重大事件：礼来官网新闻稿（GPT 检索，规则核对）。

## 口径提醒

- 2023 年的分产品收入原表以美元计，抽取时已换算成百万美元。
- 礼来早年把心血管代谢领域称为 Diabetes，这里统一改称 Cardiometabolic Health。
- 某一季没有减值费用时，原表会省略这一行，抽取时用“营业利润 = 毛利 − 各项费用”验证后补 0。
