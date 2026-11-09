# 给编程助手的说明（AGENTS.md）

这个仓库是礼来财务看板：`page/` 是页面，`data/` 是数据，`agent/pipeline/` 是每天自动维护数据的程序。完整说明见 `agent/SPEC.md`，动手之前先读它。

## 常用命令（在仓库根目录运行）

```bash
python -m unittest discover -s agent/tests -t .   # 全部测试，不访问网络
python -m agent.pipeline.run check                 # 核对 data/
python -m agent.pipeline.run site                  # 生成网页 _site/index.html
python -m agent.pipeline.run backtest              # 用已核对的季度检验解析器（需要 SEC_USER_AGENT 和网络）
python -m agent.pipeline.run selftest              # 检查 OpenAI 和 SEC 是否可用
```

## 规则

1. 只用 Python 标准库；代码要兼容 Python 3.9。
2. 不要手工修改 `data/` 里 2026Q3 及以前的数据，它们是已核对的基准和回测的标准答案。新季度只能由流程写入。
3. 解析规则（`extract.py`）改动后，必须让测试和回测都通过。不要为了让核对通过而放宽容差；核对不过说明数据或规则有问题。
4. GPT 的输出必须经过 `verify.py` / `tasks.py` 的确定性核对才能写入数据；不要删掉这些核对。
5. 页面：样式只用设计变量；CSS 类以 `ly-` 开头；静态文字写在 `index.html`，脚本只在 `data-draw` 元素里写内容；不写死任何财务数字，阈值用常量并在说明里由脚本画出。
6. 不要把任何密钥、邮箱写进代码或提交记录；它们放在 GitHub Secrets。
7. 处理 `dashboard-bot` issue 时：先看报告里“需要处理的问题”，对照 SPEC 第 10 节；修好后在 issue 里说明原因和改动，并附上测试和回测结果。
