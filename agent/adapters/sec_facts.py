"""SEC companyfacts（XBRL 财务数据接口）→ 统一格式的利润表、资产负债表、现金流量表。

数据：https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json。里面是公司在 10-K/10-Q 里申报过的、不带维度的
us-gaap 标准科目，每个数带期间、申报编号（accn）和申报日期。公司自定义科目和带维度的数不在里面
（收入构成从每份申报的 XBRL 实例里读，见 sec_xbrl.py）。

规则（每条都能对回原文）
- 映射：每个统一字段有一串候选科目（*_MAP），在同一份申报里取第一个有值的；某家公司可以有覆盖映射（state.json 的 mapping，
  例如 GPT 从这家公司实际用的科目里提议、经勾稽核对通过的），排在候选科目前面。
- 财年：按 10-K 里的全年期间确定每个财年的起止日；财年名称取截止日往前 20 天所在的年份
  （美敦力 2026 财年截至 2026 年 4 月；Veeva 2026 财年截至 2026 年 1 月；强生 2022 财年截至 2023-01-01，仍叫 2022 财年）。
- 季度取 3 个月期间；第四季度 = 全年 − 年初至今 9 个月（derived）；现金流单季 = 年初至今相减（derived）。
- 同一期间常有多份申报（以后的申报会重述旧数，例如剥离业务改列终止经营）。每个财年先用“最新申报”的一套数：
  季度和年初至今取最新申报，全年取年初至今 9 个月那份申报之后的第一份 10-K（保证全年和 9 个月是同一批申报口径）；
  三个季度之和与年初至今对不上（说明这一年中间有重述），改用“首次申报”的一套数；都对不上就不推算第四季度并提醒。
- 推算（都是原文列出的数之间的恒等关系）：没单列毛利时，毛利 = 营收 − 营业成本；没单列营业利润、但列了营业外收支合计时，
  营业利润 = 税前利润 − 营业外收支合计；没单列总负债时，总负债 = 负债和权益合计 − 权益合计；销售费用和管理费用分列时 SG&A 为两者之和；
  没单列合并净利润时，净利润 = 归母净利润 + 少数股东损益。
- 净利润 ≠ 税前利润 − 所得税、差额恰好是原文列出的税后项目（终止经营损益、税后列示的权益法投资损益）时：净利润照原文，
  所得税留空（统一格式没有放税后项目的列，留空表示这一期不能用“税前 − 所得税”核对），并在报告里列出；差额解释不了就判为错误。
- 金额换算成百万（股数为百万股，每股收益不变）。四舍五入的容差：每个数最多差半个舍入单位，舍入单位从数值本身推断
  （能整除它的最大单位：100 万、10 万、1 万、1000、1 元），参与计算的数各算一次。
"""
import datetime as dt

FORMS = ("10-K", "10-Q", "10-K/A", "10-Q/A", "10-KT", "10-QT")

# 统一字段 -> 候选科目（按优先级，同一份申报里取第一个有值的）
INCOME_MAP = [
    ("revenue", ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                 "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet", "SalesRevenueGoodsNet"]),
    ("cost_of_sales", ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold",
                       "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
                       "CostOfGoodsSoldExcludingDepreciationDepletionAndAmortization"]),
    ("gross_profit", ["GrossProfit"]),
    ("rnd", ["ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost", "ResearchAndDevelopmentExpense",
             "ResearchAndDevelopmentExpenseSoftwareExcludingAcquiredInProcessCost"]),
    ("sga", ["SellingGeneralAndAdministrativeExpense"]),
    ("acquired_iprd", ["ResearchAndDevelopmentAssetAcquiredOtherThanThroughBusinessCombinationWrittenOff",
                       "ResearchAndDevelopmentInProcess"]),
    ("operating_income", ["OperatingIncomeLoss"]),
    ("pretax_income", ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                       "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"]),
    ("income_tax", ["IncomeTaxExpenseBenefit"]),
    ("net_income", ["ProfitLoss"]),
    ("net_income_parent", ["NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"]),
    ("eps_diluted", ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"]),
    ("shares_diluted", ["WeightedAverageNumberOfDilutedSharesOutstanding", "WeightedAverageNumberOfShareOutstandingBasicAndDiluted"]),
]
# 推算和核对用的辅助科目
AUX_MAP = [
    ("selling", ["SellingAndMarketingExpense", "SellingExpense"]),
    ("ga", ["GeneralAndAdministrativeExpense"]),
    ("nonoperating", ["NonoperatingIncomeExpense"]),
    ("nci", ["NetIncomeLossAttributableToNoncontrollingInterest"]),
    ("discontinued", ["IncomeLossFromDiscontinuedOperationsNetOfTaxIncludingPortionAttributableToNoncontrollingInterest",
                      "IncomeLossFromDiscontinuedOperationsNetOfTax",
                      "IncomeLossFromDiscontinuedOperationsNetOfTaxAttributableToReportingEntity"]),
    ("equity_method", ["IncomeLossFromEquityMethodInvestmentsNetOfTax", "IncomeLossFromEquityMethodInvestments"]),
    ("liabilities_and_equity", ["LiabilitiesAndStockholdersEquity"]),
    ("debt_current", ["DebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent", "ShortTermDebtAndCurrentPortionOfLongTermDebt"]),
    ("debt_noncurrent", ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations", "ConvertibleDebtNoncurrent",
                         "ConvertibleNotesPayable", "LongTermNotesPayable"]),
]
BALANCE_MAP = [
    ("cash", ["CashAndCashEquivalentsAtCarryingValue", "Cash", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]),
    ("short_term_investments", ["ShortTermInvestments", "MarketableSecuritiesCurrent", "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
                                "OtherShortTermInvestments"]),
    ("cash_and_sti", ["CashCashEquivalentsAndShortTermInvestments"]),
    ("receivables", ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent", "AccountsNotesAndLoansReceivableNetCurrent",
                     "AccountsAndOtherReceivablesNetCurrent"]),
    ("inventories", ["InventoryNet"]),
    ("total_current_assets", ["AssetsCurrent"]),
    ("total_assets", ["Assets"]),
    ("total_current_liabilities", ["LiabilitiesCurrent"]),
    ("total_debt", ["DebtLongtermAndShorttermCombinedAmount"]),
    ("total_liabilities", ["Liabilities"]),
    ("total_equity", ["StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]),
    ("equity_parent", ["StockholdersEquity"]),
]
CASHFLOW_MAP = [
    ("cfo", ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]),
    ("capex", ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
               "PaymentsToAcquireOtherPropertyPlantAndEquipment", "PaymentsForCapitalImprovements"]),
    ("dividends_paid", ["PaymentsOfDividends", "PaymentsOfDividendsCommonStock", "PaymentsOfOrdinaryDividends"]),
    ("buybacks", ["PaymentsForRepurchaseOfCommonStock"]),
]
INCOME_FIELDS = [f for f, _ in INCOME_MAP]
ADDITIVE_INCOME = ["revenue", "cost_of_sales", "gross_profit", "rnd", "sga", "acquired_iprd", "operating_income", "pretax_income",
                   "income_tax", "net_income", "net_income_parent"]
