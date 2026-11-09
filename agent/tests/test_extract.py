"""用真实申报文件里的表格（fixtures）检验抽取逻辑；期望值来自看板现有、已核对过的数据。"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from agent.pipeline import extract as X  # noqa: E402


def load():
    out = {}
    for name in ("sec_tables.json", "sec_tables_2.json"):
        with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as f:
            for k, v in json.load(f).items():
                out.setdefault(k, {}).update(v)
    return out


F = load()


class TestIncome(unittest.TestCase):
    def test_8k_income_q3(self):
        t = F["8K-2026Q3"]["is"]
        self.assertIs(X.find_8k_income([F["2026Q3"]["is"], t]), t)
        d = X.parse_8k_income(t)
        exp = dict(revenue=24397, cost_of_sales=3933, gross_margin=20464, rnd=4247, msa=3125, acquired_iprd=4181,
                   impairment_restructuring=303, operating_income=8608, other_net=-202, pretax_income=8406,
                   income_tax=1955, net_income=6451, eps_diluted=7.22, diluted_shares_k=893439)
        for k, v in exp.items():
            self.assertAlmostEqual(d[k][0], v, places=2, msg=k)
        self.assertEqual(len(d["revenue"]), 4)
        self.assertAlmostEqual(d["revenue"][2], 67170)
        self.assertAlmostEqual(d["other_net"][2], 2)
        self.assertAlmostEqual(d["diluted_shares_k"][2], 894371)

    def test_10q_income(self):
        t = F["2026Q3"]["is"]
        self.assertIs(X.find_income_statement([F["8K-2026Q3"]["is"], t]), t)
        d = X.parse_income_statement(t)
        self.assertAlmostEqual(d["operating_income"][0], 8608)
        self.assertAlmostEqual(d["other_net"][0], -202)
        self.assertAlmostEqual(d["eps_diluted"][0], 7.22)
        self.assertAlmostEqual(d["diluted_shares_k"][0], 893400)

    def test_10k_income(self):
        d = X.parse_income_statement(F["2025Q4"]["is"])
        self.assertEqual(d["revenue"], [65179, 45043, 34124])
        self.assertAlmostEqual(d["other_net"][0], -571)
        self.assertAlmostEqual(d["net_income"][0], 20640)
        self.assertAlmostEqual(d["eps_diluted"][0], 22.95)

    def test_recons(self):
        r = X.parse_8k_recons(F["8K-2026Q3"]["gm"], F["8K-2026Q3"]["ni"], X.parse_8k_income(F["8K-2026Q3"]["is"]))
        self.assertTrue(r["recon_ok"])
        self.assertEqual([i["level"] for i in r["excluded_items"]], ["operating", "operating", "other", "tax", "tax"])
        self.assertEqual(r["ng_gross_margin"][0], 20592)
        self.assertEqual(r["ng_net_income"][0], 6679)
        self.assertAlmostEqual(r["ng_eps_diluted"][0], 7.48)
        self.assertAlmostEqual(r["ng_eps_diluted"][2], 24.41)
        self.assertAlmostEqual(r["ng_operating_income"][0], 9039)


class TestBalance(unittest.TestCase):
    COLS = ["cash_and_sti", "accounts_receivable", "inventories", "other_current_assets", "total_current_assets", "investments",
            "goodwill_intangibles", "ppe", "other_noncurrent_assets", "total_assets", "short_term_debt", "accounts_payable",
            "sales_rebates", "other_current_liabilities", "total_current_liabilities", "long_term_debt",
            "other_noncurrent_liabilities", "total_liabilities", "total_equity", "total_liabilities_equity"]
    EXP = {
        "2026Q3": [4885, 22089, 18194, 20095, 65263, 4013, 27958, 32188, 18650, 148072, 2517, 5793, 24170, 14516, 46996, 46986, 13819, 107801, 40271, 148072],
        "2025Q4": [7268, 17760, 13744, 16857, 55629, 2802, 12419, 24675, 16951, 112476, 1635, 5379, 17382, 10832, 35228, 40868, 9845, 85941, 26535, 112476],
        "2023Q3": [2493.9, 8167.1, 4901.4, 7444.6, 23007, 2691.7, 10866.9, 11863.2, 9486.7, 57915.5, 2244.7, 2435.1, 11522.3, 5796.1, 21998.2, 17923.6, 6686.3, 46608.1, 11307.4, 57915.5],
    }

    def test_bs(self):
        for q, exp in self.EXP.items():
            t = F[q]["bs"]
            self.assertIs(X.find_balance_sheet([F["2026Q3"]["is"], t]), t)
            d = X.parse_balance_sheet(t)
            for k, v in zip(self.COLS, exp):
                self.assertAlmostEqual(d[k], v, places=1, msg=f"{q} {k}")


class TestProducts(unittest.TestCase):
    def test_quarter(self):
        t = F["2026Q3"]["prod"]
        self.assertIs(X.find_products([F["2026Q3"]["prod9m"], t], "three"), t)
        d = X.parse_products(t)
        self.assertEqual(d["current"][("Cardiometabolic Health", "Mounjaro")], (4998, 5725, 10723))
        self.assertEqual(d["current"][("Cardiometabolic Health", "Zepbound")], (5267, 86, 5352))
        self.assertEqual(d["current"][("Neuroscience", "Neuroscience")], (384, 59, 444))
        self.assertEqual(d["current"][("Other", "Other")], (57, 97, 154))
        self.assertEqual(d["current"][("Immunology", "Other Immunology")], (262, 318, 579))
        self.assertEqual(d["revenue"]["current"], (15136, 9261, 24397))
        self.assertEqual(d["areas"]["Oncology"], (1518, 1082, 2600))
        self.assertEqual(sum(v[2] for v in d["current"].values()), 24397)

    def test_nine_months(self):
        t = F["2026Q3"]["prod9m"]
        self.assertIs(X.find_products([F["2026Q3"]["prod"], t], "nine"), t)
        d = X.parse_products(t)
        self.assertEqual(d["revenue"]["current"], (41668, 25502, 67170))
        self.assertEqual(d["current"][("Cardiometabolic Health", "Mounjaro")], (14022, 15306, 29328))

    def test_annual(self):
        t = F["2025Q4"]["prod"]
        self.assertIs(X.find_products([F["2026Q3"]["prod"], t], "year"), t)
        d = X.parse_products(t, annual=True)
        self.assertEqual(d["current"][("Cardiometabolic Health", "Mounjaro")], (13651, 9315, 22966))
        self.assertEqual(d["current"][("Cardiometabolic Health", "Zepbound")], (13484, 58, 13542))
        self.assertEqual(d["prior"][("Cardiometabolic Health", "Zepbound")], (4926, 0, 4926))
        self.assertEqual(d["revenue"]["current"], (43481, 21698, 65179))


class TestPRV(unittest.TestCase):
    def test_q3(self):
        t = F["2026Q3"]["prv"]
        self.assertIs(X.find_prv([F["2026Q3"]["prod"], t]), t)
        d = X.parse_prv(t)
        self.assertEqual(d["period"]["U.S."], {"volume": 0.41, "price": -0.07, "rate": 0.0, "total_change": 0.34})
        self.assertEqual(d["period"]["Total Revenue"]["volume"], 0.53)
        self.assertEqual(d["ytd"]["Outside U.S."], {"volume": 0.93, "price": -0.30, "rate": 0.04, "total_change": 0.67})

    def test_2023(self):
        d = X.parse_prv(F["2023Q3"]["prv"])
        self.assertEqual(d["period"]["U.S."], {"volume": 0.09, "price": 0.13, "rate": 0.0, "total_change": 0.21})
        self.assertEqual(d["ytd"]["Total Revenue"]["price"], 0.0)

    def test_annual(self):
        d = X.parse_prv(F["2025Q4"]["prv"], annual=True)
        self.assertEqual(d["ytd"]["U.S."], {"volume": 0.53, "price": -0.10, "rate": 0.0, "total_change": 0.43})
        self.assertEqual(d["ytd"]["Total Revenue"], {"volume": 0.50, "price": -0.06, "rate": 0.01, "total_change": 0.45})
        self.assertEqual(d["revenue"], {"U.S.": 43481, "Outside U.S.": 21698, "Total Revenue": 65179})


class TestSelected(unittest.TestCase):
    def test_selected(self):
        t = F["8K-2026Q3"]["selected"]
        self.assertIs(X.find_8k_selected([F["8K-2026Q3"]["is"], t]), t)
        d = X.parse_8k_selected(t)
        self.assertEqual(d["Foundayo"], [184, 0, 281, 0])
        self.assertEqual(d["Kisunla"], [193, 70, 484, 141])
        self.assertNotIn("Total Revenue", d)
        self.assertEqual(d["Zepbound"][0], 5352)


class TestPeriod(unittest.TestCase):
    def test_quarter_of(self):
        self.assertEqual(X.quarter_of_table(F["8K-2026Q3"]["is"]), "2026Q3")
        self.assertEqual(X.quarter_of_table(F["2026Q3"]["prv"]), "2026Q3")
        self.assertEqual(X.quarter_of_table(F["2025Q4"]["is"]), "2025Q4")
        self.assertEqual(X.quarter_of_table(F["2023Q3"]["bs"]), "2023Q3")


if __name__ == "__main__":
    unittest.main()
