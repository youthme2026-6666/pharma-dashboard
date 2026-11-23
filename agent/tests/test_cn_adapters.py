"""港股（hkex）、A 股（cninfo）适配器与共用的 cn_pdf：用冻结的报表页文字检验拆行、对照、核对、期间推算。不访问网络、不需要 pypdf。

固定样本（agent/tests/fixtures/cn/，只存报表所在几页的文字，不存整份 PDF）：
- 恒瑞医药 2026 年半年度报告、2026 年第三季度报告（巨潮，A 股统一格式）
- 信达生物 2025 年度业绩公告、2026 年中期业绩公告（披露易，港股格式）
- 迈瑞医疗 2026 年第三季度报告（深市格式：空格子不写“—”，靠 layout 文字定列）
恒瑞、信达四份报告的关键数字（下面的 HENGRUI_*、INNOVENT_*）是 2026-11-22 对照报表原文逐项人工核对过的；GPT 的回答用假的。
"""
import copy
import datetime as dt
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from agent.adapters import cn_pdf as P  # noqa: E402
from agent.adapters import cninfo, hkex  # noqa: E402
from agent.adapters.base import Context  # noqa: E402
from agent.core import registry as RG  # noqa: E402
from agent.core.store import UnifiedStore  # noqa: E402

FIX = os.path.join(ROOT, "agent", "tests", "fixtures", "cn")
CFG = json.load(open(os.path.join(ROOT, "agent", "config.json"), encoding="utf-8"))

# ---------------- 人工核对过的数（百万元；费用记正数）
HENGRUI_H1 = {  # 恒瑞医药 2026 年半年度报告：合并利润表 2026/2025 年半年度、合并资产负债表 2026-06-30/2025-12-31、合并现金流量表
    "income": {"cur": {"revenue": 15455.58372811, "cost_of_sales": 2113.34302935, "rnd": 3492.91160738, "sga": 5598.40412312,
                       "operating_income": 5087.74248921, "pretax_income": 4952.41746784, "income_tax": 487.13504796,
                       "net_income": 4465.28241988, "net_income_parent": 4465.4394281, "eps_diluted": 0.68},
               "prior": {"revenue": 15761.1936289, "cost_of_sales": 2114.96216918, "rnd": 3227.9334821, "sga": 5674.40778783,
                         "operating_income": 5158.89266521, "pretax_income": 5050.93420895, "income_tax": 596.26355174,
                         "net_income": 4454.67065721, "net_income_parent": 4450.10638446, "eps_diluted": 0.70}},
    "balance": {"cur": {"cash": 38734.32427837, "short_term_investments": 107.45916228, "receivables": 4920.29920976,
                        "inventories": 2993.97104937, "total_current_assets": 49607.55738005, "total_assets": 70773.12199889,
                        "total_current_liabilities": 4247.21556144, "total_liabilities": 5783.38158183, "total_equity": 64989.74041706,
                        "equity_parent": 64466.43020571, "total_debt": 0.0},
                "prior": {"cash": 40955.48246779, "total_current_assets": 51490.37245704, "total_assets": 69867.31577374,
                          "total_current_liabilities": 6369.237286, "total_liabilities": 8070.49235949, "total_equity": 61796.82341425,
                          "equity_parent": 61272.06864141}},
    "cashflow": {"cur": {"cfo": 1987.00387234, "capex": 1577.50532286}, "prior": {"cfo": 4300.4532514, "capex": 1267.56494742}},
}
HENGRUI_Q3 = {  # 恒瑞医药 2026 年第三季度报告：前三季度（1-9 月）与 2026-09-30
    "income": {"cur": {"revenue": 24408.76272419, "cost_of_sales": 3225.79322564, "rnd": 5422.96935997, "sga": 8525.69248084,
                       "operating_income": 7993.8937494, "pretax_income": 7728.62994488, "income_tax": 805.47588682,
                       "net_income": 6923.15405806, "net_income_parent": 6923.40421625, "eps_diluted": 1.05},
               "prior": {"revenue": 23188.08192877, "cost_of_sales": 3195.44118334, "net_income": 5760.23515202,
                         "net_income_parent": 5751.16953765, "eps_diluted": 0.89}},
    "balance": {"cur": {"total_assets": 76719.52431357, "total_liabilities": 9382.21625499, "total_equity": 67337.30805858,
                        "equity_parent": 66814.4105424, "cash": 43446.7695428, "total_debt": 354.10691072},
                "prior": {"total_assets": 69867.31577374, "total_equity": 61796.82341425}},
    "cashflow": {"cur": {"cfo": 7843.65012941, "capex": 2670.61643006}, "prior": {"cfo": 9110.43029027}},
}
INNOVENT_FY25 = {  # 信达生物 2025 年度业绩公告（RMB'000）
    "income": {"cur": {"revenue": 13041.523, "cost_of_sales": 1756.019, "gross_profit": 11285.504, "rnd": 2624.214, "sga": 6639.891,
                       "pretax_income": 838.924, "income_tax": 25.359, "net_income": 813.565, "eps_diluted": 0.47},
               "prior": {"revenue": 9421.888, "cost_of_sales": 1510.21, "gross_profit": 7911.678, "rnd": 2681.074, "sga": 5084.938,
                         "pretax_income": -78.621, "income_tax": 16.01, "net_income": -94.631, "eps_diluted": -0.06}},
    "balance": {"cur": {"total_assets": 37347.839, "total_liabilities": 17991.596, "total_equity": 19356.243,
                        "total_current_assets": 22013.335, "total_current_liabilities": 8386.565, "cash": 17344.705,
                        "receivables": 1713.832, "inventories": 1301.745, "total_debt": 2778.771, "short_term_investments": 886.741},
                "prior": {"total_assets": 21602.602, "total_liabilities": 8484.873, "total_equity": 13117.729, "total_debt": 2817.454}},
}
INNOVENT_H1_26 = {  # 信达生物 2026 年中期业绩公告
    "income": {"cur": {"revenue": 8617.801, "cost_of_sales": 1296.044, "rnd": 1683.708, "sga": 3729.869, "pretax_income": 1611.663,
                       "income_tax": 358.521, "net_income": 1253.142, "eps_diluted": 0.70},
               "prior": {"revenue": 5953.094, "net_income": 834.321, "income_tax": 62.796, "eps_diluted": 0.49}},
    "balance": {"cur": {"total_assets": 41616.652, "total_liabilities": 20870.812, "total_equity": 20745.84, "cash": 17380.396},
                "prior": {"total_assets": 37347.839, "total_liabilities": 17991.596, "total_equity": 19356.243}},
}

