"""多公司网站：首页（按方向分组的公司）、每家公司的页面（/c/<id>/）、统一格式数据下载（/data/<id>/）。

第 0 阶段的首页是静态生成的公司目录；横向比较表和通用公司页在第 2 阶段补上（见 agent/SPEC.md）。
礼来沿用现有的细页面（agent/pipeline/site.py），加上返回首页的链接和中英术语表。
"""
import html
import json
import os
import shutil

from ..pipeline.site import build_site as build_deep_page
from . import registry as RG
from .store import UnifiedStore

CURRENCY_YI = {"USD": "亿美元", "CNY": "亿元", "HKD": "亿港元", "EUR": "亿欧元", "GBP": "亿英镑", "DKK": "亿丹麦克朗", "CHF": "亿瑞士法郎", "JPY": "亿日元"}
STATUS = {"active": ("已上线", "Live"), "onboarding": ("接入中", "Onboarding"), "planned": ("待接入", "Planned"), "paused": ("暂停", "Paused")}


def load_glossary(root):
    with open(os.path.join(root, "agent", "glossary.json"), encoding="utf-8") as f:
        return json.load(f)


def _f(v):
    return None if v in (None, "") else float(v)


def summary(data_root, co):
    """首页卡片上的数字：最近一个季度（没有季度就用半年或全年）的营收、同比、毛利率、营业利润率，全部由统一格式数据算出。"""
    d = os.path.join(data_root, co["id"])
    if not os.path.exists(os.path.join(d, "income.csv")):
        return None
    rows = UnifiedStore(d).rows["income"]
    for months in (3, 6, 12):
        rs = sorted((r for r in rows if _f(r["months"]) == months and _f(r["revenue"]) is not None), key=lambda r: r["period_end"])
        if rs:
            break
    else:
        return None
    cur = rs[-1]
    prior = next((r for r in rs if r["fiscal_period"] == cur["fiscal_period"] and str(int(cur["fiscal_year"]) - 1) == str(r["fiscal_year"])), None)
    rev = _f(cur["revenue"])
    out = {"period": "%s %s" % (cur["fiscal_year"], cur["fiscal_period"]), "period_end": cur["period_end"], "currency": cur["currency"],
           "revenue": rev, "yoy": (rev / _f(prior["revenue"]) - 1) if prior and _f(prior["revenue"]) else None}
    gp, oi = _f(cur.get("gross_profit")), _f(cur.get("operating_income"))
    out["gross_margin"] = gp / rev if gp is not None and rev else None
    out["op_margin"] = oi / rev if oi is not None and rev else None
    return out


def _pct(x, signed=False):
    return "—" if x is None else (("%+.1f%%" if signed else "%.1f%%") % (x * 100))


def _card(co, info):
    e = html.escape
    st_zh, st_en = STATUS.get(co["status"], (co["status"], co["status"]))
    head = '<div class="hm-name">%s<span class="hm-en">%s</span></div><div class="hm-code">%s</div>' % (
        e(co["name_zh"]), e(co["name_en"]) if co["name_en"] != co["name_zh"] else "", e(RG.ticker(co)))
    badge = '<span class="hm-badge hm-%s">%s <span class="hm-en">%s</span></span>' % (e(co["status"]), st_zh, st_en)
    body = ""
    if info:
        unit = CURRENCY_YI.get(info["currency"], "亿 " + info["currency"])
        body = ('<dl class="hm-kpis">'
                '<div><dt>营收 <span class="hm-en">Revenue</span> · %s</dt><dd>%.1f <small>%s</small></dd></div>'
                '<div><dt>同比 <span class="hm-en">YoY</span></dt><dd class="%s">%s</dd></div>'
                '<div><dt>毛利率 <span class="hm-en">Gross margin</span></dt><dd>%s</dd></div>'
                '<div><dt>营业利润率 <span class="hm-en">Operating margin</span></dt><dd>%s</dd></div></dl>') % (
            e(info["period"]), info["revenue"] / 100, unit, "hm-up" if (info["yoy"] or 0) >= 0 else "hm-down",
            _pct(info["yoy"], True), _pct(info["gross_margin"]), _pct(info["op_margin"]))
    inner = head + badge + body
    if co["status"] == "active":
        return '<a class="hm-card hm-live" href="c/%s/">%s</a>' % (e(co["id"]), inner)
    return '<div class="hm-card">%s</div>' % inner