BALANCE_FIELDS = [f for f, _ in BALANCE_MAP]
CASHFLOW_FIELDS = ["cfo", "capex", "dividends_paid", "buybacks"]
ALL_MAPS = INCOME_MAP + AUX_MAP + BALANCE_MAP + CASHFLOW_MAP


def _date(s):
    return dt.date.fromisoformat(s) if s else None


def _days(start, end):
    return (_date(end) - _date(start)).days + 1


def _next_day(d):
    return (_date(d) + dt.timedelta(days=1)).isoformat()


def fiscal_label(end):
    """财年名称：截止日往前 20 天所在的年份。"""
    return (_date(end) - dt.timedelta(days=20)).year


def rounding_unit(val, kind="money"):
    """一个数的舍入单位（换算成百万后）：能整除它的最大单位（100 万、10 万、1 万、1000、1 元）；每股收益按分（0.01）。"""
    if kind == "per_share":
        return 0.01
    v = abs(val)
    for p in (1e6, 1e5, 1e4, 1e3):
        if abs(v / p - round(v / p)) < 1e-6:
            return p / 1e6
    return 1e-6


class Value:
    """一个统一字段的值：数值（百万）+ 出处（申报编号）+ 参与计算的每个数的舍入单位（百万）。"""
    __slots__ = ("v", "accns", "units")

    def __init__(self, v, accns=(), units=()):
        self.v, self.accns, self.units = v, tuple(accns), tuple(units)

    def __sub__(self, o):
        return Value(self.v - o.v, self.accns + o.accns, self.units + o.units)

    def __add__(self, o):
        return Value(self.v + o.v, self.accns + o.accns, self.units + o.units)

    @property
    def tol(self):
        """四舍五入最多能差多少：每个数半个单位。"""
        return sum(u / 2 for u in self.units)


def total_tol(*vals):
    return sum(v.tol for v in vals if v is not None) + 1e-6