# 信达报表行 → 统一科目：一个认真的人（或 GPT）会怎么对。同一科目列几种写法（两份公告的行名略有不同），取存在的那几行
INNOVENT_MAP = {
    "income": {"revenue": [("Revenue from contracts with customers", None)], "cost_of_sales": [("Cost of sales", None)],
               "gross_profit": [("Gross profit", None)], "rnd": [("R&D expenses", None), ("Research and development expenses", None)],
               "sga": [("Administrative and other expenses", None), ("Selling and marketing expenses", None)],
               "pretax_income": [("Profit before tax", None), ("Profit (loss) before tax", None)],
               "income_tax": [("Income tax expense", None), ("Income tax (expense) credit", None)],
               "net_income": [("Profit for the period", None), ("Profit (loss) for the year", None)],
               "eps_diluted": [("– Diluted (RMB Yuan)", None)]},
    "balance": {"cash": [("Bank balances and cash", None)], "short_term_investments": [("Other financial assets", "^Current assets")],
                "receivables": [("Trade receivables", None)], "inventories": [("Inventories", None)],
                "total_current_assets": [("[小计] Current assets", None)],
                "total_assets": [("[小计] Non-current assets", None), ("[小计] Current assets", None)],
                "total_current_liabilities": [("[小计] Current liabilities", None)],
                "total_debt": [("Borrowings", None)],
                "total_liabilities": [("[小计] Current liabilities", None), ("[小计] Non-current liabilities", None)],
                "total_equity": [("Total equity", None)]},
}


def load_fixture(name):
    with open(os.path.join(FIX, name + ".json"), encoding="utf-8") as f:
        d = json.load(f)
    pages = [""] * d["total_pages"]
    for n, t in d["pages"].items():
        pages[int(n) - 1] = t
    layout = {int(n): t for n, t in d.get("layout", {}).items()}
    return d, pages, layout


def parse(name):
    d, pages, layout = load_fixture(name)
    return d, P.parse_document(pages, layout=lambda nums: {n: layout[n] for n in nums if n in layout})


def ids_for(lines, spec):
    """按行名（和小节）找行号；spec：[(行名, 小节正则或 None)]。同名的行都算。"""
    out = []
    for label, sect in spec:
        for i, l in enumerate(lines):
            if l["label"] == label and (sect is None or re.search(sect, l["section"])) and i not in out:
                out.append(i)
    return out


def answer_from(statements, spec, unit="RMB'000", eps_unit="yuan", tweak=None):
    """按 spec 生成一份 GPT 回答（只有行号），tweak 可以改坏它。"""
    ans = {"unit": unit, "currency": "CNY", "eps_unit": eps_unit}
    for kind in P.KINDS:
        lines = statements[kind]["lines"] if kind in statements else []
        ans[kind] = {f: (ids_for(lines, spec.get(kind, {}).get(f, [])) if lines else []) for f in P.FIELDS[kind]}
    if tweak:
        tweak(ans, statements)
    return ans


