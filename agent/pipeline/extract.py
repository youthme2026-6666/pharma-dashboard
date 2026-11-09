"""从申报文件的表格里认出并解析：利润表、资产负债表、分产品收入、价格/汇率/销量拆分，
以及 8-K 业绩新闻稿里的利润表和非 GAAP 调节。规则与看板现有数据的整理口径一致（见 ../SPEC.md）。

所有金额单位为百万美元，比率为小数（0.1 = 10%）。
"""
import re

from .tables import drop_footnote, norm_label, values

AREAS = ["Cardiometabolic Health", "Oncology", "Immunology", "Neuroscience", "Other"]
AREA_ALIASES = {
    "cardiometabolic health": "Cardiometabolic Health",
    "diabetes": "Cardiometabolic Health",
    "diabetes and obesity": "Cardiometabolic Health",
    "oncology": "Oncology",
    "immunology": "Immunology",
    "neuroscience": "Neuroscience",
    "other": "Other",
}


def _is_pct(c):
    from .tables import is_pct_or_nm
    return is_pct_or_nm(c)


def _rx(p):
    return re.compile(p, re.I)


def _has(table, pattern):
    rx = _rx(pattern)
    return any(rx.search(" ".join(r)) for r in table)


def _first(table, pattern):
    rx = _rx(pattern)
    return any(r and rx.search(norm_label(r[0])) for r in table)


def _head(table, n=3):
    return " ".join(" ".join(r) for r in table[:n])


# ---------------- 找表 ----------------
def find_balance_sheet(tables):
    for t in tables:
        if _first(t, r"^Total assets$") and _first(t, r"^Total current liabilities$"):
            return t
    return None


def find_income_statement(tables):
    """10-Q / 10-K 的合并利润表（作为 8-K 缺失时的后备）。"""
    for t in tables:
        if _first(t, r"^Revenue$") and _first(t, r"^Cost of sales") and _first(t, r"^Net income") and _first(t, r"^Income (\(loss\) )?before income taxes"):
            if not _has(t, r"% Chg"):
                return t
    return None


def find_products(tables, period):
    """分产品收入附注表。period: 'three' / 'six' / 'nine'（10-Q）或 'year'（10-K）。"""
    for t in tables:
        if not (_first(t, r"^Mounjaro|^Trulicity|^Humalog") and _first(t, r"^Total (cardiometabolic health|diabetes)")):
            continue
        if _has(t, r"Percent Change"):
            continue
        head = _head(t)
        if period == "year":
            if re.search(r"U\.S\..*Outside U\.S\.", head) and not re.search(r"Months Ended", head, re.I):
                return t
        elif re.search(period + r" Months Ended", head, re.I):
            return t
    return None


def find_prv(tables):
    for t in tables:
        if _first(t, r"^Price$") and _first(t, r"^Volume$") and _first(t, r"^Percent change$"):
            return t
    return None


def find_8k_income(tables):
    for t in tables:
        if _first(t, r"^Revenue$") and _first(t, r"^Net income") and _has(t, r"% Chg"):
            return t
    return None


def find_8k_gross_recon(tables):
    for t in tables:
        if _first(t, r"^Gross Margin - As Reported$") and _first(t, r"^Gross Margin - Non-GAAP$"):
            return t
    return None


def find_8k_net_recon(tables):
    for t in tables:
        if _first(t, r"^Net income - Reported$") and _first(t, r"Earnings per share \(diluted\) - Non-GAAP"):
            return t
    return None


def find_8k_selected(tables):
    for t in tables:
        if _has(t, r"^Selected Products"):
            return t
    return None


# ---------------- 解析 ----------------
_IS_8K = [
    ("revenue", r"^Revenue$"),
    ("cost_of_sales", r"^Cost of sales$"),
    ("rnd", r"^Research and development$"),
    ("msa", r"^Marketing, selling,? and administrative$"),
    ("acquired_iprd", r"^Acquired (IPR&D|in-process research)"),
    ("impairment_restructuring", r"^Asset impairment, restructuring"),
    ("operating_income", r"^Operating income"),
    ("other_net", r"^Other income \(expense\)$"),
    ("pretax_income", r"^Income (\(loss\) )?before income taxes"),
    ("income_tax", r"^Income tax"),
    ("net_income", r"^Net income( \(loss\))?$"),
    ("eps_diluted", r"^Earnings( \(loss\))? per share\s*-\s*diluted"),
    ("diluted_shares_k", r"^Weighted-average shares outstanding"),
]