class Facts:
    """companyfacts 的索引：科目 -> 期间 (start, end) -> [(申报日期, 申报编号, 申报类型, 值, 单位, 种类)]。只收 10-K/10-Q（及修订）。"""

    def __init__(self, companyfacts, taxonomy="us-gaap"):
        self.cik = companyfacts.get("cik")
        self.name = companyfacts.get("entityName")
        g = (companyfacts.get("facts") or {}).get(taxonomy) or {}
        self.labels = {t: (v.get("label") or "") for t, v in g.items()}
        self.by_tag = {}
        self.filings = {}      # accn -> {form, filed}
        cur = {}
        for tag, v in g.items():
            for unit, facts in (v.get("units") or {}).items():
                kind = "shares" if unit == "shares" else ("per_share" if "/shares" in unit else "money")
                if kind == "money" and len(unit) == 3:
                    cur[unit] = cur.get(unit, 0) + len(facts)
                for f in facts:
                    form = f.get("form") or ""
                    if form not in FORMS or "end" not in f or f.get("val") is None:
                        continue
                    rec = (f.get("filed") or "", f.get("accn") or "", form, float(f["val"]), unit, kind)
                    self.by_tag.setdefault(tag, {}).setdefault((f.get("start"), f["end"]), []).append(rec)
                    self.filings.setdefault(rec[1], {"form": form, "filed": rec[0]})
        self.currency = max(cur, key=cur.get) if cur else "USD"

    def add_instance(self, accn, form, filed, plain):
        """把某份申报 XBRL 实例里的数并进来（companyfacts 漏了这份申报，或缺某个由实例推出的数时）：只补这份申报里还没有的科目和期间。
        plain: {科目: {(start, end): (值, 种类)}}。返回补进来的条数。"""
        n = 0
        self.filings.setdefault(accn, {"form": form, "filed": filed})
        for tag, periods in plain.items():
            for key, (val, kind) in periods.items():
                recs = self.by_tag.setdefault(tag, {}).setdefault(key, [])
                if any(r[1] == accn for r in recs):
                    continue
                unit = "shares" if kind == "shares" else ("USD/shares" if kind == "per_share" else self.currency)
                recs.append((filed, accn, form, float(val), unit, kind))
                n += 1
        return n

    def accn_period_end(self):
        """每份申报涉及的最晚日期（用作报告期末）。"""
        out = {}
        for periods in self.by_tag.values():
            for (start, end), recs in periods.items():
                for r in recs:
                    if end > out.get(r[1], ""):
                        out[r[1]] = end
        return out

    def tags(self):
        return sorted(self.by_tag)

    def candidates(self, tags, key):
        """候选科目在这个期间的全部申报记录：[(申报日期, 申报编号, 科目序号, 值, 种类, 科目, 申报类型)]。"""
        out = []
        for rank, tag in enumerate(tags):
            for filed, accn, form, val, unit, kind in self.by_tag.get(tag, {}).get(key, []):
                out.append((filed, accn, rank, val, kind, tag, form))
        return out

    def pick(self, tags, key, vintage="latest", accn=None):
        """在选定的申报里取第一个有值的候选科目。vintage：latest 最新申报 / original 首次申报；
        accn 指定申报时只在这份申报里找（同一行的数都出自同一份申报，不同批次的申报口径不混用）。
        返回 (值, 科目, 申报编号, 申报类型, 种类) 或 None。"""
        cands = self.candidates(tags, key)
        if accn is not None:
            cands = [c for c in cands if c[1] == accn]
        if not cands:
            return None
        filings = sorted({(c[0], c[1]) for c in cands}, reverse=(vintage == "latest"))
        chosen = filings[0][1]
        best = min((c for c in cands if c[1] == chosen), key=lambda c: c[2])
        return best[3], best[5], best[1], best[6], best[4]

    def filings_for(self, tags, key):
        """报告了这个期间的申报：[(申报日期, 申报编号)]，按日期从早到晚。"""
        return sorted({(c[0], c[1]) for c in self.candidates(tags, key)})

    def periods(self, tags):
        out = set()
        for tag in tags:
            out.update(self.by_tag.get(tag, {}))
        return out


# ---------------- 财年日历 ----------------
def fiscal_calendar(facts, first_year):
    """返回 [(财年名称, 起始日, 截止日或 None)]：每个财年取 10-K 里出现最多的全年期间；最后一个 10-K 之后的进行中财年截止日为 None。"""
    spans = {}
    for periods in facts.by_tag.values():
        for (start, end), recs in periods.items():
            if start and 340 <= _days(start, end) <= 380 and any(r[2].startswith("10-K") for r in recs):
                spans[(start, end)] = spans.get((start, end), 0) + len(recs)
    by_label = {}
    for (start, end), n in spans.items():
        lab = fiscal_label(end)
        if lab not in by_label or n > by_label[lab][2]:
            by_label[lab] = (start, end, n)
    years = [(lab, s, e) for lab, (s, e, n) in sorted(by_label.items())]
    if years:
        lab, s, e = years[-1]
        years.append((lab + 1, _next_day(e), None))
    return [y for y in years if y[0] >= first_year]


def _quarter_no(fy_start, end):
    days = (_date(end) - _date(fy_start)).days + 1
    return max(1, min(4, int(round(days / 91.31))))


def _in_year(fy_start, fy_end, start, end):
    lo = _date(fy_start) - dt.timedelta(days=6)
    hi = (_date(fy_end) if fy_end else _date(fy_start) + dt.timedelta(days=372)) + dt.timedelta(days=6)
    return lo <= _date(start) and _date(end) <= hi


def year_periods(facts, tags, fy):
    """一个财年里出现过的期间：{"Q1".."Q4": 3 个月, "H1": 年初至今 6 个月, "9M": 年初至今 9 个月, "FY": 全年} -> (start, end)。"""
    lab, fy_start, fy_end = fy
    out = {}
    for start, end in sorted(facts.periods(tags), key=lambda k: (str(k[0]), k[1])):
        if not start or not _in_year(fy_start, fy_end, start, end):
            continue
        n = _days(start, end)
        if 80 <= n <= 100:
            out.setdefault("Q%d" % _quarter_no(fy_start, end), (start, end))
        elif abs((_date(start) - _date(fy_start)).days) <= 6:
            if 170 <= n <= 190:
                out.setdefault("H1", (start, end))
            elif 260 <= n <= 285:
                out.setdefault("9M", (start, end))
            elif fy_end and end == fy_end and 340 <= n <= 380:
                out["FY"] = (start, end)
    return out