class FakeGPT:
    """假的 GPT：按 INNOVENT_MAP 的行名给出行号；answers 里放 tweak 函数时，第 n 次回答按第 n 个改坏。"""
    available = True

    def __init__(self, spec=None, tweaks=()):
        self.spec, self.tweaks = spec or INNOVENT_MAP, list(tweaks)
        self.calls = []
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "web_searches": 0}

    def respond(self, instructions, user_input, **kw):
        payload = json.loads(user_input)
        self.calls.append(payload)
        self.usage["calls"] += 1
        sts = {}
        for k, v in payload["statements"].items():
            lines, sec = [], ""
            for _, a, b, c, d in v["lines"]:
                sec = b or sec                                  # 小节只在变化时给出
                lines.append({"label": a, "section": sec, "cur": c, "prior": d})
            sts[k] = {"lines": lines}
        tweak = self.tweaks.pop(0) if self.tweaks else None
        ans = answer_from(sts, self.spec, tweak=tweak)
        return {"text": json.dumps(ans), "data": ans, "citations": [], "sources": [], "model": "fake", "status": "completed"}


def near(test, got, want, where):
    for kind, cols in want.items():
        for col, fields in cols.items():
            for f, v in fields.items():
                g = got[kind][col].get(f)
                test.assertIsNotNone(g, "%s %s %s %s 没有值" % (where, kind, col, f))
                test.assertAlmostEqual(g, v, places=5, msg="%s %s %s %s" % (where, kind, col, f))


