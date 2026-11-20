"""美股适配器（agent/adapters/sec.py、sec_facts.py、sec_xbrl.py）。不访问网络：
固定样本在 agent/tests/fixtures/sec/，是 SEC 真实返回的数据（companyfacts、申报清单、代码清单、10-K/10-Q 的 XBRL 实例和链接库）
裁剪出来的：companyfacts 只留映射用到的科目和 2020 年中以后的数，实例只留本期的收入、营业成本事实和用到的上下文、成员标签。"""
import csv
import datetime as dt
import glob
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from agent.adapters import sec as A  # noqa: E402
from agent.adapters import sec_facts as F  # noqa: E402
from agent.adapters import sec_xbrl as X  # noqa: E402
from agent.adapters.base import Context  # noqa: E402
from agent.core import registry as RG  # noqa: E402
from agent.core import run as CR  # noqa: E402
from agent.core import schema as S  # noqa: E402
from agent.pipeline.sec import SecError  # noqa: E402

FIX = os.path.join(ROOT, "agent", "tests", "fixtures", "sec")


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


def read_csv(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def row(rows, fy, fp):
    return [r for r in rows if r["fiscal_year"] == fy and r["fiscal_period"] == fp][0]


def load_filing(folder):
    d = os.path.join(FIX, folder)
    with open(glob.glob(d + "/*_htm.xml")[0], "rb") as f:
        inst = X.Instance(f.read())
    labels, children = {}, {}
    for pat, fn in (("_lab.xml", X.parse_labels), ("_def.xml", X.parse_definitions)):
        p = glob.glob(d + "/*" + pat)
        if p:
            with open(p[0], "rb") as f:
                if pat == "_lab.xml":
                    labels = fn(f.read())
                else:
                    children = fn(f.read())
    return inst, labels, children


class FakeSEC:
    """把 SEC 的网址对应到固定样本。没有样本的申报目录当作空目录（没有 XBRL 实例）。"""
    CF = {59478: "lly_companyfacts.json", 318154: "amgn_companyfacts.json"}
    SUB = {318154: "amgn_submissions.json", 353278: "nvo_submissions.json"}

    def __init__(self):
        self.requests = []
        self.folders = {}
        for ij in glob.glob(os.path.join(FIX, "*", "index.json")):
            name = fixture(os.path.relpath(ij, FIX))["directory"]["name"]
            self.folders[name.rstrip("/").split("/")[-1]] = os.path.dirname(ij)

    def get(self, url, ttl=None):
        self.requests.append(url)
        m = re.search(r"/Archives/edgar/data/\d+/(\d+)/(.+)$", url)
        if m:
            d = self.folders.get(m.group(1))
            if d is None:
                return b'{"directory": {"item": []}}'
            with open(os.path.join(d, m.group(2)), "rb") as f:
                return f.read()
        return json.dumps(self.json(url)).encode()

    def json(self, url, ttl=None):
        if not url.endswith("index.json") or "/Archives/" not in url:
            self.requests.append(url)
        if url.endswith("company_tickers.json"):
            return fixture("company_tickers.json")
        m = re.search(r"CIK(\d{10})\.json$", url)
        if m and "/submissions/" in url and int(m.group(1)) in self.SUB:
            return fixture(self.SUB[int(m.group(1))])
        if m and "/companyfacts/" in url and int(m.group(1)) in self.CF:
            return fixture(self.CF[int(m.group(1))])
        if "/Archives/" in url:
            return json.loads(self.get(url).decode())
        raise SecError("样本里没有：%s" % url)


class FakeGPT:
    available = True

    def __init__(self, data):
        self.data, self.usage, self.calls = data, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "web_searches": 0}, []

    def respond(self, instructions, user_input, **kw):
        self.calls.append(json.loads(user_input))
        self.usage["calls"] += 1
        return {"data": self.data(self.calls[-1]) if callable(self.data) else self.data}