# ---------------- 映射与取数 ----------------
class Mapper:
    """统一字段 -> 候选科目（覆盖映射在前），并记录实际用到的科目。"""

    def __init__(self, facts, overrides=None):
        self.f = facts
        self.overrides = dict(overrides or {})
        self.all = {}
        for field, tags in ALL_MAPS:
            extra = [t for t in self.overrides.get(field, []) if t not in tags]
            self.all[field] = extra + list(tags)
        self.used = {}
        self.no_revenue = not any(self.f.by_tag.get(t) for t in self.all["revenue"])

    def get(self, field, key, vintage, accn=None):
        r = self.f.pick(self.all[field], key, vintage, accn)
        if r is None:
            return None
        u = self.used.setdefault(field, {})
        u[r[1]] = u.get(r[1], 0) + 1
        v = r[0] if r[4] == "per_share" else r[0] / 1e6
        return Value(v, [r[2]], [rounding_unit(r[0], r[4])])

    def anchor_tags(self, kind):
        return {"income": self.all["revenue"], "income_fallback": self.all["net_income_parent"],
                "cashflow": self.all["cfo"], "balance": self.all["total_assets"]}[kind]

    def anchor_accn(self, kind, key, vintage, accn=None):
        """这一行取数的申报：营收（现金流看经营现金流，资产负债表看总资产）所在的申报；指定的申报里没有就按 vintage 选。"""
        field = {"income": "revenue", "cashflow": "cfo", "balance": "total_assets"}[kind]
        for a in ([accn, None] if accn else [None]):
            r = self.f.pick(self.all[field], key, vintage, a)
            if r is None and kind == "income" and self.no_revenue:      # 没有收入的公司（研发阶段）才按净利润找申报
                r = self.f.pick(self.all["net_income_parent"], key, vintage, a)
            if r is not None:
                return r[2]
        return None


def income_values(m, key, vintage, accn=None):
    """一个期间的利润表字段（Value），所有字段尽量取自同一份申报（以营收所在的申报为准）。"""
    accn = m.anchor_accn("income", key, vintage, accn)
    if accn is None:
        return None
    out = {field: m.get(field, key, vintage, accn) for field in INCOME_FIELDS}
    aux = {k: m.get(k, key, vintage, accn) for k in ("selling", "ga", "nonoperating", "nci", "discontinued", "equity_method")}
    if out["sga"] is None and aux["ga"] is not None:
        out["sga"] = aux["ga"] + aux["selling"] if aux["selling"] is not None else aux["ga"]
    if out["gross_profit"] is None and out["revenue"] is not None and out["cost_of_sales"] is not None:
        out["gross_profit"] = out["revenue"] - out["cost_of_sales"]
    if out["operating_income"] is None and out["pretax_income"] is not None and aux["nonoperating"] is not None:
        out["operating_income"] = out["pretax_income"] - aux["nonoperating"]
    if out["net_income"] is None and out["net_income_parent"] is not None:
        out["net_income"] = out["net_income_parent"] + aux["nci"] if aux["nci"] is not None else out["net_income_parent"]
    out["_discontinued"], out["_equity_method"] = aux["discontinued"], aux["equity_method"]
    out["_accn"] = accn
    return out


def cashflow_values(m, key, vintage, accn=None):
    accn = m.anchor_accn("cashflow", key, vintage, accn)
    if accn is None:
        return None
    out = {field: m.get(field, key, vintage, accn) for field in CASHFLOW_FIELDS}
    out["_accn"] = accn
    return out


def balance_values(m, end, vintage):
    key = (None, end)
    accn = m.anchor_accn("balance", key, vintage)
    if accn is None:
        return None
    out = {field: m.get(field, key, vintage, accn) for field in BALANCE_FIELDS}
    le = m.get("liabilities_and_equity", key, vintage, accn)
    if out["total_equity"] is None and out["equity_parent"] is not None:
        out["total_equity"] = out["equity_parent"]
    if out["cash_and_sti"] is None and out["cash"] is not None:     # 这份申报没列短期投资时，现金及短期投资 = 现金
        out["cash_and_sti"] = out["cash"] + out["short_term_investments"] if out["short_term_investments"] is not None else out["cash"]
    if out["total_liabilities"] is None and le is not None and out["total_equity"] is not None:
        out["total_liabilities"] = le - out["total_equity"]
    if out["total_debt"] is None:
        cur = m.get("debt_current", key, vintage, accn)
        if cur is None:   # 一年内到期的长期债务和短期借款分列时相加（商业票据通常已含在短期借款里，不另加）
            for tag in ("LongTermDebtCurrent", "ShortTermBorrowings"):
                r = m.f.pick([tag], key, vintage, accn)
                if r is not None and r[2] == accn:
                    v = Value(r[0] / 1e6, [r[2]], [rounding_unit(r[0])])
                    cur = v if cur is None else cur + v
        nc = m.get("debt_noncurrent", key, vintage, accn)
        if cur is not None or nc is not None:
            out["total_debt"] = (cur or Value(0.0)) + (nc or Value(0.0))
    out["_accn"] = accn
    return out


