"""A 股适配器（公司清单里 source 为 cninfo 的公司）：巨潮资讯的定期报告 PDF → 统一格式。

找报告：POST http://www.cninfo.com.cn/new/hisAnnouncement/query（column=sse/szse，stock=代码,orgId，
category=年报、半年报、一季报、三季报），PDF 在 http://static.cninfo.com.cn/<adjunctUrl>。
排除摘要、英文版、更正前、已取消的文件；同一期间有更正后的版本时用更正后的。orgId 每 30 天用巨潮的
股票清单（szse_stock.json）核对一次。读 PDF、对照、核对、期间推算都在 cn_pdf.py（见那里的说明）。

A 股合并报表是财政部统一格式，标准行名用固定对照表（cn_pdf.CAS_RULES），不调用 GPT；对照表认不全时才问 GPT。
"""
import datetime as dt
import os
import re
import urllib.parse

from . import cn_pdf as P
from .base import Result

SOURCE_ZH = "巨潮资讯"
STYLE = "cas"
MARKET = "A"
QUERY = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
STATIC = "http://static.cninfo.com.cn/"
STOCKS = "http://www.cninfo.com.cn/new/data/szse_stock.json"
CATEGORIES = "category_ndbg_szsh;category_bndbg_szsh;category_yjdbg_szsh;category_sjdbg_szsh"
SPANS = {"第一季度": 3, "一季度": 3, "半年度": 6, "中期": 6, "第三季度": 9, "三季度": 9, "年度": 12}
EXCLUDE = re.compile(r"摘要|英文|english|更正前|已取消|取消|修订前|作废|关于|公告|提示性", re.I)   # 摘要、英文版、旧版本和“关于××报告的更正公告”
PREFER = re.compile(r"更正后|修订后|修订版|更新后|修订稿")
BJ = dt.timezone(dt.timedelta(hours=8))


def make_http(ctx):
    return P.Http(os.path.join(ctx.root, "agent", ".cache", "cninfo"), min_interval=1.0)


def a_listing(company):
    for l in company.get("listings") or []:
        if l.get("exchange") in ("SSE", "SZSE") and l.get("cninfo_org_id"):
            return l
    return None


def clean_title(title):
    return re.sub(r"<[^>]+>", "", P.norm(title or "")).strip()


def parse_title(title):
    """'恒瑞医药2026年第三季度报告' → (2026, 9)；摘要、英文版、更正前等返回 None。"""
    t = clean_title(title).replace(" ", "")
    if EXCLUDE.search(t):
        return None
    m = re.search(r"(\d{4})年(第一季度|一季度|半年度|中期|第三季度|三季度|年度)报告", t)
    if not m:
        return None
    return int(m.group(1)), SPANS[m.group(2)]


def check_org(listing, http, state, ctx, res):
    """orgId 与巨潮股票清单一致（每 30 天核对一次）。"""
    if state.get("identity_checked", "") >= (ctx.today - dt.timedelta(days=30)).isoformat():
        return
    data = http.json(STOCKS, ttl=7 * 86400)
    found = [s for s in data.get("stockList") or [] if str(s.get("code")) == listing["code"]]
    if not found:
        res.warnings.append("巨潮股票清单里查不到 %s（只提醒）" % listing["code"])
    elif found[0].get("orgId") != listing["cninfo_org_id"]:
        res.error("公司清单的巨潮 orgId %s 与巨潮股票清单（%s → %s）不一致" % (listing["cninfo_org_id"], listing["code"], found[0].get("orgId")))
        return
    state["identity_checked"] = ctx.today.isoformat()
    if found:
        state["cninfo_name"] = found[0].get("zwjc")


def discover(company, ctx, http, state, res):
    """巨潮上这家公司的定期报告：[{id, title, url, published, fiscal_year, months, kind}]。"""
    l = a_listing(company)
    if not l:
        raise P.FetchError("公司清单里没有 A 股代码和巨潮 orgId")
    code, org = l["code"], l["cninfo_org_id"]
    check_org(l, http, state, ctx, res)
    since = "%d-01-01" % P.FIRST_YEAR
    until = (ctx.today + dt.timedelta(days=1)).isoformat()
    anns = []
    for page in range(1, 11):
        form = {"pageNum": page, "pageSize": 30, "column": "sse" if l["exchange"] == "SSE" else "szse", "tabName": "fulltext",
                "plate": "", "stock": "%s,%s" % (code, org), "searchkey": "", "secid": "", "category": CATEGORIES, "trade": "",
                "seDate": "%s~%s" % (since, until), "sortName": "", "sortType": "", "isHLtitle": "false"}
        data = http.json(QUERY, data=urllib.parse.urlencode(form).encode("utf-8"), ttl=3 * 3600,
                         headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "X-Requested-With": "XMLHttpRequest"})
        items = data.get("announcements") or []
        anns += items
        if not data.get("hasMore") or not items:
            break
    docs, other = {}, 0
    for a in anns:
        if str(a.get("secCode")) != code:
            other += 1
            continue
        p = parse_title(a.get("announcementTitle"))
        if not p or (a.get("adjunctType") or "PDF").upper() != "PDF" or not a.get("adjunctUrl"):
            continue
        fy, months = p
        if fy < P.FIRST_YEAR:
            continue
        title = clean_title(a.get("announcementTitle"))
        published = dt.datetime.fromtimestamp(a["announcementTime"] / 1000, BJ).date().isoformat()
        d = {"id": "%d-%s" % (fy, P.SPAN_ID[months]), "title": title, "url": STATIC + a["adjunctUrl"].lstrip("/"),
             "published": published, "fiscal_year": fy, "months": months, "kind": "report"}
        old = docs.get(d["id"])
        better = old is None or (bool(PREFER.search(title)), published) > (bool(PREFER.search(old["title"])), old["published"])
        if better:
            docs[d["id"]] = d
    if other:
        res.warnings.append("巨潮返回了 %d 条别的代码的公告，已忽略" % other)
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