class TestStatements(unittest.TestCase):
    """companyfacts -> 三张表。"""

    def test_lilly_matches_verified_data(self):
        """礼来：SEC 标准科目算出的数与 data/lly 里已核对的数一致（这就是适配器 backtest 的标准答案）。"""
        built = F.build_statements(F.Facts(fixture("lly_companyfacts.json")), 2021)
        rows, tols = F.split_tol(built.rows)
        for name in ("income", "balance", "cashflow"):
            self.assertEqual(S.problems(name, rows[name]), [], name)
        ref = {n: read_csv(os.path.join(ROOT, "data", "lly", n + ".csv")) for n in ("income", "balance")}
        cmp = F.compare_reference(rows, tols, ref)
        self.assertEqual(cmp["mismatches"], [])
        self.assertGreater(cmp["core_compared"], 200)
        q1 = row(rows["income"], "2021", "Q1")
        self.assertEqual((q1["revenue"], q1["gross_profit"]), (6805.6, 4927.0))     # 毛利 = 营收 − 营业成本（礼来没单列）
        self.assertAlmostEqual(q1["operating_income"], 1155.3, places=6)          # 营业利润 = 税前利润 − 营业外收支合计
        q4 = row(rows["income"], "2024", "Q4")
        fy, = [r for r in rows["income"] if r["fiscal_year"] == "2024" and r["fiscal_period"] == "FY"]
        nine = sum(row(rows["income"], "2024", q)["revenue"] for q in ("Q1", "Q2", "Q3"))
        self.assertEqual(q4["basis"], "derived")
        self.assertAlmostEqual(q4["revenue"], fy["revenue"] - nine, places=6)
        self.assertIn(" − ", q4["source"])
        self.assertIsNone(q4["eps_diluted"])           # 第四季度每股收益不能相减得到，10-K 没单列就留空
        cash = row(rows["cashflow"], "2024", "Q2")
        self.assertEqual(cash["basis"], "derived")     # 现金流单季 = 年初至今相减
        self.assertAlmostEqual(cash["fcf"], cash["cfo"] - cash["capex"], places=6)

    def test_fiscal_calendars(self):
        """财年：美敦力截至 4 月、Veeva 截至 1 月、强生 52/53 周（2022 财年截至 2023-01-01）。"""
        for cid, fy, end in (("mdt", "2026", "2026-04-24"), ("veev", "2026", "2026-01-31"), ("jnj", "2022", "2023-01-01")):
            built = F.build_statements(F.Facts(fixture("%s_companyfacts.json" % cid)), 2021)
            rows, _ = F.split_tol(built.rows)
            r = row(rows["income"], fy, "FY")
            self.assertEqual(r["period_end"], end, cid)
            self.assertEqual(S.problems("income", rows["income"]), [], cid)
        mdt = F.split_tol(F.build_statements(F.Facts(fixture("mdt_companyfacts.json")), 2021).rows)[0]["income"]
        self.assertEqual(row(mdt, "2027", "Q1")["period_end"], "2026-07-31")     # 2027 财年第一季度（截至 2026 年 7 月）
        self.assertEqual(row(mdt, "2022", "Q1")["period_start"], "2021-05-01")
        self.assertEqual(A.nominal_month("2023-01-01"), 12)
        self.assertEqual(A.nominal_month("2026-04-24"), 4)

    def test_restatements_and_after_tax_items(self):
        """强生：2022 年中剥离消费者健康业务（改列终止经营）——2022 用首次申报的一套数，2023 用重述后的；
        净利润含终止经营损益的期间，所得税留空（统一格式没有税后项目的列），并在说明里列出。"""
        built = F.build_statements(F.Facts(fixture("jnj_companyfacts.json")), 2021)
        rows, _ = F.split_tol(built.rows)
        self.assertEqual(S.problems("income", rows["income"]), [])
        self.assertEqual(built.errors, [])
        self.assertEqual(row(rows["income"], "2022", "Q1")["revenue"], 23426.0)      # 首次申报（含消费者健康）
        self.assertEqual(row(rows["income"], "2022", "FY")["revenue"], 94943.0)
        self.assertEqual(row(rows["income"], "2023", "Q1")["revenue"], 20894.0)      # 重述后（不含）
        q3 = row(rows["income"], "2023", "Q3")
        self.assertEqual(q3["net_income"], 26028.0)
        self.assertIsNone(q3["income_tax"])
        text = " ".join(built.notes)
        self.assertIn("2022 财年在年中有重述", text)
        self.assertIn("所得税留空：2023 Q1", text)
        self.assertEqual(row(rows["income"], "2022", "FY")["period_end"], "2023-01-01")

    def test_rounding_units(self):
        self.assertAlmostEqual(F.rounding_unit(6805600000), 0.1)
        self.assertAlmostEqual(F.rounding_unit(2225000000), 1.0)
        self.assertAlmostEqual(F.rounding_unit(943711000), 0.001)
        self.assertAlmostEqual(F.rounding_unit(1.49, "per_share"), 0.01)
        self.assertEqual(F.fiscal_label("2023-01-01"), 2022)
        self.assertEqual(F.fiscal_label("2026-01-31"), 2026)

    def test_reference_comparison_flags_differences(self):
        """回测确实能发现不一致：把已核对数据改掉一个数，比较结果里要有它。"""
        built = F.build_statements(F.Facts(fixture("lly_companyfacts.json")), 2021)
        rows, tols = F.split_tol(built.rows)
        ref = {n: read_csv(os.path.join(ROOT, "data", "lly", n + ".csv")) for n in ("income", "balance")}
        r = row(ref["income"], "2023", "Q2")
        r["revenue"] = str(float(r["revenue"]) + 5)
        cmp = F.compare_reference(rows, tols, ref)
        self.assertEqual(len(cmp["mismatches"]), 1)
        self.assertIn("2023 Q2 revenue", cmp["mismatches"][0])