# ---------------- 组装 ----------------
def _row_base(fy, fp, start, end, months, currency, basis, source):
    return {"fiscal_year": str(fy), "fiscal_period": fp, "period_start": start, "period_end": end, "months": months,
            "currency": currency, "unit": "millions", "basis": basis, "source": source}


def _num(v):
    if v is None:
        return None
    return round(v.v if isinstance(v, Value) else v, 6)


def _src(facts, accns, filled=()):
    """出处：申报类型 + 申报编号；推算的期间用“−”连接（全年 − 9 个月），个别科目取自其他申报时注明。"""
    seen = []
    for a in accns:
        if a and a not in seen:
            seen.append(a)
    out = "SEC " + " − ".join("%s %s" % (facts.filings.get(a, {}).get("form", ""), a) for a in seen)
    extra = [a for a in dict.fromkeys(filled) if a and a not in seen]
    if extra:
        out += "（个别科目取自 %s）" % "、".join("%s %s" % (facts.filings.get(a, {}).get("form", ""), a) for a in extra)
    return out


def _sum(parts):
    s = parts[0]
    for p in parts[1:]:
        s = s + p
    return s


def _consistent(qs, ytd, loose=False, fields=("revenue", "net_income_parent")):
    """季度之和与年初至今是否一致。容差：每个数半个舍入单位；loose 时用统一格式核对“四个季度之和 ≈ 全年”的容差
    （agent/core/schema.py：max(3, 0.1%)），用来容忍公司在后续申报里对个别季度的微小修订。返回问题列表。"""
    probs = []
    for k in fields:
        parts, tot = [q.get(k) for q in qs], ytd.get(k)
        if tot is None or any(p is None for p in parts):
            continue
        s = _sum(parts)
        tol = max(3, abs(tot.v) * 0.001) if loose else total_tol(s, tot)
        if abs(s.v - tot.v) > tol:
            probs.append("%s 季度之和 %.3f / 年初至今 %.3f" % (k, s.v, tot.v))
    return probs


def _merge(primary, secondary):
    """primary 里缺的字段用 secondary 同一期间的数补上（只在两套数相同、没有重述时用）。"""
    out = {}
    for p in set(primary) | set(secondary):
        if p not in primary:
            out[p] = secondary[p]
            continue
        row = dict(primary[p])
        for k, v in secondary.get(p, {}).items():
            if not k.startswith("_") and row.get(k) is None and v is not None:
                row[k] = v
                row["_filled"] = secondary[p]["_accn"]
        out[p] = row
    return out


def _same(a, b, fields=ADDITIVE_INCOME):
    """两套数（同一财年的各期间）是否在四舍五入范围内相同。"""
    for p in set(a) & set(b):
        for k in fields:
            x, y = a[p].get(k), b[p].get(k)
            if x is None or y is None:
                continue
            if abs(x.v - y.v) > total_tol(x, y):
                return False
    return True


def _year_problems(vals, loose):
    probs = []
    if all(q in vals for q in ("Q1", "Q2", "Q3", "9M")):
        probs += _consistent([vals["Q1"], vals["Q2"], vals["Q3"]], vals["9M"], loose)
    if all(q in vals for q in ("Q1", "Q2", "H1")):
        probs += _consistent([vals["Q1"], vals["Q2"]], vals["H1"], loose)
    return probs


def _year_values(m, per, vintage):
    """一个财年在某套申报口径下的各期间数值。latest：季度和年初至今取最新申报，全年取 9 个月那份申报之后的第一份 10-K。"""
    vals = {}
    for p, key in per.items():
        if p == "FY":
            continue
        v = income_values(m, key, vintage)
        if v:
            vals[p] = v
    if "FY" in per:
        accn = None
        if vintage == "latest" and "9M" in vals:
            after = m.f.filings.get(vals["9M"]["_accn"], {}).get("filed", "")
            later = [(d, a) for d, a in m.f.filings_for(m.anchor_tags("income_fallback" if m.no_revenue else "income"), per["FY"])
                     if d >= after]
            accn = later[0][1] if later else None
        v = income_values(m, per["FY"], vintage, accn)
        if v:
            vals["FY"] = v
    return vals


class Built:
    """build_statements 的结果：rows（统一格式三张表）、notes/warns/errors（报告用）、used（每个期间取数的申报，收入构成按它对齐）、
    calendar（财年日历）、mapper（实际用到的科目）。"""

    def __init__(self):
        self.rows, self.notes, self.warns, self.errors, self.used, self.calendar, self.mapper = {}, [], [], [], {}, [], None


