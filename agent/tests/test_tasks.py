"""GPT 任务的核对规则：用假的 GPT 回答检验“该收的收、该拒的拒”。不访问网络。"""
import datetime as dt
import io
import json
import os
import sys
import unittest
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from agent.pipeline import gpt as G  # noqa: E402
from agent.pipeline import tasks as T  # noqa: E402
from agent.pipeline import verify as V  # noqa: E402
from agent.pipeline.datasets import Store  # noqa: E402

CFG = json.load(open(os.path.join(ROOT, "agent", "config.json"), encoding="utf-8"))


class FakeGPT:
    def __init__(self, data, sources=(), citations=(), pages=None):
        self.data, self.sources, self.citations, self.calls = data, list(sources), list(citations), []
        self.pages = pages or {}   # 网址 → (电头, 标题)：模拟“打开新闻稿逐字读电头”的核对调用

    def respond(self, instructions, user_input, **kw):
        self.calls.append(kw)
        if user_input in self.pages:
            d = dict(zip(("dateline", "headline"), self.pages[user_input]))
            return {"text": json.dumps(d), "data": d, "citations": [], "sources": [user_input], "model": "fake", "status": "completed"}
        return {"text": json.dumps(self.data), "data": self.data, "citations": self.citations, "sources": self.sources, "model": "fake", "status": "completed"}


GOOD = "https://investor.lilly.com/news-releases/news-release-details/lilly-announces-something-new"


