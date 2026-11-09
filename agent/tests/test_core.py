"""多公司框架：公司清单、统一数据格式、公司循环（出错隔离）、网站生成、GPT 预算。不访问网络。"""
import copy
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from agent import adapters  # noqa: E402
from agent.adapters import lilly_deep  # noqa: E402
from agent.adapters.base import Budget, Result  # noqa: E402
from agent.core import registry as RG  # noqa: E402
from agent.core import run as CR  # noqa: E402
from agent.core import schema as S  # noqa: E402
from agent.core.site import build as build_site  # noqa: E402
from agent.core.store import UnifiedStore  # noqa: E402

SEED = os.path.join(ROOT, "agent", "tests", "fixtures", "seed")


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


class TestRegistry(unittest.TestCase):
    def test_real_registry_is_valid(self):
        reg = RG.load(ROOT)
        self.assertEqual(RG.problems(reg), [])
        self.assertEqual(RG.get(reg, "lly")["status"], "active")
        self.assertEqual(len(reg["companies"]), 77)
        self.assertEqual(len([c for c in reg["companies"] if c["batch"] == 1]), 29)
        self.assertEqual(RG.ticker(RG.get(reg, "hengrui")), "600276 / 01276.HK")

    def test_catches_errors(self):
        reg = copy.deepcopy(RG.load(ROOT))
        bad = dict(RG.get(reg, "innovent"), id="Bad Id", listings=[{"exchange": "HKEX", "code": "1801"}], categories=["nope"])
        reg["companies"].append(bad)
        reg["companies"].append(dict(RG.get(reg, "hengrui"), id="hengrui2", listings=[{"exchange": "SSE", "code": "600276"}]))
        text = " | ".join(RG.problems(reg))
        for key in ("id 只能是", "分类不在清单里", "上市代码不对", "巨潮 orgId"):
            self.assertIn(key, text)


