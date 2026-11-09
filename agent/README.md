# 医药行业财务看板：上线与运行

整套东西放在 GitHub 仓库 `youthme2026-6666/pharma-dashboard` 里，每天由 GitHub Actions 自动运行：逐家公司检查新报告、补新季度、用 GPT 更新重大事件、核对、发布网页。网址：https://youthme2026-6666.github.io/pharma-dashboard/ 。每一部分做什么、数据怎么来、怎么核对，见同目录的 `SPEC.md`。

## 已经做好的设置

| 项目 | 位置 | 内容 |
|---|---|---|
| `SEC_USER_AGENT` | Settings → Secrets and variables → Actions → Secrets | SEC 要求每个程序写明身份和联系邮箱，例如 `Lilly Dashboard name@example.com`；访问港交所、巨潮时也用它表明身份 |
| `OPENAI_API_KEY` | 同上 | OpenAI 的 API 密钥（platform.openai.com → API keys；和 ChatGPT 订阅分开计费） |
| `OPENAI_MODEL` | Variables（可选） | 指定模型，例如 `gpt-5.1`。不设置时按 `config.json` 的偏好挑，偏好的都下线了就自动用最新的通用模型 |
| GitHub Pages | Settings → Pages | Source 为 GitHub Actions |

换 OpenAI 密钥：在 Secrets 里把 `OPENAI_API_KEY` 改成新值即可，不用改代码。

## 手动运行

Actions → “医药行业财务看板自动更新” → Run workflow，选模式；“公司”一栏留空为全部，也可以填一个或几个公司 id（见 `companies/registry.json`，空格分隔）：

- `selftest`：检查 OpenAI、SEC、港交所、巨潮能否访问。
- `backtest`：用已核对的期间检验解析器。
- `auto`：正式更新并发布网页（每天定时运行的就是它）。
- `events`：强制检索一次重大事件。
- `check` / `site`：只核对数据 / 只生成网页。

## 平时

不用管。每天 13:23 UTC（北京时间 21:23）自动运行一次；公司发布新报告的当天或第二天，看板会自动加上新的期间。每次运行的报告在 Actions 的运行页面里。

出问题时（例如某家公司的文件格式变了、核对没通过），这家公司本次不写入，其他公司照常更新，并自动开一个带 `dashboard-bot` 标签的 issue，正文写明是哪家公司、什么原因；网页继续显示最后一次通过核对的数据。处理方法见 `SPEC.md` 第 10 节；也可以把 issue 交给编程助手（例如 Codex），它会按根目录的 `AGENTS.md` 修改并跑测试。

想加公司：在 `companies/watchlist.md` 的“待加”下面写一行公司名或代码，提交即可（GPT 自动接入在第 4 阶段上线）。

## 在自己电脑上运行（可选）

需要 Python 3.9 以上；港股、A 股适配器还需要 `pip install pypdf`。在仓库根目录：

```bash
export SEC_USER_AGENT="Lilly Dashboard name@example.com"
export OPENAI_API_KEY="sk-..."
python3 -m unittest discover -s agent/tests -t .
python3 -m agent.core.run backtest
python3 -m agent.core.run auto
```

生成的网站在 `_site/`，首页是 `_site/index.html`。

## 费用

GitHub Actions 和 Pages 对公开仓库免费。OpenAI：每家公司每两周检索一次重大事件（每次约 5~10 美分），加上新报告时的少量调用；全部约 70 家公司时每月约 10 美元。`config.json` 里设了每月上限 15 美元，超过后当月暂停事件检索，财务数据照常更新。每次运行的用量和本月累计写在运行报告里。