def build_statements(facts, first_year=2021, overrides=None):
    """companyfacts -> Built。"""
    m = Mapper(facts, overrides)
    cur = facts.currency
    income, cashflow, notes, warns, errors = [], [], [], [], []
    vintage_used, after_tax_rows, used = {}, [], {}
    cal = fiscal_calendar(facts, first_year)
    anchor = m.anchor_tags("income_fallback" if m.no_revenue else "income")
    for fy in cal:
        lab = fy[0]
        per = year_periods(facts, anchor, fy)
        if not per:
            continue
        tried = {v: _year_values(m, per, v) for v in ("latest", "original")}
        # 没有重述（两套数在四舍五入范围内相同）时用首次申报的数（精度往往更高：以后的申报常把旧数改按整百万列示），
        # 首次申报里没标准科目的字段用以后申报里的同一个数补上
        order = ("latest", "original")
        restated = not _same(tried["latest"], tried["original"])
        if not restated:
            tried["original"] = _merge(tried["original"], tried["latest"])
            order = ("original", "latest")
        chosen = None
        for loose in (False, True):          # 先要求在四舍五入范围内一致，都不行再用统一格式“四个季度之和 ≈ 全年”的容差
            for vintage in order:
                if not _year_problems(tried[vintage], loose):
                    chosen = (vintage, tried[vintage])
                    break
            if chosen:
                break
        if chosen is None:
            vals = tried["original"]
            warns.append("%s 财年：季度与年初至今对不上（%s），不推算第四季度" % (lab, "；".join(_year_problems(vals, True)[:3])))
            vals.pop("9M", None)
            chosen = ("original", vals)
        vintage, vals = chosen
        vintage_used[lab] = vintage if restated else "same"
        for p, v in vals.items():
            rev = v.get("revenue")
            used[(str(lab), p)] = {"accn": v["_accn"], "start": per[p][0], "end": per[p][1],
                                   "revenue": rev.v if rev is not None else None, "tol": rev.tol if rev is not None else 0}
        for q in ("Q1", "Q2", "Q3"):
            if q in vals:
                row = _row_base(lab, q, per[q][0], per[q][1], 3, cur, "reported",
                                _src(facts, [vals[q]["_accn"]], [vals[q].get("_filled")]))
                income.append(_fill_income(row, vals[q], after_tax_rows, errors))
        if "FY" in vals:
            fyv = vals["FY"]
            row = _row_base(lab, "FY", per["FY"][0], per["FY"][1], 12, cur, "reported", _src(facts, [fyv["_accn"]], [fyv.get("_filled")]))
            income.append(_fill_income(row, fyv, after_tax_rows, errors))
            if "9M" in vals:
                nine = vals["9M"]
                q4 = {}
                for field in ADDITIVE_INCOME:
                    a, b = fyv.get(field), nine.get(field)
                    q4[field] = a - b if a is not None and b is not None else None
                for field in ("_discontinued", "_equity_method"):     # 税后项目只在第四季度出现时，前三季度没有这一行
                    a, b = fyv.get(field), nine.get(field)
                    q4[field] = a - b if a is not None and b is not None else a
                rep = None
                q4key = (_next_day(per["9M"][1]), per["FY"][1])
                if q4key in facts.periods(anchor):     # 少数公司在 10-K 里另报第四季度（例如每股收益）
                    rep = income_values(m, q4key, "latest")
                q4["eps_diluted"] = rep.get("eps_diluted") if rep else None
                q4["shares_diluted"] = rep.get("shares_diluted") if rep else None
                row = _row_base(lab, "Q4", q4key[0], q4key[1], 3, cur, "derived",
                                _src(facts, [fyv["_accn"], nine["_accn"]], [fyv.get("_filled"), nine.get("_filled")]))
                income.append(_fill_income(row, q4, after_tax_rows, errors))
        # 现金流量表：同一财年用同一套申报口径（全年同样取 9 个月那份申报之后的第一份 10-K）
        cper = year_periods(facts, m.anchor_tags("cashflow"), fy)
        cvals = {}
        for p in ("Q1", "H1", "9M"):
            if p in cper:
                v = cashflow_values(m, cper[p], vintage)
                if v:
                    cvals[p] = v
        if "FY" in cper:
            accn = None
            if vintage == "latest" and "9M" in cvals:
                after = facts.filings.get(cvals["9M"]["_accn"], {}).get("filed", "")
                later = [(d, a) for d, a in facts.filings_for(m.anchor_tags("cashflow"), cper["FY"]) if d >= after]
                accn = later[0][1] if later else None
            v = cashflow_values(m, cper["FY"], vintage, accn)
            if v:
                cvals["FY"] = v
        for q, prev, cur_p in (("Q1", None, "Q1"), ("Q2", "Q1", "H1"), ("Q3", "H1", "9M"), ("Q4", "9M", "FY")):
            if cur_p not in cvals or (prev and prev not in cvals):
                continue
            start = cper[cur_p][0] if prev is None else _next_day(cper[prev][1])
            accns = [cvals[cur_p]["_accn"]] + ([cvals[prev]["_accn"]] if prev else [])
            row = _row_base(lab, q, start, cper[cur_p][1], 3, cur, "reported" if prev is None else "derived", _src(facts, accns))
            for field in CASHFLOW_FIELDS:
                a = cvals[cur_p].get(field)
                b = cvals[prev].get(field) if prev else Value(0.0)
                row[field] = _num(a - b) if a is not None and b is not None else None
            cashflow.append(row)
        if "FY" in cvals:
            row = _row_base(lab, "FY", cper["FY"][0], cper["FY"][1], 12, cur, "reported", _src(facts, [cvals["FY"]["_accn"]]))
            for field in CASHFLOW_FIELDS:
                row[field] = _num(cvals["FY"].get(field))
            cashflow.append(row)
    for r in cashflow:
        r["fcf"] = round(r["cfo"] - r["capex"], 6) if r.get("cfo") is not None and r.get("capex") is not None else None
    balance = build_balance(m, cal, cur, warns, errors)
    later = [str(y) for y, v in sorted(vintage_used.items()) if v == "latest"]
    first = [str(y) for y, v in sorted(vintage_used.items()) if v == "original"]
    if later:
        notes.append("%s 财年的数后来被重述过，用最新申报（含重述）的一套数" % "、".join(later))
    if first:
        notes.append("%s 财年在年中有重述、最新申报的季度和年初至今对不上，用首次申报的一套数" % "、".join(first))
    if after_tax_rows:
        notes.append("净利润含终止经营或税后列示的权益法投资损益、不等于“税前利润 − 所得税”的期间，所得税留空：%s" % "、".join(after_tax_rows))
    b = Built()
    b.rows = {"income": income, "balance": balance, "cashflow": cashflow}
    b.notes, b.warns, b.errors, b.used, b.calendar, b.mapper = notes, warns, errors, used, cal, m
    return b


