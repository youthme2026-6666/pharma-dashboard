"""港股适配器（公司清单里 source 为 hkex 的公司）：港交所披露易的业绩公告和中报/年报 PDF → 统一格式。

找报告：stockId 由 prefix.do 按代码查（公司清单里的 hkex_stock_id 每 30 天核对一次）；titleSearchServlet.do 查
- 业绩公告（Announcements and Notices - [Interim Results] / [Final Results] / [Quarterly Results]）：先出，多数含利润表、
  资产负债表，有的也含现金流量表；
- 财务报告（Financial Statements/ESG Information - [Annual Report] / [Interim/Half-Year Report] / [Quarterly Report]）：
  晚约一个月，三张表都有。
同一期间两份文件的数必须一致（cn_pdf.consistency）。PDF 在 https://www1.hkexnews.hk + FILE_LINK。
澄清、补充、更正公告不自动读，只提醒人工确认。

港股报表各家写法不同（行名、小计、单位），对照交给 GPT（只给行号，程序读数、核对），版式没变时沿用上一份的对照。
读 PDF、对照、核对、期间推算都在 cn_pdf.py。
"""
import datetime as dt
import html
import json
import os
import re
import urllib.parse

from . import cn_pdf as P
from .base import Result

SOURCE_ZH = "港交所披露易"
STYLE = "ifrs"
MARKET = "HK"
BASE = "https://www1.hkexnews.hk"
PREFIX = BASE + "/search/prefix.do?&callback=callback&lang=EN&type=A&name=%s&market=SEHK"
SEARCH = BASE + "/search/titleSearchServlet.do?"
# 分类标签按前缀认（例如晶泰的年报分类是 “[Annual Report / Environmental, Social and Governance Information]”）
CATEGORIES = [("[final results", 12, "ann"), ("[interim results", 6, "ann"), ("[quarterly results", None, "ann"),
              ("[annual report", 12, "rep"), ("[interim/half-year report", 6, "rep"), ("[quarterly report", None, "rep")]
NOTICE = re.compile(r"clarification|supplement|addendum|errat|correction|amendment", re.I)
SKIP = re.compile(r"poll results|environmental|sustainability|esg|social and governance|corporate governance report", re.I)
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
_DATE1 = re.compile(r"(\d{1,2})\s+(%s),?\s+(\d{4})" % "|".join(MONTHS), re.I)
_DATE2 = re.compile(r"(%s)\s+(\d{1,2}),?\s+(\d{4})" % "|".join(MONTHS), re.I)


def make_http(ctx):
    return P.Http(os.path.join(ctx.root, "agent", ".cache", "hkex"), min_interval=1.0)


def hk_listing(company):
    for l in company.get("listings") or []:
        if l.get("exchange") == "HKEX":
            return l
    return None


def stock_id(company, ctx, http, state, res):
    """披露易的 stockId：公司清单里有就用（每 30 天用 prefix.do 核对一次），没有就查。"""
    l = hk_listing(company)
    if not l:
        raise P.FetchError("公司清单里没有港股代码")
    sid = l.get("hkex_stock_id") or state.get("hkex_stock_id")
    if sid and state.get("identity_checked", "") >= (ctx.today - dt.timedelta(days=30)).isoformat():
        return int(sid)
    body = http.get(PREFIX % l["code"], ttl=86400, suffix=".js").decode("utf-8", "replace")
    m = re.search(r"callback\((.*)\)\s*;?\s*$", body.strip(), re.S)
    try:
        data = json.loads(m.group(1)) if m else {}
    except ValueError:
        data = {}
    found = [s for s in data.get("stockInfo") or [] if s.get("code") == l["code"]]
    if not found:
        raise P.FetchError("披露易查不到代码 %s" % l["code"])
    official = int(found[0]["stockId"])
    if l.get("hkex_stock_id") and int(l["hkex_stock_id"]) != official:
        res.error("公司清单的 hkex_stock_id %s 与披露易（%s → %s）不一致" % (l["hkex_stock_id"], l["code"], official))
    state.update(hkex_stock_id=official, hkex_name=found[0].get("name"), identity_checked=ctx.today.isoformat())
    return official