class TestOperatingFromCalc(unittest.TestCase):
    """原文没单列营业利润（强生、默沙东、辉瑞、百时美施贵宝）时，按申报的计算链接库推算。
    样本 opcalc/ 是手写的小申报（不是 SEC 真实数据）：税前利润 = 营收 − 成本费用合计 + 利息收入 − 利息费用，
    成本费用合计里含公司自定义的“其他（收益）扣除净额”（营业外）和“其他营业费用”（营业内）；另有所得税附注里的“境内 + 境外”。"""

    def load(self):
        d = os.path.join(FIX, "opcalc")
        with open(os.path.join(d, "xco-20260930_htm.xml"), "rb") as f:
            inst = X.Instance(f.read())
        with open(os.path.join(d, "xco-20260930_lab.xml"), "rb") as f:
            labels = X.parse_labels(f.read())
        with open(os.path.join(d, "xco-20260930_cal.xml"), "rb") as f:
            calc = X.parse_calculations(f.read())
        return inst, labels, calc

    def test_derives_and_checks_each_period(self):
        inst, labels, calc = self.load()
        got, nonop = X.operating_from_calc(inst, calc, labels)
        # 10000 − 3000 − 2500 − 2000 − 200（其他营业费用留在营业利润里）
        self.assertAlmostEqual(got[("2026-07-01", "2026-09-30")] / 1e6, 2300.0)
        self.assertAlmostEqual(got[("2026-01-01", "2026-09-30")] / 1e6, 6900.0)    # 成本费用合计没有数：用组成项之和
        self.assertNotIn(("2025-07-01", "2025-09-30"), got)       # 组成项之和 1700 ≠ 税前利润 1800：不推算
        self.assertEqual(nonop, ["us-gaap:InterestExpenseNonoperating", "us-gaap:InvestmentIncomeInterest",
                                 "xco:OtherIncomeDeductionsNet"])

    def test_tax_note_alone_is_not_used(self):
        inst, labels, calc = self.load()
        only_tax = {r: t for r, t in calc.items() if "IncomeTaxes" in r}
        self.assertEqual(X.operating_from_calc(inst, only_tax, labels), ({}, []))

    def test_nonoperating_labels(self):
        lab = lambda t: {"x:A": {"http://www.xbrl.org/2003/role/label": t}}
        self.assertTrue(X.is_nonoperating("x:A", lab("Other (income)/expense, net")))
        self.assertTrue(X.is_nonoperating("x:A", lab("Other income (deductions) — net")))
        self.assertTrue(X.is_nonoperating("x:A", lab("Interest income and other")))
        self.assertTrue(X.is_nonoperating("x:A", lab("Total nonoperating income")))
        self.assertFalse(X.is_nonoperating("x:A", lab("Other operating expense, net")))
        self.assertFalse(X.is_nonoperating("x:A", lab("Restructuring and other charges")))
        self.assertFalse(X.is_nonoperating("us-gaap:OtherCostAndExpenseOperating", {}))
        self.assertTrue(X.is_nonoperating("us-gaap:OtherNonoperatingIncomeExpense", {}))

    def test_extract_operating_downloads_calc(self):
        sec = FakeSEC()
        got, nonop = A.extract_operating(sec, 1, "0000000001-26-000001")
        self.assertAlmostEqual(got[("2026-07-01", "2026-09-30")] / 1e6, 2300.0)
        self.assertTrue(any(u.endswith("_cal.xml") for u in sec.requests))

    def test_statement_marks_derived(self):
        """强生不列营业利润：申报里推算的数只用在同一份申报的那一行，出处注明“推算”，第四季度 = 全年 − 9 个月也注明。"""
        facts = F.Facts(fixture("jnj_companyfacts.json"))
        built = F.build_statements(facts, 2021)
        rows, _ = F.split_tol(built.rows)
        self.assertIsNone(row(rows["income"], "2025", "Q3")["operating_income"])
        extra = {}
        for p in ("Q3", "9M", "FY"):
            u = built.used[("2025", p)]
            pt = facts.pick(F.INCOME_MAP[7][1], (u["start"], u["end"]), "latest", u["accn"])
            extra.setdefault(u["accn"], {})[(u["start"], u["end"])] = (pt[0] + 500e6, "money")
        for accn, periods in extra.items():
            facts.add_instance(accn, "", "", {"calc:OperatingIncomeLoss": periods})
        built = F.build_statements(facts, 2021)
        rows, _ = F.split_tol(built.rows)
        q3, q4 = row(rows["income"], "2025", "Q3"), row(rows["income"], "2025", "Q4")
        self.assertAlmostEqual(q3["operating_income"], q3["pretax_income"] + 500)
        self.assertAlmostEqual(q4["operating_income"], q4["pretax_income"])          # 两边都 +500，相减抵掉
        self.assertIn("营业利润为推算", q3["source"])
        self.assertIn("营业利润为推算", q4["source"])
        self.assertNotIn("营业利润为推算", row(rows["income"], "2025", "Q1")["source"])
        self.assertIn("营业利润为推算", " ".join(built.notes))
        self.assertEqual(S.problems("income", rows["income"]), [])