class TestUnified(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_lilly_conversion(self):
        rows = lilly_deep.to_unified(SEED)
        st = UnifiedStore(self.tmp)
        for name, rs in rows.items():
            st.replace(name, rs)
        self.assertEqual(st.problems(), [])
        q3 = [r for r in st.rows["income"] if r["fiscal_year"] == "2026" and r["fiscal_period"] == "Q3"][0]
        self.assertEqual((q3["revenue"], q3["gross_profit"], q3["eps_diluted"], q3["months"]), ("24397", "20464", "7.22", "3"))
        self.assertEqual(len([r for r in st.rows["income"] if r["fiscal_period"] == "FY"]), 5)
        self.assertEqual({r["axis"] for r in st.rows["segments"]}, {"product", "geography"})
        self.assertEqual(st.save(), ["income", "balance", "cashflow", "segments"])
        self.assertEqual(st.save(), [])          # 内容不变就不改文件

    def test_schema_checks(self):
        row = {k: None for k in S.FILES["income"]}
        row.update({"fiscal_year": "2026", "fiscal_period": "Q3", "months": 3, "currency": "USD", "unit": "millions", "basis": "reported",
                    "revenue": 100, "cost_of_sales": 30, "gross_profit": 60})
        probs = S.problems("income", [row])
        self.assertTrue(any("毛利" in p for p in probs))
        row.update({"gross_profit": 70, "fiscal_period": "H1"})
        self.assertTrue(any("6 个月" in p for p in S.problems("income", [row])))


def fake_adapter(ok=True, boom=False):
    class Fake:
        @staticmethod
        def update(company, ctx):
            if boom:
                raise RuntimeError("解析器崩溃")
            res = Result(company["id"])
            res.rows = {"income": [{"fiscal_year": "2026", "fiscal_period": "Q3", "period_start": "2026-07-01", "period_end": "2026-09-30",
                                    "months": 3, "currency": "USD", "unit": "millions", "basis": "reported", "source": "test",
                                    "revenue": 100, "cost_of_sales": 30, "gross_profit": 70 if ok else 50}]}
            return res
    return Fake


class TestLoop(unittest.TestCase):
    """一家公司出错（勾稽不平 / 程序异常）不影响其他公司；出错的公司不写数据，退出码为 1。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for d in ("page", "agent/web"):
            shutil.copytree(os.path.join(ROOT, d), os.path.join(self.tmp, d))
        for f in ("agent/config.json", "agent/glossary.json"):
            shutil.copy(os.path.join(ROOT, f), os.path.join(self.tmp, f))
        reg = RG.load(ROOT)
        base = RG.get(reg, "jnj")
        reg["companies"] = [dict(base, id="good", status="active", source="sec"), dict(base, id="bad", status="active", source="hkex", listings=[{"exchange": "HKEX", "code": "01801"}]),
                            dict(base, id="boom", status="active", source="cninfo", listings=[{"exchange": "SSE", "code": "600276", "cninfo_org_id": "gssh0600276"}]),
                            dict(base, id="later", status="planned")]
        os.makedirs(os.path.join(self.tmp, "companies"))
        RG.save(self.tmp, reg)
        self.saved = dict(adapters.ADAPTERS)
        adapters.ADAPTERS.update({"sec": fake_adapter(), "hkex": fake_adapter(ok=False), "cninfo": fake_adapter(boom=True)})
        os.environ["DASH_QUIET"] = "1"

    def tearDown(self):
        adapters.ADAPTERS.clear()
        adapters.ADAPTERS.update(self.saved)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_isolation(self):
        code = CR.main(["auto", "--today", "2026-11-09"], root=self.tmp)
        self.assertEqual(code, 1)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "data", "good", "income.csv")))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "data", "good", "meta.json")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "data", "bad", "income.csv")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "data", "boom")))
        report = read(self.tmp, "agent", "output", "report.md")
        self.assertIn("统一格式核对没通过", report)
        self.assertIn("解析器崩溃", report)
        self.assertIn("1 家公司在清单里等待接入", report)
        home = read(self.tmp, "_site", "index.html")
        self.assertIn('href="c/good/"', home)


class TestSite(unittest.TestCase):
    def test_home_and_lilly_page(self):
        tmp = tempfile.mkdtemp()
        try:
            data = os.path.join(tmp, "data", "lly")
            shutil.copytree(SEED, data)
            st = UnifiedStore(data)
            for name, rs in lilly_deep.to_unified(data).items():
                st.replace(name, rs)
            st.save()
            reg = RG.load(ROOT)
            info = build_site(ROOT, os.path.join(tmp, "_site"), reg, "2026-11-09", data_root=os.path.join(tmp, "data"))
            self.assertEqual(info["pages"], ["index.html", "c/lly/index.html"])
            home = read(tmp, "_site", "index.html")
            for cat in reg["categories"]:
                self.assertIn(cat["en"].replace("&", "&amp;"), home)
            self.assertIn("恒瑞医药", home)
            self.assertIn("244.0", home)             # 礼来 2026Q3 营收 244.0 亿美元，由统一格式数据算出
            self.assertIn("+38.6%", home)
            page = read(tmp, "_site", "c", "lly", "index.html")
            self.assertIn('class="site-nav"', page)
            self.assertIn("window.__GLOSSARY__", page)
            self.assertTrue(os.path.exists(os.path.join(tmp, "_site", "data", "lly", "income.csv")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestBudget(unittest.TestCase):
    def test_budget(self):
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "global.json")
            b = Budget(p, {"gpt": {"monthly_budget_usd": 1}}, dt.date(2026, 11, 9))
            self.assertTrue(b.allows())
            c = b.add({"input_tokens": 400000, "output_tokens": 20000, "web_searches": 10})
            self.assertAlmostEqual(c, 0.5 + 0.2 + 0.1, places=6)
            b.add({"input_tokens": 200000, "output_tokens": 0, "web_searches": 0})
            self.assertFalse(b.allows())
            b.save()
            self.assertAlmostEqual(json.loads(read(p))["gpt_spend"]["2026-11"], 1.05, places=4)
            self.assertTrue(Budget(p, {"gpt": {"monthly_budget_usd": 1}}, dt.date(2026, 12, 1)).allows())   # 新的月份重新计
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