# ---------------------------------------------------------------- 小工具
class TestParsing(unittest.TestCase):
    def test_values(self):
        cases = {"(1,296,044)": -1296044, "-116,288,242.80": -116288242.8, "—": 0, "–": 0, "RMB(0.46)": -0.46, "8,617,801": 8617801,
                 "0.73": 0.73, "(6)": -6, "−250,158.19": -250158.19}
        for tok, v in cases.items():
            self.assertAlmostEqual(P.parse_value(tok), v, places=6, msg=tok)
        for tok in ("七、61", "RMB'000", "(1,234", "abc", ""):
            self.assertIsNone(P.parse_value(tok), tok)

    def test_split_and_labels(self):
        self.assertEqual(P.split_line("Revenue from contracts with customers 4 8,617,801 5,953,094")[1][-2:],
                         [(8617801.0, "8,617,801"), (5953094.0, "5,953,094")])
        label, vals = P.split_line("Basic RMB(0.46) yuan RMB(0.64) yuan")
        self.assertEqual((label, [v for v, _ in vals]), ("Basic", [-0.46, -0.64]))
        self.assertEqual(P._clean_label("货币资金 七、1"), "货币资金")
        self.assertEqual(P._clean_label("经营活动产生的现金流量净额 79(1)"), "经营活动产生的现金流量净额")
        self.assertEqual(P._clean_label("七、70"), "")
        self.assertEqual(P._clean_label("Impairment losses on financial assets 3.1(b)"), "Impairment losses on financial assets")
        self.assertEqual(P.cas_core('1.归属于母公司股东的净利润（净亏损以“-”号填 列）'), "归属于母公司股东的净利润")
        self.assertEqual(P.cas_core('资产处置收益（损失以“－”'), "资产处置收益")
        self.assertTrue(P._joinable("购建固定资产、无形资产和其他长期资", "产支付的现金"))
        self.assertFalse(P._joinable("客户存款和同业存放款项净增加额", "收到的税费返还"))   # 上一行是完整科目（深市空行）
        self.assertTrue(P._joinable("Exchange differences arising on translation", "of foreign operations"))
        self.assertFalse(P._joinable("Current assets", "Inventories"))

    def test_titles(self):
        T = P.title_kind
        self.assertEqual(T("合并资产负债表")[0], "balance")
        self.assertEqual(T("2、合并年初到报告期末利润表")[0], "income")
        self.assertEqual(T("合并本报告期利润表")[0], "other")
        self.assertEqual(T("母公司现金流量表")[0], "other")
        self.assertEqual(T("公司资产负债表")[0], "other")
        self.assertEqual(T("合并资产负债表（续）")[0], "balance")
        self.assertEqual(T("CONDENSED CONSOLIDATED STATEMENT OF PROFIT OR LOSS AND OTHER", "COMPREHENSIVE INCOME"), ("income", 2))
        self.assertEqual(T("INTERIM CONDENSED CONSOLIDATED STATEMENT OF FINANCIAL", "POSITION")[0], "balance")
        self.assertEqual(T("CONDENSED CONSOLIDATED BALANCE SHEET")[0], "balance")
        self.assertEqual(T("CONSOLIDATED STATEMENT OF COMPREHENSIVE INCOME")[0], "oci")
        self.assertIsNone(T("consolidated statement of profit or loss and other comprehensive income for the year ended")[0])  # 正文折行
        self.assertIsNone(T("Consolidated Statement of Financial Position 120")[0])                                         # 目录
        self.assertIsNone(T("合并资产负债表中归属于母公司股东的其他综合收益累积余额：")[0])

    def test_units(self):
        self.assertEqual(P.unit_of("单位：元 币种：人民币"), (1e-6, "CNY"))
        self.assertEqual(P.unit_of("2026年6月30日 人民币元"), (1e-6, "CNY"))
        self.assertEqual(P.unit_of("单位：千元"), (1e-3, None))
        self.assertEqual(P.unit_of("NOTES RMB’000 RMB’000"), (1e-3, "CNY"))
        self.assertEqual(P.unit_of("US$'000"), (1e-3, "USD"))
        self.assertEqual(P.unit_of("RMB million"), (1.0, "CNY"))
        self.assertEqual(P.unit_of("For the six months ended 30 June"), (None, None))

    def test_source_titles(self):
        self.assertEqual(cninfo.parse_title("恒瑞医药2026年第三季度报告"), (2026, 9))
        self.assertEqual(cninfo.parse_title("2023年一季度报告"), (2023, 3))
        self.assertEqual(cninfo.parse_title("2026年半年度报告"), (2026, 6))
        self.assertEqual(cninfo.parse_title("<em>2024</em>年年度报告（更新后）"), (2024, 12))
        for t in ("恒瑞医药2025年年度报告摘要", "2025年半年度报告（英文版）", "2024年年度报告（更正前）", "关于2024年年度报告的更正公告"):
            self.assertIsNone(cninfo.parse_title(t), t)
        C = hkex.classify
        self.assertEqual(C({"LONG_TEXT": "Announcements and Notices - [Interim Results]",
                            "TITLE": "INTERIM RESULTS ANNOUNCEMENT\nFOR THE SIX MONTHS ENDED 30 JUNE 2026"}), (2026, 6, "ann"))
        self.assertEqual(C({"LONG_TEXT": "Announcements and Notices - [Final Results &#x2f; Closure of Books]",
                            "TITLE": "ANNUAL RESULTS ANNOUNCEMENT FOR THE YEAR ENDED DECEMBER 31, 2025"}), (2025, 12, "ann"))
        self.assertEqual(C({"LONG_TEXT": "Financial Statements&#x2f;ESG Information - [Annual Report &#x2f; Environmental, Social and Governance Information]",
                            "TITLE": "Annual Report 2025"}), (2025, 12, "rep"))
        self.assertEqual(C({"LONG_TEXT": "Financial Statements&#x2f;ESG Information - [Interim&#x2f;Half-Year Report]", "TITLE": "2026 INTERIM REPORT"}),
                         (2026, 6, "rep"))
        self.assertEqual(C({"LONG_TEXT": "Announcements and Notices - [Quarterly Results]",
                            "TITLE": "UNAUDITED RESULTS FOR THE NINE MONTHS ENDED 30 SEPTEMBER 2026"}), (2026, 9, "ann"))
        for item in ({"LONG_TEXT": "Announcements and Notices - [Interim Results]",
                      "TITLE": "CLARIFICATION ANNOUNCEMENT IN RELATION TO THE INTERIM RESULTS ANNOUNCEMENT FOR THE SIX MONTHS ENDED 30 JUNE 2025"},
                     {"LONG_TEXT": "(Cancelled since Headlines Superseded and Replaced) Announcements and Notices - [Final Results]",
                      "TITLE": "ANNUAL RESULTS ANNOUNCEMENT FOR THE YEAR ENDED 31 DECEMBER 2022"},
                     {"LONG_TEXT": "Announcements and Notices - [Results of AGM]", "TITLE": "POLL RESULTS OF THE ANNUAL GENERAL MEETING"},
                     {"LONG_TEXT": "Financial Statements&#x2f;ESG Information - [Environmental, Social and Governance Information]",
                      "TITLE": "2025 ENVIRONMENTAL, SOCIAL AND GOVERNANCE REPORT"}):
            self.assertIsNone(C(item), item["TITLE"])


