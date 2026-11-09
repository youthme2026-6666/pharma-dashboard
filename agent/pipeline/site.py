"""生成独立网页版（GitHub Pages）：沿用 page/ 下同一份页面、计算和绘图代码，数据来自 data/。"""
import csv
import datetime as dt
import html
import json
import os
import re
import shutil

from .datasets import NAMES

D3_URL = "https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js"
FILES = {"income_statement": "利润表（季度）", "income_annual": "利润表（年度）", "balance_sheet": "资产负债表",
         "product_revenue": "分产品收入", "price_volume": "价格/汇率/销量（季度）", "price_volume_annual": "价格/汇率/销量（年度）",
         "events": "重大事件"}


def _cell(v):
    """与 Claude 看板读取数据的方式一致：CSV 单元格去掉首尾空格，空的变成 null；JSON 里的嵌套值转成字符串。"""
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        return v or None
    if isinstance(v, (int, float, bool)):
        return v
    return json.dumps(v, ensure_ascii=False)


def load_rows(data_dir):
    out = {}
    for n in NAMES:
        if n == "events":
            with open(os.path.join(data_dir, "events.json"), encoding="utf-8") as f:
                out[n] = [{k: _cell(v) for k, v in r.items()} for r in json.load(f)]
        else:
            with open(os.path.join(data_dir, n + ".csv"), encoding="utf-8", newline="") as f:
                out[n] = [{k: _cell(v) for k, v in r.items()} for r in csv.DictReader(f)]
    return out


def _wrap(code, name):
    # Claude 看板把每个脚本包在函数里执行；这里照做，两个文件里的顶层变量就不会互相冲突。
    return "(function () {\n" + code + "\n}).call(this);\n//# sourceURL=" + name + "\n"


def build_site(root, out_dir, updated=None, latest=None, data_dir=None):
    page_dir, web_dir = os.path.join(root, "page"), os.path.join(root, "agent", "web")
    data_dir = data_dir or os.path.join(root, "data")
    with open(os.path.join(page_dir, "index.html"), encoding="utf-8") as f:
        page = f.read()
    body = re.sub(r'<meta name="dash-one-writer"\s*/?>\s*', "", page)
    body = re.sub(r'\s*<script src="(calcs|app)\.js"></script>', "", body).strip()
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(os.path.join(out_dir, "data"))
    rows = load_rows(data_dir)
    latest = latest or max(r["quarter"] for r in rows["income_statement"])
    updated = updated or dt.date.today().isoformat()
    for n in NAMES:
        fn = n + (".json" if n == "events" else ".csv")
        shutil.copy(os.path.join(data_dir, fn), os.path.join(out_dir, "data", fn))
    links = " · ".join('<a href="data/%s">%s</a>' % (n + (".json" if n == "events" else ".csv"), html.escape(t)) for n, t in FILES.items())

    def read(path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def script(code):
        # 内联脚本里不能出现 "</script"，统一写成 "<\/script"
        return "<script>\n" + code.replace("</script", "<\\/script") + "\n</script>"

    data_js = "window.__DASH_DATA__ = " + json.dumps(rows, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/") + ";"
    parts = {
        "css": read(os.path.join(web_dir, "theme.css")),
        "body": body,
        "updated": html.escape(updated),
        "latest": html.escape(latest),
        "links": links,
        "d3": D3_URL,
        "scripts": "\n".join([
            script(read(os.path.join(web_dir, "dash-shim.js"))),
            script(data_js),
            script(_wrap(read(os.path.join(page_dir, "calcs.js")), "calcs.js")),
            script(_wrap(read(os.path.join(page_dir, "app.js")), "app.js")),
            script("dash.__start();"),
        ]),
    }
    doc = TEMPLATE
    for k, v in parts.items():
        doc = doc.replace("{{" + k + "}}", v)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(doc)
    open(os.path.join(out_dir, ".nojekyll"), "w").close()
    return {"files": sorted(os.listdir(out_dir)), "rows": {k: len(v) for k, v in rows.items()}, "bytes": len(doc.encode("utf-8"))}


TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>礼来财务看板</title>
<meta name="description" content="礼来（Eli Lilly）季度财务数据看板：营收、利润、产品、价格与销量、资产负债表和重大事件，每天自动检查更新。">
<style>
{{css}}
</style>
</head>
<body>
<div id="dash-errors" class="site-errors" hidden></div>
<main class="site-main">
{{body}}
</main>
<footer class="site-foot">
<p>非官方看板，仅供参考，不构成投资建议。数据来自礼来向美国证监会（SEC）提交的 10-Q、10-K 和 8-K 业绩新闻稿；2026 年第三季度及以前的细分产品和分地区明细来自礼来投资者关系网站的财务数据表。</p>
<p>数据每天自动检查，最近更新 {{updated}}，最新季度 {{latest}}。下载数据：{{links}}</p>
</footer>
<script src="{{d3}}"></script>
{{scripts}}
</body>
</html>
"""
