# 礼来看板自动维护：上线步骤

整套东西放进一个 GitHub 仓库后，每天由 GitHub Actions 自动运行：检查 SEC 新财报、补新季度、用 GPT 更新重大事件、核对、发布网页。上线只需要做一次下面 5 步。每一部分做什么、数据怎么来、怎么核对，见同目录的 `SPEC.md`。

## 1. 建仓库并上传

在 GitHub 新建一个空仓库（公开仓库可以免费用 GitHub Pages；数据全部来自公开的 SEC 文件），然后在终端里把 `lilly-dashboard` 文件夹推上去（网页拖拽上传容易漏掉隐藏的 `.github` 文件夹，工作流就不会运行）：

```bash
cd ~/Desktop/yaochen/lilly-dashboard
git init -b main
git add .
git commit -m "礼来财务看板"
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

## 2. 设置两个密钥

仓库 → Settings → Secrets and variables → Actions：

| 名称 | 类型 | 填什么 |
|---|---|---|
| `SEC_USER_AGENT` | Secret | SEC 要求每个程序写明身份和联系邮箱，例如 `Lilly Dashboard yourname@example.com`。不写或写假的会被 SEC 拒绝 |
| `OPENAI_API_KEY` | Secret | OpenAI 的 API 密钥（platform.openai.com → API keys） |
| `OPENAI_MODEL` | Variable（可选） | 指定模型，例如 `gpt-5.1`。不设置时程序从你账号可用的模型里自动挑 |

## 3. 打开 GitHub Pages

Settings → Pages → Build and deployment → Source 选 **GitHub Actions**。

## 4. 第一次手动运行

Actions → “礼来看板自动更新” → Run workflow：

1. 先选 `selftest`：检查 OpenAI 密钥、模型、联网搜索和 SEC 访问是否正常（结果在运行日志里）。
2. 再选 `backtest`：用 SEC 文件重新解析 2025Q4~2026Q3，结果必须和看板现有数据一致，确认解析器在真实 SEC 文件上没问题。
3. 最后选 `auto`：正式跑一遍，生成并发布网页。网页地址显示在运行结果的 deploy 一步里，一般是 `https://<你的用户名>.github.io/<仓库名>/`。

## 5. 之后

不用管。每天 13:23 UTC 自动运行一次；礼来发布新季度财报（8-K 和 10-Q/10-K 都提交后）的当天或第二天，看板会自动多出这个季度。每次运行的报告在 Actions 的运行页面里。

出问题时（例如 SEC 文件格式变了、核对没通过），会自动开一个带 `dashboard-bot` 标签的 issue，正文写明原因；问题不会写进数据，网页继续显示最后一次通过核对的数据。处理方法见 `SPEC.md` 第 10 节。也可以把 issue 交给编程助手（例如 Codex），它会按根目录的 `AGENTS.md` 修改并跑测试。

## 在自己电脑上运行（可选）

需要 Python 3.9 以上，不需要安装任何包。在仓库根目录：

```bash
export SEC_USER_AGENT="Lilly Dashboard yourname@example.com"
export OPENAI_API_KEY="sk-..."
python3 -m unittest discover -s agent/tests -t .
python3 -m agent.pipeline.run backtest
python3 -m agent.pipeline.run auto
```

生成的网页在 `_site/index.html`，直接用浏览器打开即可。

## 费用

GitHub Actions 和 Pages 对公开仓库免费。OpenAI：OpenAI 的 API 和 ChatGPT 订阅分开计费，需要在 platform.openai.com 单独充值。实测每 3 天一次事件检索约 5~10 美分，加上每季度几次短调用，每月约 1 美元（取决于模型），每次的用量写在运行报告里。