def _fill_income(row, vals, after_tax_rows, errors):
    row["_tol"] = {}
    for field in INCOME_FIELDS:
        v = vals.get(field)
        row[field] = _num(v)
        if v is not None:
            row["_tol"][field] = v.tol
    pt, tax, ni = vals.get("pretax_income"), vals.get("income_tax"), vals.get("net_income")
    if None in (pt, tax, ni):
        return row
    gap = ni.v - (pt.v - tax.v)
    if abs(gap) <= max(2, abs(pt.v) * 0.002):        # 统一格式的核对容差（agent/core/schema.py）
        return row
    disc, eq = vals.get("_discontinued"), vals.get("_equity_method")
    for combo in ([disc], [eq], [disc, eq]):
        items = [x for x in combo if x is not None]
        if len(items) != len(combo):
            continue
        if abs(gap - sum(x.v for x in items)) <= total_tol(pt, tax, ni, *items):
            row["income_tax"] = None
            after_tax_rows.append("%s %s" % (row["fiscal_year"], row["fiscal_period"]))
            return row
    errors.append("%s %s：净利润 %.1f ≠ 税前利润 %.1f − 所得税 %.1f，差额 %.1f 无法用终止经营或权益法投资损益解释" % (
        row["fiscal_year"], row["fiscal_period"], ni.v, pt.v, tax.v, gap))
    return row


def build_balance(m, cal, cur, warns, errors):
    rows = []
    if not cal:
        return rows
    ends = set()
    for tag in m.all["total_assets"]:
        for start, end in m.f.by_tag.get(tag, {}):
            if start is None:
                ends.add(end)
    for end in sorted(ends):
        fy = None
        for lab, s, e in cal:
            if s <= end and (e is None or end <= e):
                fy = (lab, s, e)
        if fy is None:
            continue
        # 资产负债表是时点数：用首次申报的数（以后的申报只做重分类）；两边相同时，首次申报里缺的科目用以后申报补上
        chosen, last = None, None
        both = {v: balance_values(m, end, v) for v in ("latest", "original")}
        if both["latest"] is None:
            continue
        same = all(x is None or y is None or abs(x.v - y.v) <= total_tol(x, y)
                   for x, y in ((both["latest"][k], both["original"][k]) for k in BALANCE_FIELDS))
        if same:
            merged = dict(both["original"])
            for k in BALANCE_FIELDS:
                if merged.get(k) is None and both["latest"].get(k) is not None:
                    merged[k] = both["latest"][k]
                    merged["_filled"] = both["latest"]["_accn"]
            both["original"] = merged
        for vintage in ("original", "latest"):
            vals = both[vintage]
            last = last or vals
            ta, tl, te = vals["total_assets"], vals["total_liabilities"], vals["total_equity"]
            if None in (ta, tl, te) or abs(ta.v - tl.v - te.v) <= total_tol(ta, tl, te):
                chosen = vals
                break
        if chosen is None:
            if last is None:
                continue
            ta, tl, te = last["total_assets"], last["total_liabilities"], last["total_equity"]
            errors.append("%s 资产负债表：资产 %.1f ≠ 负债 %.1f + 权益 %.1f" % (end, ta.v, tl.v, te.v))
            chosen = last
        row = {"fiscal_year": str(fy[0]), "fiscal_period": "Q%d" % _quarter_no(fy[1], end), "period_end": end, "currency": cur,
               "unit": "millions", "source": _src(m.f, [chosen["_accn"]], [chosen.get("_filled")])}
        row["_tol"] = {}
        for field in BALANCE_FIELDS:
            v = chosen.get(field)
            row[field] = _num(v)
            if v is not None:
                row["_tol"][field] = v.tol
        rows.append(row)
    return rows