def parse_8k_income(table):
    """返回 {字段: [本季, 去年同季, 年初至今, 去年年初至今]}（第一季度只有前两列）。"""
    out = {}
    for r in table:
        if not r:
            continue
        lab = norm_label(r[0])
        for key, pat in _IS_8K:
            if key not in out and re.search(pat, lab, re.I):
                v = values(r, drop_pct=True)
                if v:
                    out[key] = v
    if "revenue" in out and "cost_of_sales" in out:
        out["gross_margin"] = [a - b for a, b in zip(out["revenue"], out["cost_of_sales"])]
    return out


def _items_between(table, start_pat, end_pat):
    """调节表里“报告值”和“非 GAAP 值”之间的各个调整项。标签折行（下一行以小写或括号开头且没有数字）会拼回上一项。"""
    items, on, last = [], False, None
    for r in table or []:
        if not r:
            continue
        lab = norm_label(r[0])
        if re.search(start_pat, lab, re.I):
            on, last = True, None
            continue
        if not on:
            continue
        if re.search(end_pat, lab, re.I):
            break
        v = values(r, drop_pct=True)
        if v:
            last = [lab, v]
            items.append(last)
        elif last is not None and (lab[:1].islower() or lab.startswith("(")):
            last[0] = last[0] + " " + lab
        else:
            last = None
    return [(a, b) for a, b in items]


def item_level(label):
    """调整项落在利润表哪一层：'tax'（所得税）、'other'（其他收入/费用）或 'operating'（营业利润以上）。"""
    low = label.lower()
    if "income tax" in low or "tax effect" in low or "tax law" in low:
        return "tax"
    if "other income" in low:
        return "other"
    return "operating"


def parse_8k_recons(gross_table, net_table, income=None):
    """非 GAAP 调节。income 为 parse_8k_income 的结果时，顺带算出非 GAAP 营业利润 = 营业利润 + 营业层面的调整项。"""
    out = {}
    for table, rows in ((gross_table, [("gross_margin_reported", r"^Gross Margin - As Reported$"), ("ng_gross_margin", r"^Gross Margin - Non-GAAP$")]),
                        (net_table, [("net_income_reported", r"^Net income - Reported$"), ("ng_net_income", r"^Net income - Non-GAAP$"),
                                     ("eps_reported", r"^Earnings per share \(diluted\) - Reported$"), ("ng_eps_diluted", r"^Earnings per share \(diluted\) - Non-GAAP$")])):
        if not table:
            continue
        for r in table:
            if not r:
                continue
            lab = norm_label(r[0])
            for key, pat in rows:
                if key not in out and re.search(pat, lab, re.I):
                    v = values(r, drop_pct=True)
                    if v:
                        out[key] = v
    items = _items_between(net_table, r"^Net income - Reported$", r"^Net income - Non-GAAP$")
    out["excluded_items"] = [{"label": a, "level": item_level(a), "values": b} for a, b in items]
    n = len(out.get("ng_net_income") or [])
    if n:
        adj = [sum(b[i] for a, b in items if item_level(a) == "operating" and i < len(b)) for i in range(n)]
        out["ng_operating_adj"] = adj
        total = [sum(b[i] for a, b in items if i < len(b)) for i in range(n)]
        rep = out.get("net_income_reported") or []
        # 自检：报告净利润 + 全部调整项 = 非 GAAP 净利润（允许四舍五入误差 2 百万）
        out["recon_ok"] = len(rep) == n and all(abs(rep[i] + total[i] - out["ng_net_income"][i]) <= 2 for i in range(n))
        oi = (income or {}).get("operating_income")
        if oi:
            out["ng_operating_income"] = [oi[i] + adj[i] for i in range(min(len(oi), n))]
    return out


_MONTH_Q = {"march": 1, "june": 2, "september": 3, "december": 4}


def quarter_of_table(table, rows=4):
    """从表头（如 'Three Months Ended September 30,' + '2026'）认出对应的季度，返回 '2026Q3'。"""
    head = " ".join(" ".join(r) for r in table[:rows])
    m = re.search(r"(March|June|September|December)", head, re.I)
    y = re.search(r"\b(20\d{2})\b", head)
    if m and y:
        return "%sQ%d" % (y.group(1), _MONTH_Q[m.group(1).lower()])
    # 10-K 网页版里全年的价格/销量拆分是单独一张表，表头只有“2025 vs. 2024”，没有月份：全年对应第四季度
    vs = re.search(r"\b(20\d{2})\s+vs\.?\s+20\d{2}\b", head, re.I)
    return "%sQ4" % vs.group(1) if vs else None


def table_head(table, rows=2, width=120):
    """表头前几行，用在“没认出”的报错里，方便对照 SEC 原文修规则。"""
    return " / ".join(" | ".join(r) for r in table[:rows] if r)[:width]


