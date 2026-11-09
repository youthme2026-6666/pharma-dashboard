"""交给 GPT 的四类判断任务，以及每类任务的核对规则（规则写在代码里，GPT 无法绕过）。

1. find_events     联网搜索礼来官网新闻稿，挑出重大事件（获批、并购、定价与可及、临床、产能）。
2. earnings_note   读季度业绩新闻稿，判断有没有值得单独标注的一次性项目，写一句中文说明。
3. classify_area   新闻稿里出现没见过的产品时，判断它属于哪个治疗领域。
4. q4_prv          读第四季度新闻稿，读出第四季度的销量/价格/汇率拆分（10-K 只有全年）。
"""
import datetime as dt
import json
import re

from . import verify as V

EVENT_TYPES_HELP = {
    "获批": "监管批准：FDA/EMA/日本/中国等批准新药或新适应症",
    "并购": "收购、合并、重大授权引进或出售业务（宣布或完成）",
    "定价与可及": "降价、自付封顶、直销/自费渠道、与政府或平台的价格协议",
    "临床": "关键（III 期或头对头）试验的主要结果",
    "产能": "新建或扩建生产基地（通常 10 亿美元以上）",
    "业绩说明": "季度业绩中的一次性项目（由另一个任务生成，这里不要用）",
}


def _event_schema(types):
    item = {"type": "object", "additionalProperties": False,
            "required": ["date", "type", "product", "title", "title_en", "url"],
            "properties": {"date": {"type": "string"}, "type": {"type": "string", "enum": types},
                           "product": {"type": "string"}, "title": {"type": "string"},
                           "title_en": {"type": "string"}, "url": {"type": "string"}}}
    return {"type": "object", "additionalProperties": False, "required": ["events"],
            "properties": {"events": {"type": "array", "items": item}}}


def find_events(gpt, store, cfg, today=None, start=None):
    """返回 (新事件列表, 被拒绝的条目及原因)。"""
    ecfg = cfg["events"]
    today = today or dt.date.today()
    existing = store.rows["events"]
    last = max((e["date"] for e in existing), default="2021-01-01")
    if start is None:
        start = (dt.date.fromisoformat(last) - dt.timedelta(days=7)).isoformat()
        start = min(start, (today - dt.timedelta(days=ecfg.get("lookback_days", 45))).isoformat())
    end = today.isoformat()
    types = [t for t in ecfg["types"] if t != "业绩说明"]
    products = sorted({r["product"] for r in store.rows["product_revenue"] if not r["product"].startswith("Other")})
    recent = [{"date": e["date"], "title_en": e["title_en"]} for e in existing if e["date"] >= start]
    instructions = (
        "你是礼来（Eli Lilly and Company）财务看板的事件编辑。用网络搜索查找礼来在给定日期范围内发布的重大新闻稿，"
        "只采用礼来官网投资者关系新闻稿（investor.lilly.com 的 news-release-details 页面）或 SEC 文件。"
        "只收录这些类型：" + "；".join("%s＝%s" % (t, EVENT_TYPES_HELP[t]) for t in types) + "。"
        "不要收录：分红、参加会议或在会议上展示数据的预告、业绩电话会预告、人事任命、一般性合作和公益新闻。"
        "每条事件：date 用新闻稿正文电头里的发布日期（YYYY-MM-DD），不要凭记忆；type 从给定类型里选；"
        "product 填相关产品名，优先用给定产品清单里的写法，多个用“、”分隔，公司层面的事件填空字符串；"
        "title 用一句简洁、客观的中文（不超过 40 个汉字），公司名写“礼来”，其中出现的数字必须同样出现在 title_en 里；"
        "title_en 填新闻稿原标题（逐字）；url 填你实际打开过的那篇新闻稿的网址。"
        "已经收录的事件不要重复。找不到就返回空列表。只输出 JSON。")
    user = {"date_range": [start, end], "known_products": products, "already_listed": recent,
            "output_format": {"events": [{"date": "YYYY-MM-DD", "type": "获批", "product": "Mounjaro", "title": "中文一句话",
                                          "title_en": "Original headline", "url": "https://investor.lilly.com/news-releases/news-release-details/..."}]}}
    r = gpt.respond(instructions, json.dumps(user, ensure_ascii=False), web_search=True, schema=_event_schema(types),
                    allowed_domains=["investor.lilly.com", "lilly.com", "sec.gov"], max_tokens=8000)
    items = (r["data"] or {}).get("events") or []
    seen = {V.norm_url(u) for u in r["sources"] + r["citations"]}
    known_urls = {V.norm_url(e["url"]) for e in existing}
    known_keys = {(e["date"], e["type"], e["product"]) for e in existing}
    ids = {e["id"] for e in existing}
    accepted, rejected = [], []
    for it in items:
        why = _event_problem(it, start, end, types, ecfg["allowed_domains"], seen, known_urls, known_keys)
        if not why:
            try:
                why = confirm_event(gpt, it)
            except Exception as e:  # 核对调用失败就这次不收，下次检索还会再找到
                why = "核对调用失败：%s" % e
        if why:
            rejected.append({"item": it, "reason": why})
            continue
        url = V.norm_url(it["url"])
        base = "e%s-%s" % (it["date"], V.slug(it["product"].split("、")[0] if it["product"] else url.rsplit("/", 1)[-1], 16))
        eid, k = base, 2
        while eid in ids:
            eid, k = "%s-%d" % (base, k), k + 1
        ids.add(eid)
        known_urls.add(url)
        known_keys.add((it["date"], it["type"], it["product"]))
        accepted.append({"id": eid, "date": it["date"], "quarter": V.quarter_of_date(it["date"]), "type": it["type"],
                         "product": it["product"].strip(), "title": V.cjk_spacing(it["title"].strip()), "title_en": it["title_en"].strip(), "url": it["url"].strip()})
        if len(accepted) >= ecfg.get("max_new_per_run", 6):
            break
    return accepted, rejected, {"start": start, "end": end, "sources": len(seen), "model": r["model"]}