def split_tol(rows):
    """把行里的容差信息拿出来：返回 (干净的行, {键: {字段: 容差}})。键：利润表/现金流 (财年, 期间)，资产负债表 期末日。"""
    clean, tols = {}, {}
    for name, rs in rows.items():
        out = []
        for r in rs:
            r = dict(r)
            t = r.pop("_tol", {})
            key = r["period_end"] if name == "balance" else (r["fiscal_year"], r["fiscal_period"])
            tols[(name, key)] = t
            out.append(r)
        clean[name] = out
    return clean, tols


# ---------------- 与已核对数据对比（回测） ----------------
COMPARE_INCOME = ["revenue", "cost_of_sales", "gross_profit", "rnd", "sga", "acquired_iprd", "operating_income", "pretax_income",
                  "income_tax", "net_income", "eps_diluted", "shares_diluted"]
COMPARE_BALANCE = ["cash_and_sti", "receivables", "inventories", "total_current_assets", "total_assets", "total_current_liabilities",
                   "total_debt", "total_liabilities", "total_equity"]
CORE = {"revenue", "gross_profit", "rnd", "operating_income", "net_income", "eps_diluted", "total_assets", "total_liabilities",
        "total_equity"}


def _half_unit(text):
    """已核对数据里一个数的半个舍入单位：按小数位数（6805.6 -> 0.05，65179 -> 0.5）。"""
    t = str(text).strip()
    return 0.5 * 10 ** -(len(t.split(".")[1]) if "." in t else 0)


def compare_reference(rows, tols, ref):
    """把适配器算出的行与已核对的统一格式数据比较（只比两边都有的数）。
    容差 = 我们这边参与计算的每个数 1 个舍入单位 + 已核对数据那个数半个舍入单位；每股收益两边都按分四舍五入（各 0.005）。
    SEC 一侧每个数按 1 个单位算：按整百万列示的报表为了让合计数对得上，个别行会多调 1（footing），
    例：礼来 2025 年一季度 10-Q 营业成本列 2,225，精确值 2,224.2。
    推算的第四季度若与已核对数据里直接给出的第四季度对不上，再与已核对数据自己的“全年 − 前三季度”比较
    （公司业绩新闻稿里的第四季度和 10-K 全年减前三季度可能因发稿后的调整而不同，这时以同口径比较为准，并记下差异）。
    返回 {compared, mismatches: [行], notes: [行], missing: {字段: 次数}, core_compared}。"""
    out = {"compared": 0, "core_compared": 0, "mismatches": [], "notes": [], "missing": {}}
    f = lambda v: None if v in (None, "") else float(v)
    ref_inc = {(r["fiscal_year"], r["fiscal_period"]): r for r in ref.get("income", [])}
    ref_bal = {r["period_end"]: r for r in ref.get("balance", [])}
    for r in rows.get("income", []):
        key = (r["fiscal_year"], r["fiscal_period"])
        o = ref_inc.get(key)
        if not o:
            continue
        t = tols.get(("income", key), {})
        for k in COMPARE_INCOME:
            a, b = r.get(k), f(o.get(k))
            if b is None:
                continue
            if a is None:
                out["missing"][k] = out["missing"].get(k, 0) + 1
                continue
            out["compared"] += 1
            out["core_compared"] += k in CORE
            tol = (0.01 if k == "eps_diluted" else 2 * t.get(k, 0) + _half_unit(o.get(k))) + 1e-6
            if abs(a - b) <= tol:
                continue
            if r["fiscal_period"] == "Q4" and r["basis"] == "derived" and k != "eps_diluted":
                parts = [ref_inc.get((r["fiscal_year"], p)) for p in ("FY", "Q1", "Q2", "Q3")]
                if all(p and f(p.get(k)) is not None for p in parts):
                    alt = f(parts[0][k]) - sum(f(p[k]) for p in parts[1:])
                    alt_tol = 2 * t.get(k, 0) + sum(_half_unit(p[k]) for p in parts) + 1e-6
                    if abs(a - alt) <= alt_tol:
                        out["notes"].append("%s %s %s：推算 %.1f，已核对数据直接给出 %s；与已核对数据的“全年 − 前三季度” %.1f 一致" % (
                            key[0], key[1], k, a, o[k], alt))
                        continue
            out["mismatches"].append("%s %s %s：SEC %.4g，已核对 %s（容差 %.2f）" % (key[0], key[1], k, a, o[k], tol))
    for r in rows.get("balance", []):
        o = ref_bal.get(r["period_end"])
        if not o:
            continue
        t = tols.get(("balance", r["period_end"]), {})
        for k in COMPARE_BALANCE:
            a, b = r.get(k), f(o.get(k))
            if b is None:
                continue
            if a is None:
                out["missing"][k] = out["missing"].get(k, 0) + 1
                continue
            out["compared"] += 1
            out["core_compared"] += k in CORE
            tol = 2 * t.get(k, 0) + _half_unit(o.get(k)) + 1e-6
            if abs(a - b) > tol:
                out["mismatches"].append("%s %s：SEC %.4g，已核对 %s（容差 %.2f）" % (r["period_end"], k, a, o[k], tol))
    return out