_IS_10Q = [
    ("revenue", r"^Revenue$"),
    ("cost_of_sales", r"^Cost of sales$"),
    ("rnd", r"^Research and development$"),
    ("msa", r"^Marketing, selling,? and administrative$"),
    ("acquired_iprd", r"^Acquired (IPR&D|in-process research)"),
    ("impairment_restructuring", r"^Asset impairment, restructuring"),
    ("other_expense", r"^Other\s*[-–—]+\s*net"),
    ("pretax_income", r"^Income (\(loss\) )?before income taxes"),
    ("income_tax", r"^Income taxes"),
    ("net_income", r"^Net income( \(loss\))?$"),
]


def parse_income_statement(table):
    """10-Q/10-K 利润表：返回 {字段: [各列]}；10-Q 列为 [本季, 去年同季, 年初至今, 去年年初至今]，10-K 为 [今年, 去年, 前年]。"""
    out, section = {}, None
    for r in table:
        if not r:
            continue
        lab = norm_label(r[0])
        if re.search(r"^Earnings( \(loss\))? per share", lab, re.I):
            section = "eps"
        elif re.search(r"^Shares used", lab, re.I):
            section = "shares"
        v = values(r)
        if not v:
            continue
        if lab.lower() == "diluted" and section == "eps" and "eps_diluted" not in out:
            out["eps_diluted"] = v
        elif lab.lower() == "diluted" and section == "shares" and "diluted_shares_k" not in out:
            out["diluted_shares_k"] = [round(x * 1000) for x in v]
        for key, pat in _IS_10Q:
            if key not in out and re.search(pat, lab, re.I):
                out[key] = v
    if "other_expense" in out:
        out["other_net"] = [-x for x in out.pop("other_expense")]
    if "revenue" in out and "cost_of_sales" in out:
        out["gross_margin"] = [a - b for a, b in zip(out["revenue"], out["cost_of_sales"])]
        need = ["rnd", "msa", "acquired_iprd", "impairment_restructuring"]
        if all(k in out for k in need):
            out["operating_income"] = [g - sum(out[k][i] for k in need) for i, g in enumerate(out["gross_margin"])]
    return out


_BS = [
    ("cash", r"^cash and cash equivalents$"), ("sti", r"^short-term investments$"), ("ar", r"^accounts receivable"),
    ("inventories", r"^inventories$"), ("tca", r"^total current assets$"), ("investments", r"^investments$"),
    ("goodwill", r"^goodwill$"), ("intangibles", r"^other intangibles"), ("ppe", r"^property and equipment"),
    ("ta", r"^total assets$"), ("st_debt", r"^short-term borrowings"), ("ap", r"^accounts payable$"),
    ("rebates", r"^sales rebates and discounts$"), ("tcl", r"^total current liabilities$"), ("ltd", r"^long-term debt$"),
    ("tncl", r"^total (noncurrent|other) liabilities$"), ("lilly_eq", r"^total eli lilly and company shareholders"),
    ("nci", r"^noncontrolling interests$"), ("te", r"^total equity$"), ("tle", r"^total liabilities and equity$"),
]


def parse_balance_sheet(table):
    """取每行第一个数字（当期期末），归入固定的几大类；“其他”类由合计数减去已列项目得出。"""
    raw = {}
    for r in table:
        if not r:
            continue
        lab = norm_label(r[0]).lower()
        v = values(r)
        if not v:
            continue
        for key, pat in _BS:
            if key not in raw and re.search(pat, lab):
                raw[key] = v[0]
    z = lambda k: raw.get(k) or 0.0
    te = raw["te"] if "te" in raw else z("lilly_eq") + z("nci")
    cash_sti, gi = z("cash") + z("sti"), z("goodwill") + z("intangibles")
    r1 = lambda x: round(x, 1)
    return {
        "cash_and_sti": r1(cash_sti), "accounts_receivable": r1(z("ar")), "inventories": r1(z("inventories")),
        "other_current_assets": r1(z("tca") - cash_sti - z("ar") - z("inventories")), "total_current_assets": r1(z("tca")),
        "investments": r1(z("investments")), "goodwill_intangibles": r1(gi), "ppe": r1(z("ppe")),
        "other_noncurrent_assets": r1(z("ta") - z("tca") - z("investments") - gi - z("ppe")), "total_assets": r1(z("ta")),
        "short_term_debt": r1(z("st_debt")), "accounts_payable": r1(z("ap")), "sales_rebates": r1(z("rebates")),
        "other_current_liabilities": r1(z("tcl") - z("st_debt") - z("ap") - z("rebates")), "total_current_liabilities": r1(z("tcl")),
        "long_term_debt": r1(z("ltd")), "other_noncurrent_liabilities": r1(z("tncl") - z("ltd")),
        "total_liabilities": r1(z("tcl") + z("tncl")), "total_equity": r1(te), "total_liabilities_equity": r1(z("tle")),
        "_raw": raw,
    }


