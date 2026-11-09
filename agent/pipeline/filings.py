"""找到某个季度的申报文件并解析：8-K（业绩新闻稿，Item 2.02 的 EX-99 附件）与 10-Q / 10-K（正文）。"""
import datetime as dt

from . import extract as X
from .datasets import quarter_end
from .tables import html_tables, html_text


def periodic_for(subs, q):
    """第 1~3 季度找 10-Q，第 4 季度找 10-K；报告期必须是该季度末。"""
    form = "10-K" if q.endswith("Q4") else "10-Q"
    pe = quarter_end(q)
    c = [f for f in subs if f["form"] == form and f["report_date"] == pe]
    return sorted(c, key=lambda f: f["filing_date"])[0] if c else None


def earnings_8ks(subs, q, window_days=75):
    """季度结束后 window_days 天内、包含 Item 2.02（经营结果）的 8-K，按日期排序。"""
    pe = dt.date.fromisoformat(quarter_end(q))
    out = []
    for f in subs:
        if f["form"] != "8-K" or "2.02" not in (f.get("items") or ""):
            continue
        fd = dt.date.fromisoformat(f["filing_date"])
        if pe < fd <= pe + dt.timedelta(days=window_days):
            out.append(f)
    return sorted(out, key=lambda f: f["filing_date"])


def load_8k(sec, cik, f, q):
    """在 8-K 的 EX-99 附件里找到本季度的业绩新闻稿并解析；找不到返回 None。"""
    docs = sec.documents(cik, f["accession"])
    ex = [d for d in docs if d["type"].upper().startswith("EX-99") and d["name"].lower().endswith((".htm", ".html"))]
    ex.sort(key=lambda d: (d["type"].upper() != "EX-99.1", d["type"]))
    for d in ex:
        html = sec.text(d["url"])
        tables = html_tables(html)
        t = X.find_8k_income(tables)
        if not t or X.quarter_of_table(t) != q:
            continue
        inc = X.parse_8k_income(t)
        rec = X.parse_8k_recons(X.find_8k_gross_recon(tables), X.find_8k_net_recon(tables), inc)
        sel = X.find_8k_selected(tables)
        return {"income": inc, "recons": rec, "selected": X.parse_8k_selected(sel) if sel else {},
                "text": html_text(html), "filing_date": f["filing_date"], "accession": f["accession"],
                "url": d["url"], "index": sec.index_url(cik, f["accession"]), "tables": tables}
    return None


def parse_periodic(tables, q):
    """从 10-Q / 10-K 的表格里解析出组装需要的全部内容，并记录缺了什么。"""
    n = int(q[5])
    out, missing = {"form": "10-K" if n == 4 else "10-Q"}, []
    bs_t, is_t, prv_t = X.find_balance_sheet(tables), X.find_income_statement(tables), X.find_prv(tables)
    if n < 4:
        p_t = X.find_products(tables, "three")
        y_t = X.find_products(tables, {2: "six", 3: "nine"}[n]) if n > 1 else None
    else:
        p_t, y_t = X.find_products(tables, "year"), None
    for name, t in (("资产负债表", bs_t), ("利润表", is_t), ("价格/销量拆分表", prv_t)):
        if not t:
            missing.append(name)
        elif X.quarter_of_table(t) != q:
            missing.append("%s（期间是 %s，不是 %s；表头：%s）" % (name, X.quarter_of_table(t), q, X.table_head(t)))
    if not p_t:
        missing.append("分产品收入表")
    if n in (2, 3) and not y_t:
        missing.append("年初至今分产品收入表")
    out["bs"] = X.parse_balance_sheet(bs_t) if bs_t else None
    out["income"] = X.parse_income_statement(is_t) if is_t else {}
    out["prv"] = X.parse_prv(prv_t, annual=(n == 4)) if prv_t else {}
    out["products"] = X.parse_products(p_t, annual=(n == 4)) if p_t else {"current": {}, "revenue": {}}
    out["products_ytd"] = X.parse_products(y_t) if y_t else None
    out["missing"] = missing
    return out


def load_periodic(sec, cik, f, q):
    url = sec.folder(cik, f["accession"]) + f["primary"]
    tables = html_tables(sec.text(url))
    out = parse_periodic(tables, q)
    out.update({"url": url, "filing_date": f["filing_date"], "accession": f["accession"], "tables": tables})
    return out