# 规则里不收录的新闻稿，按英文原标题的固定写法挡掉（实测 GPT 曾把“Lilly to showcase ... data at ACR”这种会议预告收进来）
_EXCLUDE = re.compile(r"\b(?:to|will) (?:showcase|present|highlight|share|host)\b|\bdividend|\bconference call\b|\bwebcast\b"
                      r"|\bboard of directors\b|\bexecutive committee\b|\bchief [a-z]+ officer\b"
                      r"|\bto (?:announce|report) (?:first|second|third|fourth)[- ]quarter", re.I)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def dateline_date(s):
    """从新闻稿电头读出日期：'INDIANAPOLIS, Sept. 25, 2026 /PRNewswire/' → '2026-09-25'；读不出返回 None。"""
    m = re.search(r"\b(%s)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b" % "|".join(_MONTHS), (s or "").lower())
    if not m:
        return None
    try:
        return dt.date(int(m.group(3)), _MONTHS.index(m.group(1)) + 1, int(m.group(2))).isoformat()
    except ValueError:
        return None


def same_headline(a, b):
    a, b = V.norm_text(a), V.norm_text(b)
    if len(a) < 12 or len(b) < 12:
        return False
    if a in b or b in a:
        return True
    ta, tb = set(re.findall(r"[a-z0-9]+", a)), set(re.findall(r"[a-z0-9]+", b))
    return len(ta & tb) / len(ta | tb) >= 0.7


def confirm_event(gpt, it):
    """第二次单独核对：只给 GPT 网址，让它打开新闻稿、逐字读出电头和标题；日期由程序从电头里解析。
    实测一次检索里 4 条有 2 条日期是错的（月份错），所以日期不能只听检索那一次。通过返回 None，否则返回原因。"""
    schema = {"type": "object", "additionalProperties": False, "required": ["dateline", "headline"],
              "properties": {"dateline": {"type": "string"}, "headline": {"type": "string"}}}
    r = gpt.respond("打开给定的礼来新闻稿网址，逐字摘录正文开头的电头（地点和日期，例如 'INDIANAPOLIS, Sept. 25, 2026'）和新闻稿标题。"
                    "打不开就都填空字符串，不要凭记忆填写。只输出 JSON。", it["url"], web_search=True, schema=schema,
                    allowed_domains=["lilly.com", "sec.gov"], max_tokens=2000)
    d = r["data"] or {}
    if V.norm_url(it["url"]) not in {V.norm_url(u) for u in r["sources"] + r["citations"]}:
        return "核对时没有打开这篇新闻稿"
    day = dateline_date(d.get("dateline"))
    if not day:
        return "核对时读不出新闻稿电头里的日期"
    if day != it["date"]:
        return "日期对不上：检索给出 %s，新闻稿电头是 %s" % (it["date"], day)
    if not same_headline(d.get("headline", ""), it["title_en"]):
        return "英文标题和新闻稿对不上：%s" % (d.get("headline") or "")[:80]
    return None