def search(http, sid, since, until, **kw):
    params = {"sortDir": "0", "sortByOptions": "DateTime", "category": "0", "market": "SEHK", "stockId": str(sid),
              "documentType": "-1", "fromDate": since, "toDate": until, "title": "", "searchType": "0", "t1code": "-2",
              "t2Gcode": "-2", "t2code": "-2", "rowRange": "100", "lang": "E"}
    params.update(kw)
    data = http.json(SEARCH + urllib.parse.urlencode(params), ttl=3 * 3600)
    try:
        return json.loads(data.get("result") or "[]")
    except ValueError:
        raise P.FetchError("披露易返回的结果读不出来")


def classify(item, fye=12):
    """一条披露易记录 → (fiscal_year, months, kind) 或 None。kind：ann 业绩公告 / rep 中报年报。"""
    cat = html.unescape(item.get("LONG_TEXT") or "").lower()
    title = re.sub(r"\s+", " ", html.unescape(item.get("TITLE") or "")).strip()
    if cat.startswith("(cancelled") or SKIP.search(title) or NOTICE.search(title):
        return None
    for key, months, kind in CATEGORIES:
        if key in cat:
            break
    else:
        return None
    low = title.lower()
    if months is None:
        months = 9 if re.search(r"nine months|third quarter|3rd quarter", low) else 3 if re.search(r"three months|first quarter|1st quarter", low) else None
        if months is None:
            return None
    m1, m2 = _DATE1.search(title), _DATE2.search(title)
    if m1 or m2:
        if m1:
            day, mon, year = int(m1.group(1)), MONTHS.index(m1.group(2).lower()) + 1, int(m1.group(3))
        else:
            day, mon, year = int(m2.group(2)), MONTHS.index(m2.group(1).lower()) + 1, int(m2.group(3))
        fy = year if mon <= fye else year + 1
        got = ((mon - fye - 1) % 12) + 1
        if got != months:
            return None
        return fy, months, kind
    y = re.search(r"\b(20\d\d)\b", title)
    if not y:
        return None
    return int(y.group(1)), months, kind


def discover(company, ctx, http, state, res):
    """披露易上这家公司的业绩公告和中报/年报：[{id, title, url, published, fiscal_year, months, kind}]。"""
    sid = stock_id(company, ctx, http, state, res)
    fye = company.get("fiscal_year_end_month", 12)
    since, until = "%d0101" % P.FIRST_YEAR, (ctx.today + dt.timedelta(days=1)).strftime("%Y%m%d")
    items = search(http, sid, since, until, title="results") + search(http, sid, since, until, searchType="1", t1code="40000")
    docs, seen, notices = {}, set(), []
    for it in items:
        link = it.get("FILE_LINK") or ""
        if not link.lower().endswith(".pdf") or link in seen:
            continue
        seen.add(link)
        title = re.sub(r"\s+", " ", html.unescape(it.get("TITLE") or "")).strip()
        try:
            day = dt.datetime.strptime((it.get("DATE_TIME") or "")[:10], "%d/%m/%Y").date()
        except ValueError:
            continue
        cat = html.unescape(it.get("LONG_TEXT") or "").lower()
        if NOTICE.search(title) and any(k in cat for k, _, _ in CATEGORIES):
            notices.append((day, title, BASE + link))
            continue
        c = classify(it, fye)
        if not c:
            continue
        fy, months, kind = c
        if fy < P.FIRST_YEAR:
            continue
        d = {"id": "%d-%s-%s" % (fy, P.SPAN_ID[months], kind), "title": title, "url": BASE + link, "published": day.isoformat(),
             "fiscal_year": fy, "months": months, "kind": "announcement" if kind == "ann" else "report"}
        old = docs.get(d["id"])
        if old is None or d["published"] > old["published"]:
            docs[d["id"]] = d
    for day, title, url in notices:
        if day >= ctx.today - dt.timedelta(days=45):
            res.warnings.append("披露易有业绩相关的澄清/补充公告，请人工确认数字是否受影响：%s（%s，%s）" % (title, day, url))
    state["notices"] = ["%s %s %s" % (d, t, u) for d, t, u in sorted(notices)]
    return sorted(docs.values(), key=lambda d: (d["published"], d["id"]))


def update(company, ctx):
    return P.run_update(company, ctx, _this())


def backtest(company, ctx):
    return P.recheck(company, ctx, _this())


def check(company, ctx):
    return P.check_company(company, ctx, _this())


def selftest(company, ctx):
    res = Result(company["id"])
    http = make_http(ctx)
    try:
        docs = discover(company, ctx, http, {}, res)
        return {"reports": len(docs), "latest": docs[-1]["title"] if docs else None}
    except P.FetchError as e:
        return {"error": str(e)}


def _this():
    import sys
    return sys.modules[__name__]
