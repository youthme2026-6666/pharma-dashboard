"""SEC EDGAR 网页格式的解析：-index.htm 文件表，以及 Workiva 风格的表格（符号拆在单独格子里、iXBRL 标签）。"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from agent.pipeline import extract as X  # noqa: E402
from agent.pipeline.sec import _IndexParser  # noqa: E402
from agent.pipeline.tables import html_tables, merge_cells  # noqa: E402

INDEX = """<html><body><div class="formGrouping">
<table class="tableFile" summary="Document Format Files">
<tr><th scope="col">Seq</th><th scope="col">Description</th><th scope="col">Document</th><th scope="col">Type</th><th scope="col">Size</th></tr>
<tr class="evenRow"><td scope="row">1</td><td scope="row">8-K</td><td scope="row"><a href="/ix?doc=/Archives/edgar/data/59478/000005947826000090/lly-20261029.htm">lly-20261029.htm</a> &nbsp;&nbsp;<span class="label">iXBRL</span></td><td scope="row">8-K</td><td scope="row">45123</td></tr>
<tr><td scope="row">2</td><td scope="row">EX-99.1</td><td scope="row"><a href="/Archives/edgar/data/59478/000005947826000090/lly-20261029xex991.htm">lly-20261029xex991.htm</a></td><td scope="row">EX-99.1</td><td scope="row">812345</td></tr>
</table>
<table class="tableFile" summary="Data Files"><tr><td>3</td><td>XBRL</td><td><a href="/Archives/edgar/data/59478/000005947826000090/lly-20261029_htm.xml">lly-20261029_htm.xml</a></td><td>XML</td><td>9</td></tr></table>
</div></body></html>"""

IS_8K = """<table>
<tr><td></td><td colspan="3">Three Months Ended</td><td colspan="3">Nine Months Ended</td></tr>
<tr><td></td><td colspan="3">September 30,</td><td colspan="3">September 30,</td></tr>
<tr><td></td><td>2026</td><td>2025</td><td>% Chg.</td><td>2026</td><td>2025</td><td>% Chg.</td></tr>
<tr><td><span>Revenue</span></td><td>$</td><td><ix:nonFraction name="us-gaap:Revenues">24,397</ix:nonFraction></td><td></td><td>$</td><td>17,601</td><td>39</td><td>%</td><td>$</td><td>67,170</td><td>$</td><td>45,887</td><td>46</td><td>%</td></tr>
<tr><td>Asset impairment, restructuring and other special charges</td><td>303</td><td>365</td><td>(17</td><td>)%</td><td>1,285</td><td>400</td><td>NM</td></tr>
<tr><td>Other income (expense)</td><td>(202</td><td>)</td><td>(</td><td>132</td><td>)</td><td>53</td><td>%</td><td>2</td><td>(462</td><td>)</td><td>(100</td><td>)</td><td>%</td></tr>
<tr><td>Net income</td><td>6,451</td><td>5,583</td><td>16</td><td>%</td><td>20,942</td><td>14,002</td><td>50</td><td>%</td></tr>
</table>"""


class TestEdgarHtml(unittest.TestCase):
    def test_index(self):
        p = _IndexParser()
        p.feed(INDEX)
        rows = [(c[3], h.split("/")[-1]) for c, h in p.rows if h]
        self.assertEqual(rows[:2], [("8-K", "lly-20261029.htm"), ("EX-99.1", "lly-20261029xex991.htm")])
        self.assertEqual(rows[2], ("XML", "lly-20261029_htm.xml"))

    def test_split_symbols(self):
        self.assertEqual(merge_cells(["Revenue", "$", "24,397", "", "$", "17,601", "39", "%"]), ["Revenue", "24,397", "17,601", "39%"])
        self.assertEqual(merge_cells(["Other", "(202", ")", "(", "132", ")", "(100", ")", "%"]), ["Other", "(202)", "(132)", "(100)%"])
        self.assertEqual(merge_cells(["Price", "(7)", "(29)"]), ["Price", "(7)", "(29)"])

    def test_footnote_in_own_cell(self):
        t = [["Three Months Ended September 30,"], ["Cardiometabolic Health:"],
             ["Zepbound", "(1)", "5,267", "86", "5,352", "3,568", "20", "3,588"],
             ["Total cardiometabolic health", "12,376", "7,388", "19,764", "8,867", "4,311", "13,178"]]
        d = X.parse_products(t)
        self.assertEqual(d["current"][("Cardiometabolic Health", "Zepbound")], (5267, 86, 5352))
        sel = X.parse_8k_selected([["Selected Products", "2026", "2025"], ["Zepbound", "(1)", "5,352", "3,588", "49%", "14,440", "9,281", "56%"],
                                   ["Price", "(7)", "(29)"]])
        self.assertEqual(sel["Zepbound"], [5352, 3588, 14440, 9281])
        self.assertEqual(sel["Price"], [-7, -29])

    def test_8k_income_from_html(self):
        t = html_tables(IS_8K)[0]
        self.assertEqual(X.quarter_of_table(t), "2026Q3")
        d = X.parse_8k_income(t)
        self.assertEqual(d["revenue"], [24397, 17601, 67170, 45887])
        self.assertEqual(d["impairment_restructuring"], [303, 365, 1285, 400])
        self.assertEqual(d["other_net"], [-202, -132, 2, -462])
        self.assertEqual(d["net_income"], [6451, 5583, 20942, 14002])


if __name__ == "__main__":
    unittest.main()