def _event_problem(it, start, end, types, domains, seen, known_urls, known_keys):
    if not isinstance(it, dict):
        return "格式不对"
    for k in ("date", "type", "product", "title", "title_en", "url"):
        if not isinstance(it.get(k), str):
            return "缺少字段 %s" % k
    if _EXCLUDE.search(it["title_en"]):
        return "属于不收录的新闻（会议预告、分红、电话会或人事）"
    if not V.is_date(it["date"]) or not (start <= it["date"] <= end):
        return "日期 %s 不在 %s ~ %s" % (it["date"], start, end)
    if it["type"] not in types:
        return "类型 %s 不在允许范围" % it["type"]
    url = V.norm_url(it["url"])
    if not url or not V.host_allowed(url, domains):
        return "网址不是礼来官网或 SEC：%s" % it["url"]
    if url not in seen:
        return "网址不在 GPT 实际检索到的来源里（可能是编的）：%s" % it["url"]
    if url in known_urls or (it["date"], it["type"], it["product"]) in known_keys:
        return "已经收录"
    if not V.has_cjk(it["title"]) or len(it["title"]) > 60:
        return "中文标题为空或过长"
    if not it["title_en"].strip() or len(it["title_en"]) > 300:
        return "英文标题为空或过长"
    bad = V.numbers_supported(it["title"], [it["title_en"]])
    if bad:
        return "中文标题里的数字在原标题里找不到：%s" % "、".join(bad)
    return None


def earnings_note(gpt, text, quarter, filing_date, url, known_products):
    """有值得标注的一次性项目时返回一条“业绩说明”事件，否则返回 None。返回 (event, reason)。"""
    schema = {"type": "object", "additionalProperties": False, "required": ["notable", "product", "title", "title_en", "evidence"],
              "properties": {"notable": {"type": "boolean"}, "product": {"type": "string"}, "title": {"type": "string"},
                             "title_en": {"type": "string"}, "evidence": {"type": "array", "items": {"type": "string"}}}}
    n = int(quarter[5])
    instructions = (
        "你在读礼来的季度业绩新闻稿。判断本季度报告的收入、营业利润或每股收益里，是否有需要提醒读者注意的特殊项目："
        "例如出售产品权益的收入、里程碑款、返利估计的大额调整、金额很大的外购在研（IPR&D）费用、税法变化影响。"
        "常规的增长、指引调整、产品获批都不算。没有就返回 notable=false，其余字段填空。"
        "有的话：title 写一句中文（不超过 60 个汉字），以“%s收入/利润含……”的口吻说明项目和金额，金额用“亿美元”，"
        "每股金额写“每股 X 美元”；只用新闻稿里出现的数字，不要自己换算或相乘；"
        "项目名称照新闻稿的说法翻译（例如 acquired IPR&D charges 写作“外购在研项目（IPR&D）费用”）；"
        "只有新闻稿原文写了 one-time 的项目才能用“一次性”这个词，否则 title 里不要出现它；"
        "product 填相关产品（没有就空字符串）；title_en 填新闻稿的英文标题；"
        "evidence 逐字摘录新闻稿里支撑每个数字的原句（每句 20~300 个字符，不要改写）。只输出 JSON。" % ("第%s季度" % "一二三四"[n - 1]))
    r = gpt.respond(instructions, text[:60000], schema=schema, max_tokens=4000)
    d = r["data"] or {}
    if not d.get("notable"):
        return None, "GPT 判断本季度没有需要单独标注的一次性项目"
    ev = [q for q in d.get("evidence") or [] if isinstance(q, str)]
    if not ev or not all(V.quote_in(q, text) for q in ev):
        return None, "引用的原句在新闻稿里找不到，放弃"
    title = (d.get("title") or "").strip()
    bad = V.numbers_supported(title, ev)
    if bad or not V.has_cjk(title) or len(title) > 80:
        return None, "中文说明里的数字与原文不符：%s" % "、".join(bad)
    if "一次性" in title and not re.search(r"one[- ]time|non-?recurring", V.norm_text(" ".join(ev))):
        return None, "中文说明称“一次性”，但引用的原文没有这么说，放弃"
    prod = (d.get("product") or "").strip()
    if prod and prod not in known_products and prod.lower() not in V.norm_text(text):
        prod = ""
    return {"id": "e%s-q%d" % (filing_date, n), "date": filing_date, "quarter": quarter, "type": "业绩说明", "product": prod,
            "title": V.cjk_spacing(title), "title_en": (d.get("title_en") or "").strip()[:300], "url": url}, "ok"