class TestVerify(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(V.numbers_supported("含 4.1 亿美元里程碑款", ["a milestone payment of $410 million"]), [])
        self.assertEqual(V.numbers_supported("含约 9 亿美元一次性利好", ["approximately $0.9 billion of one-time benefits"]), [])
        self.assertEqual(V.numbers_supported("每股收益减少 3.29 美元", ["reduced EPS by $3.29"]), [])
        self.assertEqual(V.numbers_supported("降价 70%", ["cuts insulin prices by 70%"]), [])
        self.assertEqual(V.numbers_supported("含 5 亿美元", ["a payment of $410 million"]), ["5 亿美元"])
        self.assertEqual(V.numbers_supported("10 个美国工厂", ["one of 10 new U.S. sites"]), [])
        self.assertEqual(V.numbers_supported("12 个美国工厂", ["one of 10 new U.S. sites"]), ["12"])

    def test_cjk_spacing(self):
        self.assertEqual(V.cjk_spacing("FDA 批准 Olumiant 用于12岁及以上重度斑秃患儿"), "FDA 批准 Olumiant 用于 12 岁及以上重度斑秃患儿")
        self.assertEqual(V.cjk_spacing("每股收益含0.52美元外购在研项目（IPR&D）费用"), "每股收益含 0.52 美元外购在研项目（IPR&D）费用")
        self.assertEqual(V.cjk_spacing("里士满 50 亿美元抗癌药生产基地动工"), "里士满 50 亿美元抗癌药生产基地动工")

    def test_urls(self):
        self.assertEqual(V.norm_url("http://Investor.Lilly.com/a/b/?x=1#y"), "https://investor.lilly.com/a/b")
        self.assertTrue(V.host_allowed("https://investor.lilly.com/x", ["lilly.com"]))
        self.assertFalse(V.host_allowed("https://lilly.com.evil.example/x", ["lilly.com"]))
        self.assertTrue(V.quote_in("driven by a 50% increase in volume", "Revenue rose 43%, driven by a 50%  increase in volume."))


class TestEvents(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(ROOT, "agent", "tests", "fixtures", "seed"))
        self.today = dt.date(2026, 11, 20)

    def run_with(self, items, sources, pages=None):
        pages = pages if pages is not None else {GOOD: ("INDIANAPOLIS, Nov. 12, 2026 /PRNewswire/", "FDA approves Mounjaro for a new indication")}
        g = FakeGPT({"events": items}, sources=sources, pages=pages)
        return T.find_events(g, self.store, CFG, today=self.today), g

    def ev(self, **kw):
        e = {"date": "2026-11-12", "type": "获批", "product": "Mounjaro", "title": "FDA 批准 Mounjaro 新适应症",
             "title_en": "FDA approves Mounjaro for a new indication", "url": GOOD}
        e.update(kw)
        return e

    def test_accept_and_reject(self):
        items = [
            self.ev(),
            self.ev(url="https://investor.lilly.com/news-releases/news-release-details/not-searched", title_en="X"),
            self.ev(url="https://www.example.com/lilly-news", title_en="Y"),
            self.ev(date="2025-01-01", url=GOOD + "-2", title_en="Z"),
            self.ev(url=GOOD + "-3", type="产能", product="", title="新工厂投资 50 亿美元", title_en="Lilly invests in new site"),
            self.ev(url="https://investor.lilly.com/news-releases/news-release-details/lilly-and-hims-hers-expand-access-fda-approved-lilly-medicines", title_en="dup"),
            self.ev(type="分红", url=GOOD + "-4", title_en="W"),
        ]
        sources = [GOOD, GOOD + "-2", GOOD + "-3", GOOD + "-4", "https://www.example.com/lilly-news",
                   "https://investor.lilly.com/news-releases/news-release-details/lilly-and-hims-hers-expand-access-fda-approved-lilly-medicines"]
        (new, rejected, meta), g = self.run_with(items, sources)
        self.assertEqual([e["url"] for e in new], [GOOD])
        self.assertEqual(new[0]["id"], "e2026-11-12-mounjaro")
        self.assertEqual(new[0]["quarter"], "2026Q4")
        reasons = " | ".join(r["reason"] for r in rejected)
        for key in ("实际检索到的来源", "不是礼来官网或 SEC", "不在", "数字", "已经收录", "类型"):
            self.assertIn(key, reasons)
        self.assertTrue(g.calls[0]["web_search"])

    def test_no_sources_rejects_everything(self):
        (new, rejected, meta), _ = self.run_with([self.ev()], [])
        self.assertEqual(new, [])

    def test_second_reading_must_agree(self):
        # 2026-11-09 实测：同一篇 Olumiant 新闻稿（电头 Sept. 25）一次检索给了 10-25
        cases = [({GOOD: ("INDIANAPOLIS, Oct. 12, 2026", "FDA approves Mounjaro for a new indication")}, "日期对不上"),
                 ({GOOD: ("", "")}, "读不出"),
                 ({GOOD: ("INDIANAPOLIS, Nov. 12, 2026", "Lilly to acquire a biotech company for its pipeline")}, "标题"),
                 ({}, "读不出")]
        for pages, why in cases:
            (new, rejected, meta), _ = self.run_with([self.ev()], [GOOD], pages=pages)
            self.assertEqual(new, [])
            self.assertIn(why, rejected[0]["reason"])

    def test_excluded_kinds(self):
        for headline in ("Lilly to showcase new retatrutide, Taltz (ixekizumab) and Zepbound (tirzepatide) data at ACR Convergence 2026",
                         "Lilly declares fourth-quarter 2026 dividend", "Lilly announces two new Executive Committee members"):
            (new, rejected, meta), _ = self.run_with([self.ev(title_en=headline, title="礼来新闻")], [GOOD])
            self.assertEqual(new, [], headline)
            self.assertIn("不收录", rejected[0]["reason"])

    def test_dateline(self):
        self.assertEqual(T.dateline_date("INDIANAPOLIS, Sept. 25, 2026 /PRNewswire/ --"), "2026-09-25")
        self.assertEqual(T.dateline_date("INDIANAPOLIS, Nov. 5, 2026"), "2026-11-05")
        self.assertEqual(T.dateline_date("BOSTON, June 12, 2026"), "2026-06-12")
        self.assertIsNone(T.dateline_date("INDIANAPOLIS"))
        self.assertTrue(T.same_headline("FDA approves Lilly's Olumiant (baricitinib) for pediatric patients",
                                        "FDA approves Lilly’s Olumiant (baricitinib) for pediatric patients 12 years of age and older"))


TEXT = ("Lilly reports fourth-quarter 2026 financial results. Revenue in the U.S. increased 43%, driven by a 50% increase "
        "in volume, partially offset by 7% lower realized prices. Revenue outside the U.S. increased 43%, driven by a 37% "
        "increase in volume and a 5% favorable impact of foreign exchange rates, partially offset by 1% higher realized prices. "
        "Worldwide revenue increased 43%, driven by a 46% increase in volume, partially offset by 5% lower realized prices "
        "and a 1% favorable impact of foreign exchange rates. Fourth-quarter revenue included a one-time milestone payment of $410 million.")


class TestQ4PRV(unittest.TestCase):
    def test_accept(self):
        data = {"regions": [
            {"region": "U.S.", "volume": 50, "price": -7, "rate": 0, "total_change": 43,
             "quote": "Revenue in the U.S. increased 43%, driven by a 50% increase in volume, partially offset by 7% lower realized prices."},
            {"region": "Total Revenue", "volume": 46, "price": -5, "rate": 1, "total_change": 43,
             "quote": "Worldwide revenue increased 43%, driven by a 46% increase in volume, partially offset by 5% lower realized prices and a 1% favorable impact of foreign exchange rates."},
            {"region": "Outside U.S.", "volume": 37, "price": -1, "rate": 5, "total_change": 43,
             "quote": "Revenue outside the U.S. increased 43%, driven by a 37% increase in volume"}]}
        out, problems = T.q4_prv(FakeGPT(data), TEXT, {"U.S.": 0.428, "Outside U.S.": 0.43, "Total Revenue": 0.432})
        self.assertEqual(out["U.S."], {"volume": 0.5, "price": -0.07, "rate": 0.0, "total_change": 0.43})
        self.assertEqual(out["Total Revenue"], {"volume": 0.46, "price": -0.05, "rate": 0.01, "total_change": 0.43})
        # 价格、汇率的数字不在它摘的原句里：这两项留空，原句里写明的销量照收
        self.assertEqual(out["Outside U.S."], {"volume": 0.37, "price": None, "rate": None, "total_change": 0.43})
        self.assertEqual(len(problems), 2)

    def test_reject_wrong_growth(self):
        data = {"regions": [{"region": "U.S.", "volume": 50, "price": -7, "rate": 0, "total_change": 43,
                             "quote": "Revenue in the U.S. increased 43%, driven by a 50% increase in volume, partially offset by 7% lower realized prices."}]}
        out, problems = T.q4_prv(FakeGPT(data), TEXT, {"U.S.": 0.30})
        self.assertEqual(out, {})


# 礼来 2025 年第四季度新闻稿（2026-02-04 的 8-K）里的原句：美国以外没写价格，全球没写汇率
REAL = ("In Q4 2025, worldwide revenue was $19.3 billion, an increase of 43% compared with Q4 2024, driven by a 46% increase in "
        "volume, partially offset by a 5% decrease due to lower realized prices. Key Products1 revenue grew to $13.8 billion in "
        "Q4 2025, led by Mounjaro and Zepbound.\n\nRevenue in the U.S. increased 43% to $12.9 billion, driven by a 50% increase "
        "in volume, partially offset by a 7% decrease due to lower realized prices. The increase in U.S. volume and decline in "
        "realized prices were driven by Zepbound and Mounjaro.\n\nRevenue outside the U.S. increased 43% to $6.4 billion, driven "
        "by a 38% increase in volume and to a lesser extent a 4% favorable impact on foreign exchange rates.")
WW = "In Q4 2025, worldwide revenue was $19.3 billion, an increase of 43% compared with Q4 2024, driven by a 46% increase in volume, partially offset by a 5% decrease due to lower realized prices."
US = "Revenue in the U.S. increased 43% to $12.9 billion, driven by a 50% increase in volume, partially offset by a 7% decrease due to lower realized prices."
OUS = "Revenue outside the U.S. increased 43% to $6.4 billion, driven by a 38% increase in volume and to a lesser extent a 4% favorable impact on foreign exchange rates."
GROWTH = {"U.S.": 0.4257, "Outside U.S.": 0.4253, "Total Revenue": 0.4256}   # 由已核对的收入算出


class TestQ4PRVReal(unittest.TestCase):
    def run_with(self, *regions):
        return T.q4_prv(FakeGPT({"regions": [dict(zip(("region", "volume", "price", "rate", "total_change", "quote"), r)) for r in regions]}), REAL, GROWTH)

    def test_partial_disclosure(self):
        out, problems = self.run_with(("U.S.", 50, -7, None, 43, US), ("Outside U.S.", 38, None, 4, 43, OUS), ("Total Revenue", 46, -5, None, 43, WW))
        self.assertEqual(out["U.S."], {"volume": 0.5, "price": -0.07, "rate": 0.0, "total_change": 0.43})
        self.assertEqual(out["Outside U.S."], {"volume": 0.38, "price": None, "rate": 0.04, "total_change": 0.43})
        self.assertEqual(out["Total Revenue"], {"volume": 0.46, "price": -0.05, "rate": None, "total_change": 0.43})
        self.assertEqual(problems, [])

    def test_inferred_or_misplaced_numbers_are_dropped(self):
        out, problems = self.run_with(
            ("Outside U.S.", 38, 1, 4, 43, OUS),          # 价格 1% 是推算出来的，原句没有
            ("Total Revenue", -5, 46, 4, 43, WW),         # 销量和价格对调；4 只出现在“Q4”里
            ("U.S.", 50, 7, 0, 43, US))                   # 价格方向错了（原句是下降）
        self.assertEqual(out["Outside U.S."], {"volume": 0.38, "price": None, "rate": 0.04, "total_change": 0.43})
        self.assertNotIn("Total Revenue", out)
        self.assertEqual(out["U.S."], {"volume": 0.5, "price": None, "rate": 0.0, "total_change": 0.43})
        self.assertEqual(len(problems), 6)

    def test_phrasing_variants(self):
        self.assertTrue(T.part_supported("driven by volume growth of 12% and lower realized prices of 3%", "volume", 12))
        self.assertTrue(T.part_supported("partially offset by 3 percent lower realized prices", "price", -3))
        self.assertTrue(T.part_supported("a 2% unfavorable impact of foreign exchange rates", "rate", -2))
        self.assertFalse(T.part_supported("a 2% unfavorable impact of foreign exchange rates", "rate", 2))
        self.assertFalse(T.part_supported("a 46% increase in volume", "price", 46))


class TestEarningsNote(unittest.TestCase):
    def test_accept(self):
        data = {"notable": True, "product": "Jardiance", "title": "第四季度收入含 4.1 亿美元 Jardiance 里程碑款（一次性）",
                "title_en": "Lilly reports fourth-quarter 2026 financial results",
                "evidence": ["Fourth-quarter revenue included a one-time milestone payment of $410 million."]}
        ev, why = T.earnings_note(FakeGPT(data), TEXT + " Jardiance", "2026Q4", "2027-02-05", "https://www.sec.gov/Archives/x.htm", {"Jardiance"})
        self.assertEqual(why, "ok")
        self.assertEqual(ev["id"], "e2027-02-05-q4")
        self.assertEqual(ev["type"], "业绩说明")

    def test_one_time_must_be_in_source(self):
        # 2026-02-04 实测：GPT 把外购在研费用称作“一次性”，新闻稿并没有这么说
        text = "EPS in Q4 2025 and Q4 2024 included acquired IPR&D charges of $0.52 and $0.19, respectively."
        base = {"notable": True, "product": "", "title_en": "Lilly reports fourth-quarter 2025 financial results", "evidence": [text]}
        ev, why = T.earnings_note(FakeGPT(dict(base, title="第四季度每股收益包含0.52美元一次性在研项目支出")), text, "2025Q4", "2026-02-04", "u", set())
        self.assertIsNone(ev)
        self.assertIn("一次性", why)
        ev, why = T.earnings_note(FakeGPT(dict(base, title="第四季度每股收益含0.52美元外购在研项目（IPR&D）费用")), text, "2025Q4", "2026-02-04", "u", set())
        self.assertEqual(ev["title"], "第四季度每股收益含 0.52 美元外购在研项目（IPR&D）费用")

    def test_product_context_uses_every_mention(self):
        text = ("x" * 2000 + " launching Inluriyo, expanding Mounjaro " + "y" * 3000 +
                " Updated data for Lilly's Inluriyo (imlunestrant) in ER+, HER2- advanced breast cancer")
        ctx = T.product_context(text, "Inluriyo")
        self.assertIn("launching Inluriyo", ctx)
        self.assertIn("breast cancer", ctx)
        self.assertLessEqual(len(ctx), 3000)

    def test_reject_fabricated(self):
        data = {"notable": True, "product": "", "title": "第四季度收入含 6 亿美元里程碑款",
                "title_en": "x", "evidence": ["Fourth-quarter revenue included a one-time milestone payment of $410 million."]}
        ev, why = T.earnings_note(FakeGPT(data), TEXT, "2026Q4", "2027-02-05", "https://www.sec.gov/x", set())
        self.assertIsNone(ev)
        data["evidence"] = ["This sentence is not in the press release at all."]
        data["title"] = "第四季度收入含 4.1 亿美元里程碑款"
        ev, why = T.earnings_note(FakeGPT(data), TEXT, "2026Q4", "2027-02-05", "https://www.sec.gov/x", set())
        self.assertIsNone(ev)


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestGPTClient(unittest.TestCase):
    def test_parse_and_fallback(self):
        sent = []
        payload = {"status": "completed", "usage": {"input_tokens": 10, "output_tokens": 5}, "output": [
            {"type": "web_search_call", "action": {"sources": [{"url": GOOD}]}},
            {"type": "message", "content": [{"type": "output_text", "text": "```json\n{\"ok\": true}\n```",
                                             "annotations": [{"type": "url_citation", "url": GOOD + "?utm=1"}]}]}]}

        def opener(req, timeout=None):
            body = json.loads(req.data.decode()) if req.data else None
            sent.append(body)
            if body and "filters" in json.dumps(body):
                raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(b'{"error": {"message": "Unknown parameter: tools[0].filters"}}'))
            return FakeResp(json.dumps(payload).encode())

        g = G.GPT(CFG, api_key="sk-test", model="gpt-test", opener=opener)
        r = g.respond("i", "u", web_search=True, allowed_domains=["lilly.com"], schema={"type": "object"})
        self.assertEqual(r["data"], {"ok": True})
        self.assertEqual(r["sources"], [GOOD])
        self.assertEqual(len(sent), 2)
        self.assertNotIn("filters", json.dumps(sent[1]))
        self.assertEqual(g.usage["web_searches"], 1)

    def test_model_fallback(self):
        def opener(req, timeout=None):
            ids = ("gpt-4o", "gpt-5", "gpt-5.5", "gpt-5.5-pro", "gpt-6-luna", "gpt-5.10-mini", "o3")
            return FakeResp(json.dumps({"data": [{"id": i} for i in ids]}).encode())
        g = G.GPT({"gpt": {"model_preference": ["gpt-5.1", "gpt-4o"]}}, api_key="sk-test", model="", opener=opener)
        self.assertEqual(g.resolve_model(), "gpt-4o")
        g = G.GPT({"gpt": {"model_preference": ["gpt-5.1"]}}, api_key="sk-test", model="", opener=opener)
        self.assertEqual(g.resolve_model(), "gpt-5.5")   # 偏好的都下线了：选版本最新的通用模型

    def test_parse_json(self):
        self.assertEqual(G.parse_json('前言 {"a": {"b": "}"}} 后记'), {"a": {"b": "}"}})
        self.assertIsNone(G.parse_json("没有 JSON"))


if __name__ == "__main__":
    unittest.main()
