"""端到端：假装 2026Q3 刚发布，用“假 SEC”提供真实表格，跑一遍每天的 auto 流程，检查写回的数据、状态、网页和报告。"""
import html
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from agent.pipeline import run as R  # noqa: E402
from agent.pipeline.datasets import Store  # noqa: E402
from agent.pipeline.sec import SecError  # noqa: E402
from agent.tests.test_extract import F  # noqa: E402

SEED = os.path.join(ROOT, "agent", "tests", "fixtures", "seed")
K8, Q10 = "0000059478-26-000090", "0000059478-26-000100"


def tables_html(*tables):
    return "".join("<table>" + "".join("<tr>" + "".join("<td>%s</td>" % html.escape(c) for c in row) + "</tr>" for row in t) + "</table>" for t in tables)


class FakeSEC:
    def __init__(self, drop_bs=False, no_10q=False):
        e = F["8K-2026Q3"]
        q = F["2026Q3"]
        self.k8 = "<html><body><p>Lilly reports third-quarter 2026 financial results.</p>%s</body></html>" % tables_html(e["is"], e["gm"], e["ni"], e["selected"])
        parts = [q["is"]] + ([] if drop_bs else [q["bs"]]) + [q["prod"], q["prod9m"], q["prv"]]
        self.q10 = "<html><body>%s</body></html>" % tables_html(*parts)
        self.no_10q = no_10q
        self.urls = []

    def submissions(self, cik, include_older=False):
        subs = [{"form": "8-K", "accession": K8, "filing_date": "2026-10-29", "report_date": "2026-10-29", "items": "2.02,9.01", "primary": "lly-8k.htm"}]
        if not self.no_10q:
            subs.insert(0, {"form": "10-Q", "accession": Q10, "filing_date": "2026-11-04", "report_date": "2026-09-30", "items": "", "primary": "lly-20260930.htm"})
        return subs

    def documents(self, cik, acc):
        return [{"name": "ex991.htm", "type": "EX-99.1", "description": "PRESS RELEASE", "url": "fake://%s/ex991.htm" % acc}] if acc == K8 else []

    def folder(self, cik, acc):
        return "fake://%s/" % acc

    def index_url(self, cik, acc):
        return "fake://%s/index" % acc

    def text(self, url, ttl=None):
        self.urls.append(url)
        return self.k8 if url.endswith("ex991.htm") else self.q10

    def concept(self, cik, taxonomy, tag):
        raise SecError("offline")


class TestAuto(unittest.TestCase):
    def setUp(self):
        self.saved = {k: getattr(R, k) for k in ("DATA", "OUT", "SITE", "STATE_DIR", "make_sec", "BACKTEST_QUARTERS")}
        self.key = os.environ.pop("OPENAI_API_KEY", None)
        os.environ["DASH_QUIET"] = "1"
        self.tmp = tempfile.mkdtemp()
        d = os.path.join(self.tmp, "data")
        shutil.copytree(SEED, d)
        s = Store(d)
        for n in ("income_statement", "balance_sheet", "product_revenue", "price_volume"):
            s.rows[n] = [r for r in s.rows[n] if r["quarter"] != "2026Q3"]
        s.rows["income_annual"] = [r for r in s.rows["income_annual"] if r["months"] == "12"]
        s.rows["price_volume_annual"] = [r for r in s.rows["price_volume_annual"] if r["year"] != "2026"]
        s.save()
        R.DATA, R.OUT, R.SITE, R.STATE_DIR = d, os.path.join(self.tmp, "out"), os.path.join(self.tmp, "site"), os.path.join(self.tmp, "state")
        R.BACKTEST_QUARTERS = ["2026Q3"]

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(R, k, v)
        if self.key is not None:
            os.environ["OPENAI_API_KEY"] = self.key
        shutil.rmtree(self.tmp, ignore_errors=True)

    def read(self, *p):
        with open(os.path.join(*p), encoding="utf-8") as f:
            return f.read()

    def test_new_quarter_end_to_end(self):
        R.make_sec = lambda cfg: FakeSEC()
        code = R.main(["auto", "--today", "2026-11-08"])
        report = self.read(R.OUT, "report.md")
        self.assertEqual(code, 0, report)
        self.assertIn("已写入：2026Q3", report)
        for name in ("income_statement", "balance_sheet", "income_annual"):
            self.assertEqual(self.read(R.DATA, name + ".csv"), self.read(SEED, name + ".csv"), name)
        state = json.loads(self.read(R.STATE_DIR, "state.json"))
        self.assertEqual(state["quarters"]["2026Q3"]["10-Q"], Q10)
        self.assertIn("2026Q3", self.read(R.SITE, "index.html"))
        pr = self.read(R.DATA, "product_revenue.csv")
        self.assertIn("2026Q3,Cardiometabolic Health,Foundayo,,,184", pr)
        self.assertIn("2026Q3,Oncology,Other Oncology (combined),,,788", pr)
        # 第二天再跑：没有新申报，数据不应变化
        code = R.main(["auto", "--today", "2026-11-09"])
        self.assertEqual(code, 0)
        self.assertIn("数据变化：无", self.read(R.OUT, "report.md"))

    def test_format_change_blocks_quarter(self):
        R.make_sec = lambda cfg: FakeSEC(drop_bs=True)
        before = self.read(R.DATA, "income_statement.csv")
        code = R.main(["auto", "--today", "2026-11-08"])
        report = self.read(R.OUT, "report.md")
        self.assertEqual(code, 1)
        self.assertIn("资产负债表", report)
        self.assertEqual(self.read(R.DATA, "income_statement.csv"), before)

    def test_waiting_for_10q(self):
        R.make_sec = lambda cfg: FakeSEC(no_10q=True)
        code = R.main(["auto", "--today", "2026-11-01"])
        report = self.read(R.OUT, "report.md")
        self.assertEqual(code, 0, report)
        self.assertIn("尚未发布", report)


if __name__ == "__main__":
    unittest.main()