def render_home(root, reg, updated, data_root=None):
    e = html.escape
    cos = [c for c in reg["companies"] if c.get("status") != "removed"]
    live = [c for c in cos if c["status"] == "active"]
    data_root = data_root or os.path.join(root, "data")
    infos = {c["id"]: summary(data_root, c) for c in live}
    sections = []
    for cat in reg["categories"]:
        members = [c for c in cos if cat["id"] in c["categories"]]
        if not members:
            continue
        members.sort(key=lambda c: ({"active": 0, "onboarding": 1, "planned": 2, "paused": 3}.get(c["status"], 9), c["batch"], c["id"]))
        sections.append('<section class="hm-sec"><h2>%s <span class="hm-en">%s</span><span class="hm-count">%d</span></h2><div class="hm-grid">%s</div></section>' % (
            e(cat["zh"]), e(cat["en"]), len(members), "".join(_card(c, infos.get(c["id"])) for c in members)))
    with open(os.path.join(root, "agent", "web", "theme.css"), encoding="utf-8") as f:
        css = f.read()
    return HOME.replace("{{css}}", css + HOME_CSS).replace("{{sections}}", "\n".join(sections)).replace(
        "{{n}}", str(len(cos))).replace("{{live}}", str(len(live))).replace("{{updated}}", e(updated))


def build(root, out_dir, reg, updated, data_root=None):
    """生成整个网站；返回 {pages, bytes}。"""
    data_root = data_root or os.path.join(root, "data")
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)
    glossary = load_glossary(root)
    doc = render_home(root, reg, updated, data_root)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(doc)
    pages, total = ["index.html"], len(doc.encode("utf-8"))
    for co in reg["companies"]:
        if co.get("status") != "active":
            continue
        src = os.path.join(data_root, co["id"])
        if co["source"] == "lilly_deep":
            info = build_deep_page(root, os.path.join(out_dir, "c", co["id"]), updated=updated, data_dir=src, home="../../", glossary=glossary)
            pages.append("c/%s/index.html" % co["id"])
            total += info["bytes"]
        dl = os.path.join(out_dir, "data", co["id"])
        os.makedirs(dl, exist_ok=True)
        for fn in ("income.csv", "balance.csv", "cashflow.csv", "segments.csv", "events.json", "meta.json"):
            if os.path.exists(os.path.join(src, fn)):
                shutil.copy(os.path.join(src, fn), os.path.join(dl, fn))
    open(os.path.join(out_dir, ".nojekyll"), "w").close()
    return {"pages": pages, "bytes": total}