class TestGroupHarmonize(unittest.TestCase):
    """收入构成分组跨期间统一（sec.harmonize_groups）。"""

    @staticmethod
    def rows(spec):
        out = []
        for fy, fp, end, member, group in spec:
            out.append({"fiscal_year": fy, "fiscal_period": fp, "period_end": end, "axis": "product", "member": member,
                        "name_en": member, "group": group, "value": 1.0})
        return out

    def test_renamed_groups_and_member_case(self):
        """美敦力：事业部改名（旧组的产品后来都归到同一个新组）；成员名只差大小写的统一成最近的写法。"""
        rows = self.rows([
            ("2021", "Q1", "2020-07-31", "CardiacRhythmandHeartFailure", "Cardiac & Vascular Group"),
            ("2021", "Q1", "2020-07-31", "CoronaryandStructuralHeart", "Cardiac & Vascular Group"),
            ("2022", "Q1", "2021-07-30", "CardiacRhythmAndHeartFailure", "Cardiovascular"),
            ("2022", "Q1", "2021-07-30", "Neuromodulation", "Neuroscience"),
            ("2021", "Q1", "2020-07-31", "Neuromodulation", "Restorative Therapies Group"),
        ])
        notes = A.harmonize_groups(rows, pharma=False)
        self.assertEqual({r["group"] for r in rows if r["member"].lower().startswith("cardiac")}, {"Cardiovascular"})
        self.assertEqual([r["group"] for r in rows if r["member"] == "CoronaryandStructuralHeart"], ["Cardiovascular"])
        self.assertEqual({r["member"] for r in rows if r["member"].lower().startswith("cardiac")}, {"CardiacRhythmAndHeartFailure"})
        self.assertEqual({r["group"] for r in rows if r["member"] == "Neuromodulation"}, {"Neuroscience"})
        self.assertIn("Cardiac & Vascular Group → Cardiovascular", " ".join(notes))

    def test_pharma_switching_schemes_keeps_areas_only(self):
        """百时美施贵宝：同一产品时而归治疗领域、时而归“在售产品 / 增长组合”——只留治疗领域，其余等 GPT 归类。"""
        rows = self.rows([
            ("2021", "FY", "2021-12-31", "Opdivo", "Oncology"),
            ("2022", "Q3", "2022-09-30", "Opdivo", "In-Line Products"),
            ("2025", "Q1", "2025-03-31", "Opdivo", "Growth Portfolio"),
            ("2025", "Q1", "2025-03-31", "Cobenfy", "Growth Portfolio"),
        ])
        A.harmonize_groups(rows, pharma=True)
        self.assertEqual({r["group"] for r in rows if r["member"] == "Opdivo"}, {"Oncology"})
        self.assertEqual([r["group"] for r in rows if r["member"] == "Cobenfy"], [""])

    def test_pharma_consistent_own_groups_kept(self):
        """吉利德一直按 HIV / 丙肝分组：不动。"""
        rows = self.rows([("2021", "Q1", "2021-03-31", "Biktarvy", "HIV"), ("2026", "Q1", "2026-03-31", "Biktarvy", "HIV"),
                          ("2026", "Q1", "2026-03-31", "Trodelvy", "Oncology")])
        self.assertEqual(A.harmonize_groups(rows, pharma=True), [])
        self.assertEqual({r["group"] for r in rows if r["member"] == "Biktarvy"}, {"HIV"})

    def test_case_variants_in_same_period_not_merged(self):
        rows = self.rows([("2026", "Q1", "2026-03-31", "Xa", "G"), ("2026", "Q1", "2026-03-31", "XA", "G")])
        A.harmonize_groups(rows, pharma=False)
        self.assertEqual(sorted(r["member"] for r in rows), ["XA", "Xa"])