def product_context(text, name, width=350, limit=4):
    """新闻稿里提到某产品的每一处（最多 4 处）前后文，拼在一起给 classify_area。
    只看第一处不够：例如 Inluriyo 第一次出现在 CEO 的一句话里，说明它是乳腺癌药的句子在后面。"""
    low, key, out, i = text.lower(), name.lower(), [], 0
    while key and len(out) < limit:
        i = low.find(key, i)
        if i < 0:
            break
        out.append(text[max(0, i - width): i + len(name) + width].strip())
        i += len(name) + width
    return " … ".join(out) if out else text[:3000]


def classify_area(gpt, product, areas, context):
    schema = {"type": "object", "additionalProperties": False, "required": ["area"],
              "properties": {"area": {"type": "string", "enum": list(areas) + ["unknown"]}}}
    r = gpt.respond("判断礼来的这个产品属于哪个治疗领域，只能从给定选项里选。依据新闻稿段落和你对这个产品（通用名、适应症）的了解；"
                    "新闻稿没说清、你也不确定时，用网络搜索在 lilly.com 上查它的适应症。查了还拿不准就选 unknown。只输出 JSON。",
                    "产品：%s\n新闻稿相关段落：%s" % (product, context[:3000]), web_search=True, schema=schema,
                    allowed_domains=["lilly.com"], max_tokens=3000)
    a = (r["data"] or {}).get("area")
    return a if a in areas else None


PARTS = {"volume": "销量", "price": "价格", "rate": "汇率"}
_PART_WORDS = {"volume": ("volume",), "price": ("price",), "rate": ("exchange rate", "foreign exchange", "currenc")}
_NEG = re.compile(r"decreas|declin|lower|unfavorabl|negative|reduc")
_POS = re.compile(r"increas|growth|grew|higher|(?<!un)favorabl|positive")
_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent)")
_CLAUSE = re.compile(r",|;|\.\s+|\s+(?:and|while|but|partially offset by|offset by)\s+")


def part_supported(quote, part, value):
    """某一项（销量/价格/汇率）的数字是否真由原句给出：把原句按逗号、and、offset by 等切成分句，
    要有一个分句同时含这个百分数和这一项的关键词、不含别的项，而且增减方向一致。
    例：“a 50% increase in volume, partially offset by a 7% decrease due to lower realized prices” 里 50 只能是销量、-7 只能是价格。"""
    for clause in _CLAUSE.split(V.norm_text(quote)):
        if not any(w in clause for w in _PART_WORDS[part]):
            continue
        if any(w in clause for k, ws in _PART_WORDS.items() if k != part for w in ws):
            continue
        if not any(abs(float(m.group(1)) - abs(value)) <= 0.05 for m in _PCT.finditer(clause)):
            continue
        neg, pos = bool(_NEG.search(clause)), bool(_POS.search(clause))
        if (neg and not pos and value > 0) or (pos and not neg and value < 0):
            continue
        return True
    return False