HOME_CSS = """
.hm-wrap { max-width: 84rem; margin: 0 auto; }
.hm-head h1 { font-size: var(--cds-font-size-title); font-weight: var(--cds-font-weight-medium); margin: 0 0 4px; }
.hm-head p { margin: 0 0 4px; color: var(--cds-text-secondary); }
.hm-en { color: var(--color-fg-muted); font-weight: 400; margin-left: 0.35em; font-size: 0.92em; }
.hm-sec { margin-top: var(--cds-gap-xl); }
.hm-sec h2 { font-size: var(--cds-font-size-heading); font-weight: var(--cds-font-weight-medium); margin: 0 0 var(--cds-gap-md); display: flex; align-items: baseline; gap: 2px; }
.hm-count { margin-left: auto; color: var(--color-fg-muted); font-weight: 400; font-size: var(--cds-font-size-caption); }
.hm-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(15rem, 1fr)); gap: var(--cds-gap-md); }
.hm-card { display: block; background: var(--color-panel); border: 1px solid var(--color-border-line); border-radius: var(--cds-radius); padding: var(--cds-pad-lg); color: inherit; text-decoration: none; min-width: 0; }
a.hm-card:hover { border-color: color-mix(in srgb, var(--color-fg) 35%, transparent); }
.hm-name { font-weight: var(--cds-font-weight-medium); overflow-wrap: anywhere; }
.hm-name .hm-en { display: block; margin-left: 0; font-size: var(--cds-font-size-caption); }
.hm-code { color: var(--color-fg-muted); font-size: var(--cds-font-size-caption); margin-top: 2px; font-variant-numeric: tabular-nums; }
.hm-badge { display: inline-block; margin-top: var(--cds-gap-sm); font-size: var(--cds-font-size-caption); padding: 1px 6px; border-radius: 999px; background: color-mix(in srgb, var(--color-fg) 6%, transparent); color: var(--cds-text-secondary); }
.hm-badge .hm-en { font-size: inherit; }
.hm-active { background: color-mix(in srgb, var(--color-ok) 14%, transparent); color: var(--color-ok); }
.hm-active .hm-en { color: inherit; opacity: 0.8; }
.hm-kpis { display: grid; grid-template-columns: 1fr 1fr; gap: var(--cds-gap-sm) var(--cds-gap-md); margin: var(--cds-gap-md) 0 0; }
.hm-kpis div { min-width: 0; }
.hm-kpis dt { color: var(--color-fg-muted); font-size: var(--cds-font-size-caption); }
.hm-kpis dt .hm-en { display: block; margin-left: 0; font-size: inherit; }
.hm-kpis dd { margin: 0; font-size: var(--cds-font-size-heading); font-weight: var(--cds-font-weight-medium); font-variant-numeric: tabular-nums; }
.hm-kpis small { font-size: var(--cds-font-size-caption); font-weight: 400; color: var(--color-fg-muted); }
.hm-up { color: var(--color-ok); }
.hm-down { color: var(--color-bad); }
"""

HOME = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>医药行业财务看板</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 16 16%27%3E%3Crect x=%271%27 y=%279%27 width=%273%27 height=%276%27 fill=%27%232a78d6%27/%3E%3Crect x=%276.5%27 y=%275%27 width=%273%27 height=%2710%27 fill=%27%231baf7a%27/%3E%3Crect x=%2712%27 y=%271%27 width=%273%27 height=%2714%27 fill=%27%23eb6834%27/%3E%3C/svg%3E">
<meta name="description" content="医药行业上市公司财务看板：头部药企、RNA、细胞与基因疗法、中国创新药、医疗器械、供应链与 AI，数据来自 SEC、港交所和巨潮资讯的官方文件，每天自动更新。">
<style>
{{css}}
</style>
</head>
<body>
<main class="site-main"><div class="hm-wrap">
<header class="hm-head">
<h1>医药行业财务看板 <span class="hm-en">Pharma Financial Dashboard</span></h1>
<p>覆盖 {{n}} 家公司，已上线 {{live}} 家，其余按批次接入。数据来自各公司向 SEC、港交所、上交所/深交所提交的官方文件，每天自动检查更新。</p>
<p>点击已上线的公司查看完整看板；指标均为最近一个报告期，“同比”与上年同期比较。</p>
</header>
{{sections}}
</div></main>
<footer class="site-foot">
<p>非官方看板，仅供参考，不构成投资建议。金额为各公司报告币种；亿 = 1 亿（100 百万）。</p>
<p>最近更新 {{updated}}。想加公司：在仓库的 <code>companies/watchlist.md</code> 里写上公司名，GPT 会自动核对并接入。</p>
</footer>
</body>
</html>
"""