class TestMemberRenames(unittest.TestCase):
    """成员改科目名（sec.member_renames / apply_renames）：Ionis 把 Eplontersen 改成 WAINUA、改正拼错的 WainusRoyalties。"""

    @staticmethod
    def filing(filed, per, members):
        return {"filed": filed, "period_end": per.split("|")[1], "periods": {per: {"product": [
            "x", "us-gaap:Revenue", sum(v for _, _, v in members),
            [[m, "ions:%sMember" % m, lab, "", v, 0.0005, "reported"] for m, lab, v in members]]}}}

    def filings(self):
        fy22 = "2022-01-01|2022-12-31"
        return {
            "a": self.filing("2023-02-22", fy22, [("EplontersenJointDevelopmentRevenue", "Eplontersen Joint Development Revenue", 76.787),
                                                   ("Pifeltro", "Pifeltro", 5.0), ("Spinraza", "SPINRAZA", 200.0)]),
            "b": self.filing("2024-02-21", fy22, [("WainuaJointDevelopmentRevenue", "WAINUA Joint Development Revenue", 76.787),
                                                   ("AllianceRevenueKoselugo", "Alliance revenue - Koselugo", 5.0), ("Spinraza", "SPINRAZA", 200.0)]),
            "c": self.filing("2025-10-29", "2025-07-01|2025-09-30", [("WainusRoyalties", "WAINUA Royalties", 13.32)]),
            "d": self.filing("2026-11-09", "2025-07-01|2025-09-30", [("WainuaRoyalties", "WAINUA Royalties", 13.32)]),
        }

    def test_renames_need_same_value_and_label(self):
        ren = A.member_renames(self.filings())["product"]
        self.assertEqual(ren["EplontersenJointDevelopmentRevenue"], "WainuaJointDevelopmentRevenue")
        self.assertEqual(ren["WainusRoyalties"], "WainuaRoyalties")
        self.assertNotIn("Pifeltro", ren)              # 数碰巧一样、名字完全不同：不是改名

    def test_apply_renames(self):
        rows = [{"fiscal_year": "2022", "fiscal_period": "FY", "period_end": "2022-12-31", "axis": "product",
                 "member": "EplontersenJointDevelopmentRevenue", "name_en": "Eplontersen Joint Development Revenue", "group": ""},
                {"fiscal_year": "2023", "fiscal_period": "FY", "period_end": "2023-12-31", "axis": "product",
                 "member": "WainuaJointDevelopmentRevenue", "name_en": "WAINUA Joint Development Revenue", "group": ""}]
        merged = A.apply_renames(rows, {"product": {"EplontersenJointDevelopmentRevenue": "WainuaJointDevelopmentRevenue"}})
        self.assertEqual(merged, [("product", "WainuaJointDevelopmentRevenue", ["EplontersenJointDevelopmentRevenue"])])
        self.assertEqual({(r["member"], r["name_en"]) for r in rows}, {("WainuaJointDevelopmentRevenue", "WAINUA Joint Development Revenue")})

    def test_both_names_in_same_period_not_merged(self):
        rows = [{"fiscal_year": "2022", "fiscal_period": "FY", "period_end": "2022-12-31", "axis": "product", "member": m,
                 "name_en": m, "group": ""} for m in ("Old", "New")]
        self.assertEqual(A.apply_renames(rows, {"product": {"Old": "New"}}), [])
        self.assertEqual(sorted(r["member"] for r in rows), ["New", "Old"])

    def test_label_members_uses_group_of_longest_name(self):
        """改名前后 GPT 各归过一次类（WAINUA Royalties：旧写法 罕见病、新写法 心血管代谢）：用覆盖期间多的那个。"""
        run = object.__new__(A.Run)
        run.state = {"names_zh": {"product|WainuaRoyalties": "WAINUA 特许权使用费"},
                     "product_groups": {"WainusRoyalties": "Rare Disease", "WainuaRoyalties": "Cardiometabolic Health"}}
        run.co = {"categories": ["rna"], "name_en": "Ionis"}
        run.seg = {"filings": {k: v for k, v in self.filings().items() if k in "cd"}}
        run.ctx = type("C", (), {"gpt": None})()
        run.res = type("R", (), {"notes": [], "warnings": []})()
        rows = [{"fiscal_year": "2025", "fiscal_period": q, "period_end": e, "axis": "product", "member": "WainusRoyalties",
                 "name_en": "WAINUA Royalties", "group": "", "name_zh": ""} for q, e in (("Q1", "2025-03-31"), ("Q2", "2025-06-30"))]
        rows.append({"fiscal_year": "2025", "fiscal_period": "Q3", "period_end": "2025-09-30", "axis": "product",
                     "member": "WainuaRoyalties", "name_en": "WAINUA Royalties", "group": "", "name_zh": ""})
        run.label_members(rows)
        self.assertEqual({(r["member"], r["group"]) for r in rows}, {("WainuaRoyalties", "Rare Disease")})


