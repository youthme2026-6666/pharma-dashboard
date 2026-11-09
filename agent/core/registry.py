"""公司清单 companies/registry.json：读取与格式核对。

清单里每家公司一条：中英文名、分类、地区、各上市地代码、数据来源（适配器）、币种、财年截止月、批次、状态。
新增公司由 GPT 根据 companies/watchlist.md 提议，程序用这里的规则和官方清单核对后才写入。
"""
import json
import os
import re

STATUSES = ("active", "onboarding", "planned", "paused", "removed")
SOURCES = ("lilly_deep", "sec", "hkex", "cninfo")
REGIONS = ("US", "EU", "CN")
EXCHANGES = ("NYSE", "NASDAQ", "HKEX", "SSE", "SZSE")
CURRENCIES = ("USD", "CNY", "HKD", "EUR", "GBP", "DKK", "CHF", "JPY")
_CODE = {
    "NYSE": r"^[A-Z][A-Z.]{0,5}$",
    "NASDAQ": r"^[A-Z][A-Z.]{0,5}$",
    "HKEX": r"^\d{5}$",
    "SSE": r"^6\d{5}$",
    "SZSE": r"^[03]\d{5}$",
}


def path(root):
    return os.path.join(root, "companies", "registry.json")


def load(root):
    with open(path(root), encoding="utf-8") as f:
        return json.load(f)


def save(root, reg):
    """固定格式写回：每家公司一行，方便在 GitHub 上看改动。"""
    lines = ["{", '  "schema": %d,' % reg.get("schema", 1), '  "note": %s,' % json.dumps(reg.get("note", ""), ensure_ascii=False),
             '  "categories": [']
    cats = reg.get("categories", [])
    lines += ["    %s%s" % (json.dumps(c, ensure_ascii=False), "," if i < len(cats) - 1 else "") for i, c in enumerate(cats)]
    lines += ["  ],", '  "companies": [']
    cos = reg.get("companies", [])
    lines += ["    %s%s" % (json.dumps(c, ensure_ascii=False), "," if i < len(cos) - 1 else "") for i, c in enumerate(cos)]
    lines += ["  ]", "}"]
    with open(path(root), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def problems(reg):
    """返回格式问题列表（空表示通过）。"""
    out, ids = [], set()
    cats = {c.get("id") for c in reg.get("categories", [])}
    for c in reg.get("categories", []):
        if not (c.get("id") and c.get("zh") and c.get("en")):
            out.append("分类缺少 id/zh/en：%s" % c)
    for co in reg.get("companies", []):
        cid = co.get("id") or ""
        who = cid or co.get("name_en") or "?"
        if not re.match(r"^[a-z0-9]{2,20}$", cid):
            out.append("%s：id 只能是 2~20 个小写字母或数字" % who)
        if cid in ids:
            out.append("%s：id 重复" % who)
        ids.add(cid)
        for k in ("name_en", "name_zh"):
            if not (co.get(k) or "").strip():
                out.append("%s：缺少 %s" % (who, k))
        if not co.get("categories") or any(x not in cats for x in co["categories"]):
            out.append("%s：分类不在清单里：%s" % (who, co.get("categories")))
        if co.get("region") not in REGIONS:
            out.append("%s：地区应为 %s" % (who, "/".join(REGIONS)))
        if co.get("status") not in STATUSES:
            out.append("%s：状态应为 %s" % (who, "/".join(STATUSES)))
        if co.get("source") not in SOURCES:
            out.append("%s：数据来源应为 %s" % (who, "/".join(SOURCES)))
        if co.get("currency") not in CURRENCIES:
            out.append("%s：币种 %s 不认识" % (who, co.get("currency")))
        if not isinstance(co.get("fiscal_year_end_month"), int) or not 1 <= co["fiscal_year_end_month"] <= 12:
            out.append("%s：财年截止月应为 1~12" % who)
        if not isinstance(co.get("batch"), int):
            out.append("%s：批次应为整数" % who)
        listings = co.get("listings") or []
        if not listings:
            out.append("%s：没有上市代码" % who)
        for l in listings:
            ex, code = l.get("exchange"), str(l.get("code") or "")
            if ex not in EXCHANGES or not re.match(_CODE[ex], code):
                out.append("%s：上市代码不对：%s %s" % (who, ex, code))
        exs = {l.get("exchange") for l in listings}
        if co.get("source") == "cninfo" and not any(l.get("cninfo_org_id") for l in listings if l.get("exchange") in ("SSE", "SZSE")):
            out.append("%s：A 股来源需要巨潮 orgId" % who)
        if co.get("source") == "hkex" and "HKEX" not in exs:
            out.append("%s：港股来源需要港交所代码" % who)
        if co.get("source") in ("sec", "lilly_deep") and not exs & {"NYSE", "NASDAQ"}:
            out.append("%s：SEC 来源需要美股代码" % who)
        if co.get("sec_cik") and not re.match(r"^\d{10}$", co["sec_cik"]):
            out.append("%s：SEC CIK 应为 10 位数字" % who)
    return out


def companies(reg, status=None, source=None):
    out = []
    for co in reg.get("companies", []):
        if status and co.get("status") not in ((status,) if isinstance(status, str) else status):
            continue
        if source and co.get("source") not in ((source,) if isinstance(source, str) else source):
            continue
        out.append(co)
    return out


def get(reg, cid):
    for co in reg.get("companies", []):
        if co.get("id") == cid:
            return co
    return None


def ticker(co):
    """展示用代码：优先美股，其次 A 股、港股。"""
    order = {"NYSE": 0, "NASDAQ": 0, "SSE": 1, "SZSE": 1, "HKEX": 2}
    ls = sorted(co.get("listings") or [], key=lambda l: order.get(l.get("exchange"), 9))
    return " / ".join(("%s.HK" % l["code"] if l["exchange"] == "HKEX" else l["code"]) for l in ls)