# ---------------------------------------------------------------- 固定样本：A 股
class TestHengrui(unittest.TestCase):
    def check(self, name, want, months):
        d, stmts = parse(name)
        self.assertEqual(set(stmts), {"income", "balance", "cashflow"})
        m = P.cas_mapping(stmts)
        scale, cur = P.detect_unit(stmts["balance"])
        self.assertEqual((scale, cur), (1e-6, "CNY"))
        vals, probs, warns = P.evaluate({"period_end_year": d["fiscal_year"]}, stmts, m, {"scale": scale, "currency": cur}, "cas")
        self.assertEqual(probs, [])
        near(self, vals, want, name)
        return stmts, vals

    def test_h1(self):
        stmts, vals = self.check("hengrui_2026h1", HENGRUI_H1, 6)
        self.assertEqual(stmts["balance"]["pages"], [75, 76])        # 母公司报表不算
        self.assertAlmostEqual(vals["income"]["cur"]["gross_profit"], 15455.58372811 - 2113.34302935, places=6)

    def test_q3(self):
        self.check("hengrui_2026q3", HENGRUI_Q3, 9)

    def test_structure_check_catches_bad_line(self):
        d, stmts = parse("hengrui_2026q3")
        inc = stmts["income"]
        i = next(i for i, l in enumerate(inc["lines"]) if P.cas_core(l["label"]) == "研发费用")
        inc["lines"][i]["cur"] = "5,422,969,350.97"          # 读错一位：营业总成本对不上
        vals, probs, _ = P.evaluate({"period_end_year": 2026}, stmts, P.cas_mapping(stmts), {"scale": 1e-6}, "cas")
        self.assertTrue(any("营业总成本" in p for p in probs), probs)

    def test_szse_single_values(self):
        """深市报表空格子不写“—”：只有一个数的行按 layout 文字里的横向位置定列；整张表的结构核对要能通过。"""
        d, stmts = parse("mindray_2026q3")
        self.assertEqual(sum(len(st["bad"]) for st in stmts.values()), 0)
        vals, probs, _ = P.evaluate({"period_end_year": 2026}, stmts, P.cas_mapping(stmts), {"scale": 1e-6}, "cas")
        self.assertEqual(probs, [])
        self.assertAlmostEqual(vals["balance"]["cur"]["cash"], 19359.51017, places=5)
        self.assertAlmostEqual(vals["balance"]["prior"]["cash"], 17690.372308, places=5)


# ---------------------------------------------------------------- 固定样本：港股 + 假的 GPT
class TestInnovent(unittest.TestCase):
    def run_gpt(self, name, gpt):
        d, stmts = parse(name)
        header = "\n".join(st["unit_text"] for st in stmts.values())
        ans = gpt.respond("", json.dumps({"statements": {k: {"header": st["header"], "lines": [[i, l["label"], l["section"], l["cur"], l["prior"]]
                                                                                                 for i, l in enumerate(st["lines"])]}
                                                         for k, st in stmts.items()}}))["data"]
        m, unit, probs = P.verify_mapping(stmts, ans, header)
        if probs:
            return d, stmts, None, probs
        vals, probs, _ = P.evaluate({"period_end_year": d["fiscal_year"]}, stmts, m, unit, "ifrs")
        return d, stmts, vals, probs

    def test_fy2025(self):
        d, stmts, vals, probs = self.run_gpt("innovent_2025fy_ann", FakeGPT())
        self.assertEqual(probs, [])
        self.assertNotIn("cashflow", stmts)                   # 业绩公告没有现金流量表（在年报里）
        near(self, vals, INNOVENT_FY25, "innovent FY2025")

    def test_h1_2026(self):
        d, stmts, vals, probs = self.run_gpt("innovent_2026h1_ann", FakeGPT())
        self.assertEqual(probs, [])
        near(self, vals, INNOVENT_H1_26, "innovent 2026H1")

    def test_wrong_answers_rejected(self):
        def wrong_rnd(ans, sts):          # 研发费用对到了销售费用那一行
            ans["income"]["rnd"] = ids_for(sts["income"]["lines"], [("Selling and marketing expenses", None)])

        def wrong_revenue(ans, sts):      # 营收对到了其他收入
            ans["income"]["revenue"] = ids_for(sts["income"]["lines"], [("Other income", None)])

        def bad_ids(ans, sts):
            ans["income"]["net_income"] = [999]

        def bad_unit(ans, sts):           # 单位不在表头原文里
            ans["unit"] = "RMB million"

        def half_assets(ans, sts):        # 总资产只给了流动资产小计：资产 ≠ 负债 + 权益
            ans["balance"]["total_assets"] = ids_for(sts["balance"]["lines"], [("[小计] Current assets", None)])

        def wrong_cost(ans, sts):         # 营业成本对到了研发费用（行名不符合）
            ans["income"]["cost_of_sales"] = ids_for(sts["income"]["lines"], [("R&D expenses", None)])

        for tweak, key in ((wrong_rnd, "rnd"), (wrong_revenue, "revenue"), (bad_ids, "不存在"), (bad_unit, "单位"),
                           (half_assets, "资产 ≠ 负债 + 权益"), (wrong_cost, "cost_of_sales")):
            d, stmts, vals, probs = self.run_gpt("innovent_2026h1_ann", FakeGPT(tweaks=[tweak]))
            self.assertTrue(probs and any(key in p for p in probs), (tweak.__name__, probs))

    def test_guard_comprehensive_attribution(self):
        line = {"label": "Owners of the Company", "section": "Total comprehensive income for the period attributable to:"}
        self.assertIn("综合收益", P.guard_problem("net_income_parent", line))
        self.assertIsNone(P.guard_problem("net_income_parent", dict(line, section="Profit for the period attributable to:")))
        self.assertIsNotNone(P.guard_problem("total_debt", {"label": "Lease liabilities", "section": "Current liabilities"}))
        self.assertIsNone(P.guard_problem("cash", {"label": "Bank balances and cash", "section": "Current assets"}))
        self.assertIsNotNone(P.guard_problem("cash", {"label": "Restricted cash", "section": "Current assets"}))
        # 药明生物 2023 年报表的小标题是单数 “Non-current Asset”
        self.assertIsNone(P.guard_problem("total_assets", {"label": "[小计] Non-current Asset", "section": "Non-current Asset"}))
        self.assertIsNotNone(P.guard_problem("total_assets", {"label": "Total Assets less Current Liabilities", "section": ""}))
        self.assertIsNone(P.guard_problem("total_liabilities", {"label": "[小计] Current Liabilities", "section": "Current Liabilities"}))