class TestBreakdown(unittest.TestCase):
    """XBRL 实例 -> 收入构成。"""

    def breakdown(self, folder):
        inst, labels, children = load_filing(folder)
        out, warns, pending = X.filing_breakdown(inst, labels, children, inst.dei["DocumentPeriodEndDate"])
        return out, warns, pending

    def assertSums(self, b):
        self.assertAlmostEqual(sum(m["value"] for m in b["members"]), b["total"], delta=0.5 * (len(b["members"]) + 1))

    def test_amgen_drops_product_sales_total(self):
        out, warns, pending = self.breakdown("amgn_10q")
        b = out[("2026-07-01", "2026-09-30")]["product"]
        self.assertIn("Product sales", b["method"])
        names = {m["name"] for m in b["members"]}
        self.assertIn("Prolia", names)
        self.assertNotIn("Product sales", names)
        self.assertEqual(b["total"], 10365.0)
        self.assertSums(b)

    def test_merck_scope_partial_and_nested(self):
        """默沙东：分部口径的数不当成合并总收入；两个分部之外的收入作为“其他（未单列）”；产品挂在分部下（组 = 分部）。"""
        out, _, _ = self.breakdown("mrk_10q")
        q = out[("2026-07-01", "2026-09-30")]
        seg = {m["member"]: m for m in q["segment"]["members"]}
        self.assertEqual(seg[X.OTHER_MEMBER]["value"], 141.0)
        self.assertEqual(seg[X.OTHER_MEMBER]["basis"], "derived")
        geo = [m["name"] for m in q["geography"]["members"]]
        self.assertNotIn("Non-US", geo)
        self.assertEqual(len(geo), 7)
        keytruda = [m for m in q["product"]["members"] if m["member"] == "Keytruda"][0]
        self.assertEqual((keytruda["group"], keytruda["value"]), ("Pharmaceutical", 7771.0))
        for fam in ("segment", "geography", "product"):
            self.assertSums(q[fam])

    def test_bms_needs_judgement(self):
        """百时美施贵宝：同一产品轴上叠着几种拆法，规则拆不开——列为待判定；GPT 提议的拆法要经过数值核对才采用。"""
        out, warns, pending = self.breakdown("bmy_10q")
        self.assertNotIn("product", out[("2026-07-01", "2026-09-30")])
        item = [p for p in pending if p["period"] == ("2026-07-01", "2026-09-30")][0]
        good = ["bmy:NetProductSalesMember", "bmy:AllianceAndOtherRevenuesMember", "bmy:AllianceRevenuesMember",
                "bmy:OtherRevenuesMember", "bmy:SalesRevenueGrossMember", "bmy:GrowthBrandsMember", "bmy:LegacyBrandsMember"]
        self.assertEqual(A.verify_aggregates(item, good), (True, ""))
        self.assertFalse(A.verify_aggregates(item, good[:-1])[0])
        self.assertFalse(A.verify_aggregates(item, ["bmy:NotAMember"])[0])
        inst, labels, children = load_filing("bmy_10q")
        out2, _, pending2 = X.filing_breakdown(inst, labels, children, "2026-09-30", exclude={"product": set(good)})
        b = out2[("2026-07-01", "2026-09-30")]["product"]
        self.assertIn("Eliquis", {m["name"] for m in b["members"]})
        self.assertSums(b)

    def test_custom_axes_and_definition_hierarchy(self):
        out, _, _ = self.breakdown("beone_10k")          # 百济神州：产品名在公司自定义的 ProductNameAxis 上
        names = [m["name"] for m in out[("2025-01-01", "2025-12-31")]["product"]["members"]]
        self.assertIn("BRUKINSA", " ".join(names).upper())
        out, _, _ = self.breakdown("gild_10q")            # 吉利德：定义链接库给出层级，组 = 最近的非泛称上级（Total HIV -> HIV）
        b = out[("2026-04-01", "2026-06-30")]["product"]
        self.assertEqual(b["method"], "定义链接库的末级成员")
        biktarvy = [m for m in b["members"] if "Biktarvy" in m["name"]][0]
        self.assertEqual(biktarvy["group"], "HIV")
        out, _, _ = self.breakdown("isrg_10q")            # 直觉外科：两个“服务”成员数值相同，不算两种拆法
        self.assertEqual(len(out[("2026-07-01", "2026-09-30")]["product"]["members"]), 3)

    def test_cost_of_sales_from_product_and_service(self):
        """赛默飞：营业成本只按“产品 / 服务”两个标准成员列示、没有合计——两者相加作为营业成本。"""
        inst, _, _ = load_filing("tmo_10q")
        cogs = X.plain_facts(inst)["CostOfGoodsAndServicesSold"]
        self.assertEqual(cogs[("2026-06-28", "2026-09-26")], (7006000000.0, "money"))

    def test_find_partition_rules(self):
        lab = {}

        def mem(**kw):
            return {k: (v, 0.5, "x:%sMember" % k) for k, v in kw.items()}
        sel, how, partial = X.find_partition(mem(A=60, B=40), 100, 0.5)
        self.assertEqual((sorted(sel), partial), (["A", "B"], False))
        sel, how, _ = X.find_partition(mem(US=60, International=40, Europe=25, Japan=15), 100, 0.5, labels=lab)
        self.assertEqual(sorted(sel), ["Europe", "Japan", "US"])                   # 去掉“International”合计
        sel, how, _ = X.find_partition(mem(A=50, B=50, C=50, D=50), 100, 0.5)
        self.assertIsNone(sel)                                                    # 多种拆法，判断不了
        self.assertIn("多种拆法", how)
        sel, how, partial = X.find_partition(mem(A=80, B=15), 100, 0.5)
        self.assertTrue(partial)                                                  # 只列了一部分（95%）
        sel, how, _ = X.find_partition(mem(A=60, B=20), 100, 0.5)
        self.assertIsNone(sel)                                                    # 不到九成，不接受


