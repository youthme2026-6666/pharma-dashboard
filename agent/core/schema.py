"""统一数据格式（所有公司、所有适配器共用的约定）。

每家公司一个目录 data/<公司 id>/，统一格式的四个文件：
  income.csv     利润表：季度（Q1~Q4，3 个月）、半年（H1/H2，6 个月）、全年（FY，12 个月）各一行
  balance.csv    资产负债表：每个报告期末一行
  cashflow.csv   现金流量表：同 income 的期间
  segments.csv   收入构成（长表）：产品 product / 业务 segment / 地区 geography，每个期间每个成员一行
另有 events.json（重大事件，格式同礼来）和 meta.json（由框架生成）。

约定
- 金额单位一律为“百万”（报告币种，currency 列写明，例如 USD、CNY），每股收益为每股多少元，股数为百万股。
- fiscal_year / fiscal_period 用公司自己的财年口径（美敦力 2026 财年截至 2026 年 4 月）；period_end 为期末日期，横向比较按它对齐。
- basis：reported（报告原文直接给出）或 derived（推算，例如第四季度 = 全年 − 前三季度，下半年 = 全年 − 上半年，单季现金流 = 年初至今相减）。
- source：数据出处（文件网址或申报编号），方便追溯。
- 某项公司没有披露就留空，不填 0。
- 行的唯一键：income/cashflow 为 (fiscal_year, fiscal_period)；balance 为 period_end；segments 为 (fiscal_year, fiscal_period, axis, member)。
"""

PERIOD = ["fiscal_year", "fiscal_period", "period_start", "period_end", "months", "currency", "unit", "basis", "source"]
INCOME = ["revenue", "cost_of_sales", "gross_profit", "rnd", "sga", "acquired_iprd", "operating_income", "pretax_income",
          "income_tax", "net_income", "net_income_parent", "eps_diluted", "shares_diluted"]
BALANCE_PERIOD = ["fiscal_year", "fiscal_period", "period_end", "currency", "unit", "source"]
BALANCE = ["cash", "short_term_investments", "cash_and_sti", "receivables", "inventories", "total_current_assets", "total_assets",
           "total_current_liabilities", "total_debt", "total_liabilities", "total_equity", "equity_parent"]
CASHFLOW = ["cfo", "capex", "fcf", "dividends_paid", "buybacks"]
SEGMENT = ["fiscal_year", "fiscal_period", "period_end", "months", "axis", "member", "name_en", "name_zh", "group", "value",
           "currency", "unit", "basis", "source"]

FILES = {
    "income": PERIOD + INCOME,
    "balance": BALANCE_PERIOD + BALANCE,
    "cashflow": PERIOD + CASHFLOW,
    "segments": SEGMENT,
}
KEYS = {
    "income": ("fiscal_year", "fiscal_period"),
    "balance": ("period_end",),
    "cashflow": ("fiscal_year", "fiscal_period"),
    "segments": ("fiscal_year", "fiscal_period", "axis", "member"),
}
NUMERIC = set(INCOME + BALANCE + CASHFLOW + ["value", "months"])
FISCAL_PERIODS = {"Q1": 3, "Q2": 3, "Q3": 3, "Q4": 3, "H1": 6, "H2": 6, "FY": 12}
AXES = ("product", "segment", "geography")
BASES = ("reported", "derived")


def _num(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return "bad"


def problems(name, rows):
    """统一格式文件的结构与勾稽核对，返回问题列表（空表示通过）。"""
    out, seen = [], set()
    cols = FILES[name]
    for i, r in enumerate(rows):
        where = "%s 第 %d 行" % (name, i + 1)
        extra = set(r) - set(cols)
        if extra:
            out.append("%s：多出列 %s" % (where, sorted(extra)))
        for k in cols:
            if k in NUMERIC and _num(r.get(k)) == "bad":
                out.append("%s：%s 不是数字（%s）" % (where, k, r.get(k)))
        key = tuple(str(r.get(k)) for k in KEYS[name])
        if key in seen:
            out.append("%s：重复 %s" % (where, key))
        seen.add(key)
        if r.get("unit") != "millions":
            out.append("%s：单位应为 millions" % where)
        if not (isinstance(r.get("currency"), str) and len(r["currency"]) == 3):
            out.append("%s：缺少币种" % where)
        fp = r.get("fiscal_period")
        if fp not in FISCAL_PERIODS:
            out.append("%s：期间 %s 不认识" % (where, fp))
        elif name in ("income", "cashflow", "segments") and _num(r.get("months")) != FISCAL_PERIODS[fp]:
            out.append("%s：%s 应为 %d 个月" % (where, fp, FISCAL_PERIODS[fp]))
        if name in ("income", "cashflow", "segments") and r.get("basis") not in BASES:
            out.append("%s：basis 应为 reported/derived" % where)
        if name == "segments" and r.get("axis") not in AXES:
            out.append("%s：axis 应为 %s" % (where, "/".join(AXES)))
        n = {k: _num(r.get(k)) for k in cols if k in NUMERIC}
        if name == "income":
            rev, cos, gp = n.get("revenue"), n.get("cost_of_sales"), n.get("gross_profit")
            if None not in (rev, cos, gp) and abs(rev - cos - gp) > max(2, abs(rev) * 0.002):
                out.append("%s：毛利 ≠ 营收 − 营业成本" % where)
            pt, tax, ni = n.get("pretax_income"), n.get("income_tax"), n.get("net_income")
            if None not in (pt, tax, ni) and abs(pt - tax - ni) > max(2, abs(pt) * 0.002):
                out.append("%s：净利润 ≠ 税前利润 − 所得税" % where)
        if name == "balance":
            ta, tl, te = n.get("total_assets"), n.get("total_liabilities"), n.get("total_equity")
            if None not in (ta, tl, te) and abs(ta - tl - te) > max(2, abs(ta) * 0.001):
                out.append("%s：资产 ≠ 负债 + 权益" % where)
    if name == "income":
        out += _quarters_vs_year(rows)
    return out


def _quarters_vs_year(rows):
    """四个季度之和 ≈ 全年（营收、净利润）。"""
    out, by = [], {}
    for r in rows:
        by.setdefault(str(r.get("fiscal_year")), {})[r.get("fiscal_period")] = r
    for fy, ps in by.items():
        if "FY" in ps and all(q in ps for q in ("Q1", "Q2", "Q3", "Q4")):
            for k in ("revenue", "net_income"):
                vals = [_num(ps[q].get(k)) for q in ("Q1", "Q2", "Q3", "Q4")]
                tot = _num(ps["FY"].get(k))
                if None not in vals and tot is not None and abs(sum(vals) - tot) > max(3, abs(tot) * 0.001):
                    out.append("%s 年四个季度的 %s 之和（%.1f）与全年（%.1f）不符" % (fy, k, sum(vals), tot))
    return out