def _norm_product(label):
    s = norm_label(label)
    low = s.lower()
    m = re.match(r"^other (cardiometabolic health|diabetes|oncology|immunology|neuroscience)$", low)
    if m:
        return "Other " + AREA_ALIASES[m.group(1)]
    return s


def parse_products(table, annual=False):
    """返回 {'current': {(领域, 产品): (美国, 美国以外, 合计)}, 'prior': {...}, 'areas': {...}, 'revenue': {...}}。

    10-Q 每行 6 个数：本期 美国/美国以外/合计，上年同期 美国/美国以外/合计。
    10-K 每行 6 个数：美国 今年/去年/前年，美国以外 今年/去年/前年（合计 = 两者相加）。
    """
    cur, pri, areas_c, areas_p, rev = {}, {}, {}, {}, {}
    area = None
    for r in table:
        if not r:
            continue
        r = drop_footnote(r, 6)
        lab = norm_label(r[0])
        low = lab.lower().rstrip(":")
        v = values(r)
        if not v:
            if lab.endswith(":") and low in AREA_ALIASES:
                area = AREA_ALIASES[low]
            continue
        if annual:
            if len(v) < 6:
                continue
            c, p = (v[0], v[3], v[0] + v[3]), (v[1], v[4], v[1] + v[4])
        else:
            if len(v) < 6:
                continue
            c, p = (v[0], v[1], v[2]), (v[3], v[4], v[5])
        tm = re.match(r"^total (.+)$", low)
        if low == "revenue":
            rev = {"current": c, "prior": p}
            area = None
        elif tm and tm.group(1) in AREA_ALIASES:
            a = AREA_ALIASES[tm.group(1)]
            areas_c[a], areas_p[a] = c, p
            area = None
        elif area is None and low in AREA_ALIASES:
            a = AREA_ALIASES[low]
            areas_c[a], areas_p[a] = c, p
            cur[(a, a)], pri[(a, a)] = c, p
        elif area is not None:
            cur[(area, _norm_product(lab))], pri[(area, _norm_product(lab))] = c, p
    return {"current": cur, "prior": pri, "areas": areas_c, "areas_prior": areas_p, "revenue": rev}


_REGIONS = {"u.s.": "U.S.", "outside u.s.": "Outside U.S.", "consolidated": "Total Revenue"}
_PRV_ROWS = {"volume": "volume", "price": "price", "foreign exchange rates": "rate", "percent change": "total_change"}


def parse_prv(table, annual=False):
    """返回 {'period': {地区: {volume, price, rate, total_change}}, 'ytd': {...}, 'revenue': {地区: 金额}}。

    10-Q：每行 6 个数（本季 美国/美国以外/合并，年初至今 同样三列）；第一季度只有 3 个。
    10-K：前半段是各地区收入（今年/去年/增幅），后半段每行 3 个数（全年）。
    """
    period, ytd, revenue = {}, {}, {}
    for r in table:
        if not r:
            continue
        low = norm_label(r[0]).lower()
        v = values(r)
        if low in _REGIONS and v and annual:
            revenue[_REGIONS[low]] = v[0]
            continue
        if low == "revenue" and v and annual:
            revenue["Total Revenue"] = v[0]
            continue
        if low not in _PRV_ROWS or not v:
            continue
        key = _PRV_ROWS[low]
        regions = ["U.S.", "Outside U.S.", "Total Revenue"]
        if annual:
            for i, g in enumerate(regions[: len(v)]):
                ytd.setdefault(g, {})[key] = round(v[i] / 100, 4)
        else:
            for i, g in enumerate(regions):
                if i < len(v):
                    period.setdefault(g, {})[key] = round(v[i] / 100, 4)
                if i + 3 < len(v):
                    ytd.setdefault(g, {})[key] = round(v[i + 3] / 100, 4)
    if not annual and not ytd:
        ytd = {g: dict(d) for g, d in period.items()}
    return {"period": period, "ytd": ytd, "revenue": revenue}


def parse_8k_selected(table):
    """新闻稿里的“重点产品”：{产品: [本季, 去年同季, 年初至今, 去年年初至今]}。"""
    out = {}
    for r in table:
        if not r:
            continue
        lab = norm_label(r[0])
        if re.search(r"^(Selected Products|Total Revenue|\(Dollars)", lab, re.I):
            continue
        r = [r[0]] + [c for c in r[1:] if not _is_pct(c)]
        r = drop_footnote(r, (2, 4))
        lab = norm_label(r[0])
        v = values(r, drop_pct=True)
        if v:
            out[lab] = v
    return out
