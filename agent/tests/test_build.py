"""端到端检验组装逻辑：假装 2026Q3 是“新季度”，只用 SEC 申报文件里的表格重新算一遍，与看板里已核对的数据比较。"""
import copy
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from agent.pipeline import build as B  # noqa: E402
from agent.pipeline import extract as X  # noqa: E402
from agent.pipeline.datasets import Store  # noqa: E402
from agent.tests.test_extract import F  # noqa: E402

SEED = os.path.join(HERE, "fixtures", "seed")  # 2026Q3 时已核对数据的固定快照，测试不随 data/ 变化
CFG = json.load(open(os.path.join(ROOT, "agent", "config.json"), encoding="utf-8"))


def k8_2026q3():
    inc = X.parse_8k_income(F["8K-2026Q3"]["is"])
    return {"income": inc, "recons": X.parse_8k_recons(F["8K-2026Q3"]["gm"], F["8K-2026Q3"]["ni"], inc),
            "selected": X.parse_8k_selected(F["8K-2026Q3"]["selected"])}


def per_2026q3():
    t = F["2026Q3"]
    return {"form": "10-Q", "income": X.parse_income_statement(t["is"]), "bs": X.parse_balance_sheet(t["bs"]),
            "products": X.parse_products(t["prod"]), "products_ytd": X.parse_products(t["prod9m"]), "prv": X.parse_prv(t["prv"])}


def seed_bucketed(store, q, split, combined):
    """把看板里的细分产品按 SEC 口径归并：单列产品保留，其余并入各领域的“(combined)”。"""
    out = {}
    for r in store.rows["product_revenue"]:
        if r["quarter"] != q:
            continue
        name = r["product"] if r["product"] in split else combined[r["area"]]
        out[name] = round(out.get(name, 0) + float(r["total"]), 1)
    return out


class TestBuild2026Q3(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = Store(SEED)
        cls.res = B.build_quarter(cls.store, "2026Q3", k8_2026q3(), per_2026q3(), cfg=CFG)

    def test_all_checks_pass(self):
        self.assertTrue(self.res.ok, self.res.failed())
        self.assertEqual([c["name"] for c in self.res.failed()], [])

    def test_income_matches_seed(self):
        new, old = self.res.rows["income_statement"][0], self.store.row("income_statement", quarter="2026Q3")
        for k in B.IS_FIELDS + B.NG_FIELDS:
            self.assertAlmostEqual(new[k], float(old[k]), places=2, msg=k)

    def test_annual_matches_seed(self):
        rows = {(r["year"], r["months"]): r for r in self.res.rows["income_annual"]}
        for key in (("2026", "9"), ("2025", "9")):
            old = self.store.row("income_annual", year=key[0], months=key[1])
            for k in B.IS_FIELDS + B.NG_FIELDS:
                self.assertAlmostEqual(rows[key][k], float(old[k]), places=2, msg="%s %s" % (key, k))

    def test_balance_matches_seed(self):
        new, old = self.res.rows["balance_sheet"][0], self.store.row("balance_sheet", quarter="2026Q3")
        for k, v in old.items():
            if k not in ("quarter", "period_end"):
                self.assertAlmostEqual(new[k], float(v), places=1, msg=k)

    def test_products(self):
        rows = {r["product"]: r for r in self.res.rows["product_revenue"]}
        self.assertEqual((rows["Mounjaro"]["us"], rows["Mounjaro"]["intl"], rows["Mounjaro"]["total"]), (4998, 5725, 10723))
        self.assertIsNone(rows["Foundayo"]["us"])
        self.assertEqual(rows["Foundayo"]["total"], 184)
        split = {p for p, r in rows.items() if not p.endswith("(combined)")}
        want = seed_bucketed(self.store, "2026Q3", split, CFG["combined_bucket_names"])
        got = {p: r["total"] for p, r in rows.items()}
        self.assertEqual(set(got), set(want))
        for p in want:
            self.assertAlmostEqual(got[p], want[p], places=1, msg=p)
        self.assertEqual(rows["Other (combined)"]["us"], 57)  # 没有拆出产品的领域保留美国/国际拆分

    def test_price_volume(self):
        rows = {r["region"]: r for r in self.res.rows["price_volume"]}
        for region in ("U.S.", "Total Revenue"):
            old = self.store.row("price_volume", quarter="2026Q3", region=region)
            for k in ("revenue", "price", "rate", "volume", "total_change"):
                self.assertAlmostEqual(rows[region][k], float(old[k]), places=4, msg="%s %s" % (region, k))
        self.assertEqual(rows["Outside U.S."]["revenue"], 9261)
        self.assertEqual(rows["Outside U.S."]["volume"], 0.76)

    def test_price_volume_annual(self):
        rows = {r["region"]: r for r in self.res.rows["price_volume_annual"]}
        old = self.store.row("price_volume_annual", year="2026", months="9", region="U.S.")
        for k in ("revenue", "price", "rate", "volume", "total_change"):
            self.assertAlmostEqual(rows["U.S."][k], float(old[k]), places=4, msg=k)

    def test_apply_round_trip(self):
        """删掉 2026Q3 后用 SEC 数据补回：利润表、年度利润表、资产负债表三个文件逐字节不变。"""
        s = copy.deepcopy(self.store)
        for name in ("income_statement", "balance_sheet"):
            s.rows[name] = [r for r in s.rows[name] if r["quarter"] != "2026Q3"]
        B.apply(s, self.res)
        d = tempfile.mkdtemp()
        s.save(d)
        for name in ("income_statement", "income_annual", "balance_sheet"):
            with open(os.path.join(SEED, name + ".csv"), encoding="utf-8") as a, open(os.path.join(d, name + ".csv"), encoding="utf-8") as b:
                self.assertEqual(a.read(), b.read(), name)


class TestQ4Logic(unittest.TestCase):
    """第四季度 = 全年（10-K）− 前三季度（取第三季度 10-Q 的“上年同期”列，即 2025 年前三季度）。"""

    def test_q4_products_and_regions(self):
        store = Store(SEED)
        nine = X.parse_products(F["2026Q3"]["prod9m"])
        prior_nine = {"current": nine["prior"], "revenue": {"current": nine["revenue"]["prior"]}}
        fy = X.parse_products(F["2025Q4"]["prod"], annual=True)
        res = B.Result("2025Q4")
        prods = B._quarter_products(res, {"products": fy}, prior_nine, "2025Q4")
        self.assertTrue(res.ok, res.failed())
        seed = {r["product"]: r for r in store.rows["product_revenue"] if r["quarter"] == "2025Q4"}
        for name in ("Mounjaro", "Zepbound", "Trulicity", "Verzenio", "Taltz", "Jardiance"):
            v = [k for k in prods if k[1] == name][0]
            for i, col in enumerate(("us", "intl", "total")):
                self.assertAlmostEqual(prods[v][i], float(seed[name][col]), delta=1.01, msg="%s %s" % (name, col))
        rev = [round(fy["revenue"]["current"][i] - nine["revenue"]["prior"][i], 1) for i in range(3)]
        self.assertEqual(rev[0], float(store.row("price_volume", quarter="2025Q4", region="U.S.")["revenue"]))
        self.assertEqual(rev[2], float(store.row("price_volume", quarter="2025Q4", region="Total Revenue")["revenue"]))


if __name__ == "__main__":
    unittest.main()