def q4_prv(gpt, text, growth):
    """从第四季度新闻稿读出美国、美国以外、全球的销量/价格/汇率变化（小数）。growth：由收入算出的各地区增速，用来核对。
    每一项单独核对：原句里写明且对得上的才采用；新闻稿没写的项留空（None），不推算。返回 ({地区: 拆分}, 问题列表)。"""
    reg = {"type": "object", "additionalProperties": False, "required": ["region", "volume", "price", "rate", "total_change", "quote"],
           "properties": {"region": {"type": "string", "enum": ["U.S.", "Outside U.S.", "Total Revenue"]},
                          "volume": {"type": ["number", "null"]}, "price": {"type": ["number", "null"]},
                          "rate": {"type": ["number", "null"]}, "total_change": {"type": ["number", "null"]},
                          "quote": {"type": "string"}}}
    schema = {"type": "object", "additionalProperties": False, "required": ["regions"],
              "properties": {"regions": {"type": "array", "items": reg}}}
    instructions = (
        "这是礼来第四季度业绩新闻稿。找出第四季度（不是全年）收入变化的拆分：美国（U.S.）、美国以外（Outside U.S.）、"
        "全球（Total Revenue）各自的销量（volume）、价格（price）、汇率（rate）对收入增速的贡献，以及合计增速（total_change），"
        "都用百分数的数值，下降用负数（例如价格下降 7% 填 -7）。只填新闻稿明确写出的数字；没写的项填 null，"
        "不要用合计减去其他项推算。quote 逐字摘录给出这些数字的那一句原文（包括合计增速）。只输出 JSON。")
    r = gpt.respond(instructions, text[:60000], schema=schema, max_tokens=4000)
    out, problems = {}, []
    for it in ((r["data"] or {}).get("regions") or []):
        g = it.get("region")
        if g not in growth or g in out:
            continue
        quote = it.get("quote") or ""
        if not V.quote_in(quote, text):
            problems.append("%s：原句找不到" % g)
            continue
        vals = {}
        for k in PARTS:
            v = it.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                if part_supported(quote, k, v):
                    vals[k] = float(v)
                elif v != 0:   # 0 多半是“没写”的意思，不算问题
                    problems.append("%s：%s %s 在原句里对不上，留空" % (g, PARTS[k], v))
        if not vals:
            problems.append("%s：没有能在原句里核实的拆分数字" % g)
            continue
        if g == "U.S." and "rate" not in vals:
            vals["rate"] = 0.0   # 美国收入以美元计，没有汇率影响
        total = it.get("total_change")
        if isinstance(total, (int, float)) and not isinstance(total, bool):
            if not any(abs(float(m.group(1)) - abs(total)) <= 0.05 for m in _PCT.finditer(V.norm_text(quote))):
                problems.append("%s：合计增速 %s 不在原句里，改用收入算出的增速" % (g, total))
                total = None
        else:
            total = None
        if total is None and len(vals) == 3:
            total = sum(vals.values())
        if total is not None and growth.get(g) is not None and abs(total / 100 - growth[g]) > 0.011:
            problems.append("%s：合计增速 %s%% 与收入算出的 %.1f%% 不符" % (g, total, growth[g] * 100))
            continue
        if total is not None and len(vals) == 3 and abs(sum(vals.values()) - total) > 1.5:
            problems.append("%s：三项之和与合计不符" % g)
            continue
        out[g] = {k: (round(vals[k] / 100, 4) if k in vals else None) for k in PARTS}
        out[g]["total_change"] = round(total / 100, 4) if total is not None else None
    return out, problems


def selftest(gpt):
    """检查密钥、模型和联网搜索是否可用。"""
    out = {"model": gpt.resolve_model()}
    r = gpt.respond("只回答 JSON。", '回答 {"ok": true}', max_tokens=500)
    out["basic"] = bool((r["data"] or {}).get("ok"))
    r = gpt.respond("用网络搜索找出礼来最近一次季度业绩新闻稿在 investor.lilly.com 上的网址。只输出 JSON：{\"url\": \"...\", \"title_en\": \"...\"}",
                    "Eli Lilly latest quarterly financial results press release", web_search=True,
                    allowed_domains=["investor.lilly.com", "lilly.com"], max_tokens=4000)
    url = V.norm_url((r["data"] or {}).get("url", ""))
    seen = {V.norm_url(u) for u in r["sources"] + r["citations"]}
    out["web_search"] = {"url": url, "sources": len(seen), "url_in_sources": url in seen,
                         "domain_ok": bool(url) and V.host_allowed(url, ["lilly.com"])}
    out["usage"] = dict(gpt.usage)
    out["log"] = list(gpt.log)
    return out