class TestQ4Breakdown(unittest.TestCase):
    """第四季度收入构成 = 全年 − 9 个月；两边成员不同时，独有成员的差额合并为“其他（未单列）”，差额为负或太大就不推算。"""

    @staticmethod
    def version(total, **members):
        return ["全部成员", "us-gaap:Revenues", total, [[k, "", k, "", v, 0.5, "reported"] for k, v in members.items()]]

    def test_same_members(self):
        q4, why = A._q4_members(self.version(100, A=70, B=30), self.version(75, A=50, B=25))
        self.assertEqual(({r[0]: r[4] for r in q4}, why), ({"A": 20, "B": 5}, ""))

    def test_new_member_goes_to_other(self):
        q4, why = A._q4_members(self.version(100, A=70, B=25, C=5), self.version(75, A=50, B=25))
        got = {r[0]: r[4] for r in q4}
        self.assertEqual(got, {"A": 20, "B": 0, X.OTHER_MEMBER: 5})
        self.assertAlmostEqual(sum(got.values()), 25)
        self.assertIn("C", why)

    def test_negative_or_large_difference_is_refused(self):
        q4, why = A._q4_members(self.version(100, A=70, B=30), self.version(75, A=40, C=35))
        self.assertIsNone(q4)
        q4, why = A._q4_members(self.version(100, A=50, D=50), self.version(75, A=40, C=35))
        self.assertIsNone(q4)


