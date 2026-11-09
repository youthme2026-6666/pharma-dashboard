# 给编程助手的说明（AGENTS.md）

这个仓库是医药行业财务看板（多公司）：`companies/` 是公司清单，`data/<公司>/` 是数据，`agent/core/` 是多公司框架，`agent/adapters/` 是各数据来源的适配器，`agent/pipeline/` 是礼来细数据流程，`page/` 是礼来细页面。完整说明见 `agent/SPEC.md`（第 0 部分是框架），动手之前先读它。

## 常用命令（在仓库根目录运行）

```bash
python -m unittest discover -s agent/tests -t .      # 全部测试，不访问网络
python -m agent.core.run check                        # 核对公司清单和每家公司的数据
python -m agent.core.run site                         # 生成网站 _site/
python -m agent.core.run backtest --company lly       # 回测解析器（需要 SEC_USER_AGENT 和网络）
python -m agent.core.run selftest                     # 检查 OpenAI、SEC、港交所、巨潮能否访问
python -m agent.core.run auto --company <id>          # 只更新一家公司
```

## 规则

1. 代码兼容 Python 3.9，只用标准库；唯一例外是港股、A 股适配器可以用 `pypdf` 读 PDF 文字（工作流里安装，导入放在函数里，别的模块不依赖它）。
2. 不要手工修改 `data/` 里已核对的数据（礼来 2026Q3 及以前是回测的标准答案）。新数据只能由流程写入。
3. 适配器遵守 `agent/adapters/base.py` 的接口和 `agent/core/schema.py` 的统一格式：只返回行和核对结果，不直接写统一格式文件；金额单位百万、注明币种和出处、推算的数标 `derived`；没披露的留空。
4. 解析规则改动后，测试和回测都必须通过。不要为了让核对通过而放宽容差；核对不过说明数据或规则有问题。容差只能按可解释的原因设定（例如按参与计算的四舍五入项数），并写明理由。
5. GPT 的输出必须经过确定性核对（`agent/pipeline/verify.py`、`tasks.py` 和各适配器的核对）才能写入；不要删掉这些核对。
6. 公司清单 `companies/registry.json` 的格式规则在 `agent/core/registry.py`；代码、代码号、orgId 要能在官方清单（SEC、港交所、巨潮）里查到。
7. 页面：样式只用设计变量；礼来页面的 CSS 类以 `ly-` 开头，首页以 `hm-` 开头；静态文字写在 HTML 里，脚本只在 `data-draw` 元素里写内容；不写死任何财务数字。所有指标、报表行、图例都要中英对照，英文取自 `agent/glossary.json`（新术语先加进术语表）。
8. 一家公司出问题不能影响其他公司：适配器里的网络、GPT 偶发失败只记提醒；可能写错数据时才算“必过”项失败。
9. 不要把任何密钥写进代码或提交记录；它们放在 GitHub Secrets。提交作者用 GitHub 的匿名邮箱。
10. 处理 `dashboard-bot` issue 时：先看报告里“需要处理的问题”和是哪家公司，对照 SPEC 第 10 节；修好后在 issue 里说明原因和改动，并附上测试和回测结果。