# ---------------------------------------------------------------- 端到端：发现报告 → 处理 → 统一格式的行
class FakeSource:
    """代替 cninfo / hkex 模块：discover 返回固定样本的报告。"""

    def __init__(self, names, style, market):
        self.names, self.STYLE, self.MARKET, self.SOURCE_ZH = names, style, market, "测试来源"

    def make_http(self, ctx):
        return None

    def discover(self, company, ctx, http, state, res):
        docs = []
        for n in self.names:
            d = load_fixture(n)[0]
            docs.append({k: d[k] for k in ("id", "title", "url", "published", "fiscal_year", "months", "kind")})
        return docs


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "state"))
        self.saved = (P.pdf_pages, P.pdf_layout)
        by_url = {}
        for n in ("hengrui_2026h1", "hengrui_2026q3", "innovent_2025fy_ann", "innovent_2026h1_ann"):
            d, pages, layout = load_fixture(n)
            by_url[d["url"]] = (pages, layout)
        P.pdf_pages = lambda http, url: by_url[url][0]
        P.pdf_layout = lambda http, url, nums: {k: v for k, v in by_url[url][1].items() if k in nums}
        self.reg = RG.load(ROOT)

    def tearDown(self):
        P.pdf_pages, P.pdf_layout = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ctx(self, gpt=None):
        return Context(self.tmp, CFG, today=dt.date(2026, 11, 22), gpt=gpt)

    def write(self, co, res):
        st = UnifiedStore(os.path.join(self.tmp, "data", co["id"]))
        for name, rows in res.rows.items():
            st.replace(name, rows)
        self.assertEqual(st.problems(), [])
        return st

    def test_hengrui(self):
        co = RG.get(self.reg, "hengrui")
        src = FakeSource(["hengrui_2026h1", "hengrui_2026q3"], "cas", "A")
        res = P.run_update(co, self.ctx(), src)
        self.assertTrue(res.ok, res.errors)
        st = self.write(co, res)
        inc = {(r["fiscal_year"], r["fiscal_period"]): r for r in st.rows["income"]}
        self.assertEqual(inc[("2026", "H1")]["basis"], "reported")
        q3 = inc[("2026", "Q3")]
        self.assertEqual((q3["basis"], q3["months"], q3["period_start"], q3["period_end"]), ("derived", "3", "2026-07-01", "2026-09-30"))
        self.assertAlmostEqual(float(q3["revenue"]), 24408.76272419 - 15455.58372811, places=5)
        self.assertAlmostEqual(float(q3["net_income"]), 6923.15405806 - 4465.28241988, places=5)
        self.assertEqual(q3["eps_diluted"], "")                    # 每股收益不推算
        self.assertIn(" − ", q3["source"])
        self.assertEqual(inc[("2025", "H1")]["source"], "http://static.cninfo.com.cn/finalpage/2026-08-20/1225483600.PDF（上年同期比较数）")
        bal = {r["period_end"]: r for r in st.rows["balance"]}
        self.assertEqual(sorted(bal), ["2025-12-31", "2026-06-30", "2026-09-30"])
        self.assertEqual(bal["2026-06-30"]["fiscal_period"], "Q2")
        self.assertAlmostEqual(float(bal["2026-06-30"]["cash_and_sti"]), 38734.32427837 + 107.45916228, places=5)
        cf = {(r["fiscal_year"], r["fiscal_period"]): r for r in st.rows["cashflow"]}
        self.assertAlmostEqual(float(cf[("2026", "H1")]["fcf"]), 1987.00387234 - 1577.50532286, places=5)
        recs = P.load_records(self.ctx(), "hengrui")
        self.assertEqual(sorted(recs), ["2026-9M", "2026-H1"])
        self.assertEqual(recs["2026-H1"]["mapping_source"], "A 股统一格式对照表")
        # 第二次运行：没有新报告，不重新处理，结果相同
        res2 = P.run_update(co, self.ctx(), src)
        self.assertTrue(res2.ok)
        self.assertEqual(res2.rows["income"], res.rows["income"])

    def test_innovent_with_gpt(self):
        co = RG.get(self.reg, "innovent")
        src = FakeSource(["innovent_2025fy_ann", "innovent_2026h1_ann"], "ifrs", "HK")
        res = P.run_update(co, self.ctx(), src)                     # 没有 GPT：暂缓，不算错
        self.assertTrue(res.ok)
        self.assertTrue(any("GPT" in w for w in res.warnings), res.warnings)
        self.assertEqual(res.rows, {})
        gpt = FakeGPT()
        res = P.run_update(co, self.ctx(gpt), src)
        self.assertTrue(res.ok, res.errors)
        self.assertEqual(len(gpt.calls), 2)                       # 两份公告版式不同，各问一次
        self.assertNotIn("cashflow", gpt.calls[0]["statements"])
        st = self.write(co, res)
        inc = {(r["fiscal_year"], r["fiscal_period"]): r for r in st.rows["income"]}
        self.assertEqual(sorted(inc), [("2024", "FY"), ("2025", "FY"), ("2025", "H1"), ("2025", "H2"), ("2026", "H1")])
        h2 = inc[("2025", "H2")]
        self.assertEqual(h2["basis"], "derived")
        self.assertAlmostEqual(float(h2["revenue"]), 13041.523 - 5953.094, places=6)
        self.assertAlmostEqual(float(h2["net_income"]), 813.565 - 834.321, places=6)
        self.assertEqual(inc[("2026", "H1")]["operating_income"], "")    # 报表没印营业利润：留空
        bal = {r["period_end"]: r for r in st.rows["balance"]}
        self.assertEqual({k: v["fiscal_period"] for k, v in bal.items()}, {"2024-12-31": "FY", "2025-12-31": "FY", "2026-06-30": "H1"})
        self.assertAlmostEqual(float(bal["2025-12-31"]["total_assets"]), 37347.839, places=6)
        # 再运行一次：版式相同的报告不再问 GPT
        gpt2 = FakeGPT()
        P.run_update(co, self.ctx(gpt2), src)
        self.assertEqual(gpt2.calls, [])
        # 核对规则改了（CHECK_VERSION）：用存下的报表行和原有的对照重新核对，不问 GPT，结果不变
        saved = P.CHECK_VERSION
        try:
            P.CHECK_VERSION = saved + 1
            gpt3 = FakeGPT()
            res3 = P.run_update(co, self.ctx(gpt3), src)
            self.assertEqual(gpt3.calls, [])
            self.assertTrue(res3.ok, res3.errors)
            self.assertEqual(res3.rows["income"], res.rows["income"])
            self.assertTrue(all(r["check_version"] == saved + 1 for r in P.load_records(self.ctx(), "innovent").values()))
        finally:
            P.CHECK_VERSION = saved

    def test_partial_reuse(self):
        """版式和以前某份报告相同的表沿用对照，其余的表才交给 GPT；合并后沿用的对照不能被冲掉。"""
        co = RG.get(self.reg, "innovent")
        src = FakeSource(["innovent_2026h1_ann"], "ifrs", "HK")
        P.run_update(co, self.ctx(FakeGPT()), src)
        recs = P.load_records(self.ctx(), "innovent")
        rec = copy.deepcopy(recs["2026-H1-ann"])
        rec.update(doc="2026-H1-rep", kind="report", url=rec["url"] + "#report", published="2026-09-29")
        for k in ("mapping", "mapping_labels", "signatures", "values", "status", "problems"):
            rec.pop(k, None)                                   # 新拆出来的报告还没有对照
        rec["statements"]["cashflow"] = {"pages": [70], "header": ["RMB'000 RMB'000"], "unit_text": "RMB'000 RMB'000", "eps_cents": False, "bad": [],
                                         "lines": [["Net cash from operating activities", "", "3,588,341", "1,822,170"],
                                                   ["Purchase of property, plant and equipment", "", "(184,553)", "(150,205)"],
                                                   ["Net cash used in investing activities", "", "(1,000,000)", "(500,000)"],
                                                   ["Net cash used in financing activities", "", "(100,000)", "(200,000)"],
                                                   ["Net increase in cash and cash equivalents", "", "2,488,341", "1,122,170"],
                                                   ["Cash and cash equivalents at end of the period", "", "9,999,999", "8,888,888"]]}
        spec = dict(INNOVENT_MAP, cashflow={"cfo": [("Net cash from operating activities", None)],
                                            "capex": [("Purchase of property, plant and equipment", None)]})
        gpt = FakeGPT(spec=spec)
        out = P.finish_record(rec, co, self.ctx(gpt), recs)
        self.assertEqual(out["status"], "ok", out.get("problems"))
        self.assertEqual(list(gpt.calls[0]["statements"]), ["cashflow"])
        self.assertIn("沿用 2026-H1-ann", out["mapping_source"])
        self.assertAlmostEqual(out["values"]["income"]["cur"]["revenue"], 8617.801, places=6)
        self.assertAlmostEqual(out["values"]["cashflow"]["cur"]["capex"], 184.553, places=6)

    def test_gpt_retry_and_failure(self):
        co = RG.get(self.reg, "innovent")
        src = FakeSource(["innovent_2026h1_ann"], "ifrs", "HK")

        def wrong(ans, sts):
            ans["income"]["rnd"] = ids_for(sts["income"]["lines"], [("Selling and marketing expenses", None)])
        gpt = FakeGPT(tweaks=[wrong])                               # 第一次错、第二次对
        res = P.run_update(co, self.ctx(gpt), src)
        self.assertTrue(res.ok, res.errors)
        self.assertEqual(len(gpt.calls), 2)
        self.assertTrue(gpt.calls[1].get("feedback"))
        rec = P.load_records(self.ctx(), "innovent")["2026-H1-ann"]
        self.assertIn("第 2 次", rec["mapping_source"])
        shutil.rmtree(os.path.join(self.tmp, "state"))
        gpt = FakeGPT(tweaks=[wrong, wrong])                        # 两次都错：这份报告不写入，记为需要处理
        res = P.run_update(co, self.ctx(gpt), src)
        self.assertFalse(res.ok)
        self.assertTrue(any("没通过核对" in e["detail"] for e in res.errors), res.errors)
        self.assertEqual(res.rows, {})

    def test_consistency(self):
        co = RG.get(self.reg, "hengrui")
        P.run_update(co, self.ctx(), FakeSource(["hengrui_2026h1", "hengrui_2026q3"], "cas", "A"))
        recs = P.load_records(self.ctx(), "hengrui")
        self.assertEqual(P.consistency(recs, 12)[0], [])
        bad = copy.deepcopy(recs)
        bad["2026-9M"]["values"]["balance"]["prior"]["total_assets"] += 5          # 上年末总资产和半年报写的不一样
        probs, notes, warns = P.consistency(bad, 12, fresh={"2026-9M"})
        self.assertTrue(probs and "没有重述说明" in probs[0], probs)
        bad["2026-9M"]["restatement_hint"] = "因会计政策变更，追溯调整上年同期数"
        probs, notes, warns = P.consistency(bad, 12, fresh={"2026-9M"})
        self.assertEqual(probs, [])
        self.assertTrue(notes and warns)
        bad["2026-9M"]["values"]["balance"]["prior"]["receivables"] = 1.0         # 非核心科目对不上只列出来
        bad["2026-9M"]["restatement_hint"] = ""
        bad["2026-9M"]["values"]["balance"]["prior"]["total_assets"] -= 5
        probs, notes, warns = P.consistency(bad, 12)
        self.assertEqual(probs, [])
        self.assertTrue(any("receivables" in n for n in notes))

    def test_restatement_phrases(self):
        self.assertTrue(P.RESTATE.search("注:因会计政策变更,追溯调整上年同期营业成本以及毛利率。"))
        self.assertTrue(P.RESTATE.search("(1). 重要会计政策变更 √适用 □不适用"))
        self.assertTrue(P.RESTATE.search("2025 (restated)"))
        self.assertFalse(P.RESTATE.search("公司是否需追溯调整或重述以前年度会计数据 □是 否"))
        self.assertFalse(P.RESTATE.search("本期发生同一控制下企业合并的，被合并方在合并前实现的净利润为：0 元"))


class TestRegistry(unittest.TestCase):
    def test_batch1_cn(self):
        reg = RG.load(ROOT)
        self.assertEqual(RG.problems(reg), [])
        from agent import adapters
        self.assertIs(adapters.get("hkex"), hkex)
        self.assertIs(adapters.get("cninfo"), cninfo)
        for cid in ("innovent", "akeso", "wuxibio", "xtalpi"):
            self.assertTrue(hkex.hk_listing(RG.get(reg, cid)).get("hkex_stock_id"), cid)
        for cid in ("hengrui", "mindray", "wuxiapptec"):
            self.assertTrue(cninfo.a_listing(RG.get(reg, cid)), cid)


if __name__ == "__main__":
    unittest.main()