class TestAdapter(unittest.TestCase):
    """适配器接口：update / backtest / check / selftest（FakeSEC 提供固定样本）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        shutil.copytree(os.path.join(ROOT, "data", "lly"), os.path.join(self.tmp, "data", "lly"))
        os.makedirs(os.path.join(self.tmp, "state"))
        with open(os.path.join(ROOT, "agent", "config.json"), encoding="utf-8") as f:
            self.cfg = json.load(f)
        self.sec = FakeSEC()
        self.saved = A.make_client
        A.make_client = lambda ctx: self.sec
        A._REFERENCE.clear()
        self.reg = RG.load(ROOT)

    def tearDown(self):
        A.make_client = self.saved
        A._REFERENCE.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ctx(self, gpt=None):
        return Context(self.tmp, self.cfg, today=dt.date(2026, 11, 9), gpt=gpt)

    def test_update_writes_checked_rows_and_state(self):
        co = RG.get(self.reg, "amgn")
        ctx = self.ctx()
        res = A.update(co, ctx)
        self.assertTrue(res.ok, [c for c in res.checks if not c["ok"]])
        names = [c["name"] for c in res.checks]
        self.assertIn("礼来回测（解析器检验）", names)
        self.assertIn("收入构成之和 = 营收", names)
        self.assertTrue(res.replace)
        for name in ("income", "balance", "cashflow", "segments"):
            self.assertEqual(S.problems(name, res.rows[name]), [], name)
        self.assertEqual(row(res.rows["income"], "2026", "Q3")["revenue"], 10365.0)
        seg = [r for r in res.rows["segments"] if r["fiscal_year"] == "2026" and r["fiscal_period"] == "Q3" and r["axis"] == "product"]
        self.assertAlmostEqual(sum(r["value"] for r in seg), 10365.0, delta=0.5 * (len(seg) + 1))
        self.assertTrue(all(r["source"].startswith("SEC 10-Q ") for r in seg))
        CR.apply(co, res, ctx)                                   # 框架核对、写入
        self.assertTrue(res.ok, res.errors)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "data", "amgn", "segments.csv")))
        st = ctx.load_state("amgn")
        self.assertEqual((st["cik"], st["ticker"], st["form_type"]), ("0000318154", "AMGN", "10-K"))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "state", "amgn", A.SEG_FILE)))
        self.assertEqual(A.check(co, ctx), [])
        res2 = A.update(co, ctx)                                 # 没有新申报、程序没改：不动数据
        self.assertEqual((res2.ok, res2.rows), (True, {}))
        self.assertIn("没有新的 10-K/10-Q", " ".join(l for t, ls in res2.notes for l in ls))

    def test_foreign_issuer_is_skipped(self):
        res = A.update(RG.get(self.reg, "nvo"), self.ctx())
        self.assertTrue(res.ok)
        self.assertIn("第 5 阶段", res.skipped)
        self.assertEqual(self.ctx().load_state("nvo")["form_type"], "20-F")

    def test_unknown_ticker_and_network_errors_only_warn(self):
        co = dict(RG.get(self.reg, "amgn"), id="zzzz", listings=[{"exchange": "NASDAQ", "code": "ZZZZ"}])
        res = A.update(co, self.ctx())
        self.assertTrue(res.ok)
        self.assertIn("查不到 ZZZZ", " ".join(res.warnings))
        self.assertEqual(res.rows, {})

    def test_backtest_against_lilly(self):
        res = A.backtest(RG.get(self.reg, "lly"), self.ctx())
        self.assertTrue(res.ok, res.errors)
        detail = [c for c in res.checks if c["name"].startswith("礼来回测")][0]["detail"]
        self.assertIn("不一致 0 个", detail)
        res = A.backtest(RG.get(self.reg, "amgn"), self.ctx())
        self.assertTrue(res.ok, res.errors)
        self.assertTrue(self.ctx().load_state("amgn")["backtest"]["ok"])

    def test_gpt_labels_are_checked(self):
        items = [("product|Repatha", "product", "Repatha", True), ("product|Prolia", "product", "Prolia", True),
                 ("geography|US", "geography", "U.S.", False), ("product|Foo", "product", "Foo", True)]
        proposed = {"product|Repatha": {"name_zh": "瑞百安", "group": "Cardiovascular"},
                    "product|Prolia": {"name_zh": "Prolia", "group": "Bone health"},          # 组不在清单里
                    "geography|US": {"name_zh": "美国", "group": ""},
                    "product|Foo": {"name_zh": "a very long made up name that is clearly not chinese", "group": "Other"}}
        names, groups, bad = A.verify_labels(items, proposed, A.AREAS)
        self.assertEqual(names, {"product|Repatha": "瑞百安", "product|Prolia": "Prolia", "geography|US": "美国"})
        self.assertEqual(groups, {"product|Repatha": "Cardiovascular", "product|Foo": "Other"})
        self.assertEqual(bad, 2)

    def test_gpt_names_and_groups_once(self):
        def answer(req):
            return {"items": [{"id": i["id"], "name_zh": "中文" + i["name"][:3],
                               "group": ("Other" if "Other" in i["name"] else "Cardiovascular") if i["need_group"] else ""}
                              for i in req["items"]]}
        gpt = FakeGPT(answer)
        ctx = self.ctx(gpt)
        res = A.update(RG.get(self.reg, "amgn"), ctx)
        self.assertTrue(res.ok, res.errors)
        seg = res.rows["segments"]
        self.assertTrue(seg and all(r["name_zh"] for r in seg))
        self.assertTrue(all(r["group"] for r in seg if r["axis"] == "product"))
        calls = gpt.usage["calls"]
        st = ctx.load_state("amgn")
        self.assertTrue(st["names_zh"] and st["product_groups"])
        res2 = A.backtest(RG.get(self.reg, "amgn"), ctx)          # 以前判定过的直接用，不再问 GPT
        self.assertTrue(res2.ok, res2.errors)
        self.assertEqual(gpt.usage["calls"], calls)


class TestCompanySpecificLines(unittest.TestCase):
    """公司自定义科目：经核对的映射才用；推算的恒等关系。"""

    def facts(self):
        def fact(start, end, val, accn, form="10-Q", filed="2026-05-01"):
            return {"start": start, "end": end, "val": val, "accn": accn, "fy": 2026, "fp": "Q1", "form": form, "filed": filed}
        cf = {"cik": 1, "entityName": "X", "facts": {"us-gaap": {
            "Revenues": {"label": "", "units": {"USD": [fact("2025-01-01", "2025-12-31", 1000e6, "a-1", "10-K", "2026-02-01"),
                                                       fact("2026-01-01", "2026-03-31", 300e6, "a-2")]}},
            "GrossProfit": {"label": "", "units": {"USD": [fact("2026-01-01", "2026-03-31", 210e6, "a-2")]}},
        }}}
        f = F.Facts(cf)
        f.add_instance("a-1", "10-K", "2026-02-01", {"ext:x:CostOfProductSold": {("2025-01-01", "2025-12-31"): (400e6, "money")}})
        return f

    def test_identities_and_mapping(self):
        built = F.build_statements(self.facts(), 2021)
        rows, _ = F.split_tol(built.rows)
        q1 = row(rows["income"], "2026", "Q1")
        self.assertEqual(q1["cost_of_sales"], 90.0)                  # 营业成本 = 营收 − 毛利
        fy = row(rows["income"], "2025", "FY")
        self.assertIsNone(fy["cost_of_sales"])
        built = F.build_statements(self.facts(), 2021, {"cost_of_sales": ["ext:x:CostOfProductSold"]})
        fy = row(F.split_tol(built.rows)[0]["income"], "2025", "FY")
        self.assertEqual((fy["cost_of_sales"], fy["gross_profit"]), (400.0, 600.0))

    def test_plausibility(self):
        self.assertTrue(A.plausible("cost_of_sales", {"cost_of_sales": 40, "revenue": 100}))
        self.assertFalse(A.plausible("cost_of_sales", {"cost_of_sales": 140, "revenue": 100}))
        self.assertFalse(A.plausible("rnd", {"rnd": -3}))
        self.assertFalse(A.plausible("receivables", {"receivables": 50, "total_current_assets": 40}))


if __name__ == "__main__":
    unittest.main()
