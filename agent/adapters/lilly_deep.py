"""礼来：沿用已验证的细数据流程（agent/pipeline，见 agent/SPEC.md），再换算成统一格式供首页和横向比较。

细数据（7 份文件：利润表、年度利润表、资产负债表、分产品、价格/汇率/销量、重大事件）照旧放在 data/lly/，
礼来自己的页面继续用它们；统一格式的 income/balance/segments 由这里换算得到（礼来细数据里没有现金流量表，cashflow 先留空）。
"""
import argparse

from ..pipeline import run as R
from ..pipeline.datasets import QUARTER_END, Store
from ..pipeline.sec import SecError
from .base import Result

SOURCE = "礼来投资者关系财务数据表（2026Q3 及以前）与 SEC 10-Q/10-K/8-K（之后）"
_START = {1: "01-01", 2: "04-01", 3: "07-01", 4: "10-01"}


def _bind(company, ctx):
    R.DATA = ctx.data_dir(company["id"])
    R.STATE_DIR = ctx.state_dir(company["id"])


def _absorb(res, rep):
    """把细数据流程的报告（agent/pipeline/run.Report）并进统一结果。"""
    for m in rep.fatal:
        res.error(m)
    res.warnings += rep.warn
    res.notes += [(t, lines) for t, lines in rep.sections]
    res.changed += list(rep.changed)


def update(company, ctx):
    _bind(company, ctx)
    rep = R.Report("auto")
    args = argparse.Namespace(today=ctx.today.isoformat(), events=ctx.force_events, quarters=[])
    R.auto(args, ctx.cfg, rep, site=False, gpt=ctx.gpt, allow_events=ctx.gpt_ok("events"))
    res = Result(company["id"])
    _absorb(res, rep)
    if res.ok:
        res.rows, res.replace = to_unified(R.DATA, company.get("currency", "USD")), True
    return res


def backtest(company, ctx):
    _bind(company, ctx)
    rep, res = R.Report("backtest"), Result(company["id"])
    try:
        sec = R.make_sec(ctx.cfg)
        cik = ctx.cfg["company"]["cik"]
        ok = R.run_backtest(sec, cik, sec.submissions(cik, include_older=True), Store(R.DATA), ctx.cfg, rep)
    except SecError as e:
        res.error("无法访问 SEC：%s" % e)
        return res
    if not ok:
        rep.error("解析器回测没通过")
    _absorb(res, rep)
    return res


def check(company, ctx):
    _bind(company, ctx)
    return R.check_data(Store(R.DATA), ctx.cfg)


def _f(v):
    return None if v in (None, "") else float(v)


def _income_row(r, fy, fp, start, end, months, currency):
    shares = _f(r.get("diluted_shares_k"))
    return {"fiscal_year": fy, "fiscal_period": fp, "period_start": start, "period_end": end, "months": months,
            "currency": currency, "unit": "millions", "basis": "reported", "source": SOURCE,
            "revenue": _f(r["revenue"]), "cost_of_sales": _f(r["cost_of_sales"]), "gross_profit": _f(r["gross_margin"]),
            "rnd": _f(r["rnd"]), "sga": _f(r["msa"]), "acquired_iprd": _f(r["acquired_iprd"]),
            "operating_income": _f(r["operating_income"]), "pretax_income": _f(r["pretax_income"]),
            "income_tax": _f(r["income_tax"]), "net_income": _f(r["net_income"]), "net_income_parent": None,
            "eps_diluted": _f(r["eps_diluted"]), "shares_diluted": round(shares / 1000, 3) if shares is not None else None}


def to_unified(data_dir, currency="USD"):
    """礼来细数据 → 统一格式（income、balance、segments；cashflow 暂无）。"""
    st = Store(data_dir)
    income = []
    for r in st.rows["income_statement"]:
        q, n = r["quarter"], int(r["quarter"][5])
        income.append(_income_row(r, q[:4], "Q%d" % n, "%s-%s" % (q[:4], _START[n]), r["period_end"], 3, currency))
    for r in st.rows["income_annual"]:
        if str(r["months"]) == "12":
            income.append(_income_row(r, r["year"], "FY", "%s-01-01" % r["year"], r["period_end"], 12, currency))
    balance = []
    for r in st.rows["balance_sheet"]:
        q = r["quarter"]
        sd, ld = _f(r.get("short_term_debt")), _f(r.get("long_term_debt"))
        balance.append({"fiscal_year": q[:4], "fiscal_period": "Q%s" % q[5], "period_end": r["period_end"], "currency": currency,
                        "unit": "millions", "source": SOURCE, "cash": None, "short_term_investments": None,
                        "cash_and_sti": _f(r["cash_and_sti"]), "receivables": _f(r["accounts_receivable"]),
                        "inventories": _f(r["inventories"]), "total_current_assets": _f(r["total_current_assets"]),
                        "total_assets": _f(r["total_assets"]), "total_current_liabilities": _f(r["total_current_liabilities"]),
                        "total_debt": (sd or 0) + (ld or 0) if (sd is not None or ld is not None) else None,
                        "total_liabilities": _f(r["total_liabilities"]), "total_equity": _f(r["total_equity"]), "equity_parent": None})
    segments = []
    for r in st.rows["product_revenue"]:
        q = r["quarter"]
        segments.append({"fiscal_year": q[:4], "fiscal_period": "Q%s" % q[5], "period_end": "%s-%s" % (q[:4], QUARTER_END[int(q[5])]),
                         "months": 3, "axis": "product", "member": r["product"], "name_en": r["product"], "name_zh": "",
                         "group": r["area"], "value": _f(r["total"]), "currency": currency, "unit": "millions", "basis": "reported", "source": SOURCE})
    for r in st.rows["price_volume"]:
        if r["region"] == "Total Revenue":
            continue
        q = r["quarter"]
        segments.append({"fiscal_year": q[:4], "fiscal_period": "Q%s" % q[5], "period_end": r["period_end"], "months": 3,
                         "axis": "geography", "member": r["region"], "name_en": r["region"], "name_zh": "", "group": "",
                         "value": _f(r["revenue"]), "currency": currency, "unit": "millions", "basis": "reported", "source": SOURCE})
    return {"income": income, "balance": balance, "cashflow": [], "segments": segments}
