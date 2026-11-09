"""美股适配器（公司清单里 source 为 sec 的公司）：SEC 10-K/10-Q 的 XBRL 数据 → 统一格式。

每天（update）
1. 身份：用 SEC 的代码清单（company_tickers.json）由美股代码查 CIK；看申报清单是 10-K/10-Q 还是 20-F
   （20-F 外国发行人第 5 阶段接入，本阶段跳过、不算失败）。CIK、公司名、财年截止日记在 state/<公司>/state.json。
2. 有没有新的 10-K/10-Q：没有、而且程序没改过，就不动数据。
3. 有：三张表取自 companyfacts（sec_facts.py）；每份 10-K/10-Q 的 XBRL 实例拆出收入构成（sec_xbrl.py），结果存在
   state/<公司>/sec_segments.json（以后不用重新下载）；companyfacts 漏掉的申报，用实例里的数补上。
4. 核对（“必过”项，任何一项不过这家公司本次不写）：统一格式勾稽（框架）、季度与年初至今一致、净利润差额能用税后项目解释、
   资产 = 负债 + 权益、收入构成每个方向之和 = 同期利润表营收（收入构成和利润表取自同一份申报）。
   程序改动后第一次运行时先跑一遍礼来回测（用礼来已核对的数据检验解析器，见 backtest），不通过就不写。

GPT（ctx.gpt）只做判断，结果都要经过数值核对才采用，并记在 state.json 里以后直接用：
- 收入构成拆不开时（同一维度上叠着几种拆法），判断哪些成员是合计；去掉后其余成员之和必须等于总收入。
- 产品归到治疗领域（组）：只能从给定的领域里选，每个产品恰好一个。
"""
import datetime as dt
import hashlib
import json
import os
import re

from ..pipeline.sec import SEC, SecError
from . import sec_facts as F
from . import sec_xbrl as X
from .base import Result

FIRST_YEAR = 2021                 # 从 2021 财年开始补数据
EXTRACT_VERSION = 1               # 收入构成提取规则的版本；改了提取规则就加 1，旧申报会重新下载、重新拆
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEG_FILE = "sec_segments.json"
LLY = {"id": "lly", "cik": "0000059478", "ticker": "LLY"}
FOREIGN_FORMS = ("20-F", "40-F", "6-K")      # 6-K 只有外国发行人申报
AREAS = ["Oncology", "Immunology", "Neuroscience", "Cardiometabolic Health", "Cardiovascular", "Infectious Disease", "Vaccines",
         "Hematology", "Rare Disease", "Ophthalmology", "Respiratory", "Women's Health", "Animal Health", "Aesthetics",
         "Established Products", "Other"]
_REFERENCE = {}                   # 礼来回测结果（每次运行只做一次）


# ---------------- 工具 ----------------
def code_hash():
    """解析程序的指纹：sec*.py 有改动就变，用来决定要不要重新回测、重新生成数据。"""
    h = hashlib.sha1()
    d = os.path.dirname(os.path.abspath(__file__))
    for name in ("sec.py", "sec_facts.py", "sec_xbrl.py"):
        with open(os.path.join(d, name), "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:12]


def make_client(ctx):
    scfg = (ctx.cfg or {}).get("sec", {})
    return SEC(cache_dir=os.path.join(ctx.root, "agent", ".cache", "sec"), min_interval=scfg.get("min_interval_seconds", 0.25),
               retries=scfg.get("retries", 4))


def us_ticker(company):
    for l in company.get("listings") or []:
        if l.get("exchange") in ("NYSE", "NASDAQ"):
            return l["code"]
    return None


def _cik10(cik):
    return "%010d" % int(cik)


def _read_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_json(path, obj, compact=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if compact:
        text = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    else:
        text = json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    old = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            old = f.read()
    if old != text:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)


def nominal_month(end):
    """财年截止日对应的月份：离哪个月的月底最近（强生 2025-12-28、2023-01-01 都算 12 月）。"""
    d = dt.date.fromisoformat(end)
    best = None
    for delta in (-1, 0, 1):
        y, mth = d.year, d.month + delta
        if mth == 0:
            y, mth = y - 1, 12
        if mth == 13:
            y, mth = y + 1, 1
        last = (dt.date(y + (mth == 12), mth % 12 + 1, 1) - dt.timedelta(days=1))
        dist = abs((last - d).days)
        if best is None or dist < best[0]:
            best = (dist, mth)
    return best[1]


# ---------------- 身份 ----------------
def identify(company, ctx, sec, state):
    """返回 (身份信息, 申报清单 JSON)。身份信息：cik、name、ticker、kind（10-K / 20-F）、fiscal_year_end。"""
    ticker = us_ticker(company)
    if not ticker:
        raise SecError("公司清单里没有美股代码")
    cik = company.get("sec_cik") or (state.get("cik") if state.get("ticker") == ticker else None)
    if not cik or state.get("identity_checked", "") < (ctx.today - dt.timedelta(days=30)).isoformat():
        data = sec.json(TICKERS_URL, ttl=7 * 86400)
        found = [v for v in data.values() if str(v.get("ticker", "")).upper() == ticker.upper()]
        if not found:
            raise SecError("SEC 代码清单（company_tickers.json）里查不到 %s" % ticker)
        listed = _cik10(found[0]["cik_str"])
        if company.get("sec_cik") and company["sec_cik"] != listed:
            raise SecError("公司清单的 CIK %s 与 SEC 代码清单（%s -> %s）不一致" % (company["sec_cik"], ticker, listed))
        cik = listed
        state["identity_checked"] = ctx.today.isoformat()
    subs = sec.json("https://data.sec.gov/submissions/CIK%s.json" % _cik10(cik), ttl=0)
    recent = subs.get("filings", {}).get("recent", {})
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    since = (ctx.today - dt.timedelta(days=3 * 366)).isoformat()
    recent_forms = {f for f, d in zip(forms, dates) if d >= since}
    if recent_forms & {"10-K", "10-Q"}:
        kind = "10-K"
    elif recent_forms & set(FOREIGN_FORMS):
        kind = "20-F"
    else:
        kind = None
    info = {"cik": _cik10(cik), "name": subs.get("name"), "ticker": ticker, "kind": kind,
            "fiscal_year_end": subs.get("fiscalYearEnd"), "sic": subs.get("sicDescription")}
    return info, subs


def periodic_filings(subs, facts, first_start):
    """2021 财年以来的 10-K/10-Q（不含修订）：申报清单（最近一页）和 companyfacts 里出现过的申报编号合在一起。
    返回 [{accn, form, filed, report_date}]，按申报日期排序。"""
    out = {}
    r = subs.get("filings", {}).get("recent", {})
    for i, form in enumerate(r.get("form") or []):
        if form in ("10-K", "10-Q"):
            acc = r["accessionNumber"][i]
            out[acc] = {"accn": acc, "form": form, "filed": r["filingDate"][i], "report_date": r["reportDate"][i]}
    if facts is not None:
        ends = facts.accn_period_end()
        for acc, f in facts.filings.items():
            if f["form"] in ("10-K", "10-Q") and acc not in out:
                out[acc] = {"accn": acc, "form": f["form"], "filed": f["filed"], "report_date": ends.get(acc, "")}
    keep = [f for f in out.values() if (f["report_date"] or f["filed"]) >= first_start]
    return sorted(keep, key=lambda f: (f["filed"], f["accn"]))


# ---------------- 每份申报的收入构成 ----------------
def filing_files(sec, cik, accn):
    """申报目录里的 XBRL 实例、标签链接库、定义链接库（没有单独的链接库文件时用内嵌链接库的 .xsd）。"""
    folder = "https://www.sec.gov/Archives/edgar/data/%d/%s/" % (int(cik), accn.replace("-", ""))
    idx = sec.json(folder + "index.json")
    names = [it.get("name", "") for it in (idx.get("directory") or {}).get("item") or []]
    inst = [n for n in names if n.endswith("_htm.xml")]
    xsd = [n for n in names if n.endswith(".xsd")]
    lab = [n for n in names if n.endswith("_lab.xml")] or xsd
    deff = [n for n in names if n.endswith("_def.xml")] or xsd
    return folder, (inst or [None])[0], (lab or [None])[0], (deff or [None])[0]


def _period_end(inst, filing):
    d = (inst.dei.get("DocumentPeriodEndDate") or "").strip()
    return d if re.match(r"^\d{4}-\d{2}-\d{2}$", d) else filing.get("report_date")


def extract_filing(sec, cik, filing, statement_revenue, exclude):
    """下载并拆一份申报。返回 (提取结果, 实例里的不带维度数 {科目: {(start, end): (值, 种类)}})。
    提取结果：{v, form, filed, period_end, periods: {"start|end": {方向: [方法, 科目, 总数, [[member, qname, name, group, value, half, basis]]]}},
              warns: [...], pending: [...]}"""
    folder, inst_name, lab_name, def_name = filing_files(sec, cik, filing["accn"])
    out = {"v": EXTRACT_VERSION, "form": filing["form"], "filed": filing["filed"], "periods": {}, "warns": [], "pending": []}
    if not inst_name:
        out["warns"].append("申报里没有 XBRL 实例")
        return out, {}
    inst = X.Instance(sec.get(folder + inst_name))
    labels = X.parse_labels(sec.get(folder + lab_name)) if lab_name else {}
    children = X.parse_definitions(sec.get(folder + def_name)) if def_name else {}
    out["period_end"] = _period_end(inst, filing)
    got, warns, pending = X.filing_breakdown(inst, labels, children, None, statement_revenue, exclude)
    for (start, end), axes in got.items():
        out["periods"]["%s|%s" % (start, end)] = {
            fam: [b["method"], b["concept"], round(b["total"], 6),
                  [[m["member"], m["qname"], m["name"], m["group"], round(m["value"], 6), round(m["half"], 6), m["basis"]]
                   for m in b["members"]]]
            for fam, b in axes.items()}
    out["warns"] = warns[:20]
    out["pending"] = [{"period": "%s|%s" % p["period"], "family": p["family"], "total": p["total"], "total_half": p["total_half"],
                       "members": p["members"]} for p in pending][:6]
    return out, X.plain_facts(inst)


# ---------------- 收入构成 -> 统一格式 ----------------
def build_segments(built, seg_state, currency, groups=None):
    """按利润表每个期间取数的申报对齐收入构成：同一份申报里有就用它，没有就用总数等于该期营收的那份。
    第四季度 = 全年 − 9 个月（两边成员相同才推算）。返回 (rows, notes, problems)。"""
    groups = groups or {}
    versions = {}
    for accn, ext in (seg_state.get("filings") or {}).items():
        for pk, axes in (ext.get("periods") or {}).items():
            versions.setdefault(pk, []).append((ext.get("filed", ""), accn, ext.get("form", ""), axes))
    rows, notes, problems = [], [], []
    picked = {}            # (财年, 期间, 方向) -> (成员列表, 出处申报, 总数)
    for (lab, p), u in sorted(built.used.items()):
        if p not in ("Q1", "Q2", "Q3", "9M", "FY") or u.get("revenue") is None:
            continue
        cands = versions.get("%s|%s" % (u["start"], u["end"]), [])
        for fam in ("product", "segment", "geography"):
            have = [(filed, accn, form, axes[fam]) for filed, accn, form, axes in cands if fam in axes]
            if not have:
                continue
            match = [h for h in have if abs(h[3][2] - u["revenue"]) <= u["tol"] + sum(m[5] for m in h[3][3]) + 0.5 + 1e-6]
            same = [h for h in match if h[1] == u["accn"]]
            chosen = (same or sorted(match, reverse=True) or [None])[0]
            if chosen is None:
                problems.append("%s %s %s：收入构成的总数与利润表营收 %.1f 对不上（%s）" % (
                    lab, p, fam, u["revenue"], "、".join("%.1f" % h[3][2] for h in have[:3])))
                continue
            picked[(lab, p, fam)] = (chosen[3][3], chosen[1], chosen[2], chosen[3][0])
    forms = {accn: ext.get("form", "") for accn, ext in (seg_state.get("filings") or {}).items()}
    for (lab, p, fam), (members, accn, form, method) in sorted(picked.items()):
        if p == "9M":
            continue
        u = built.used[(lab, p)]
        rows += _segment_rows(lab, p, u["end"], 12 if p == "FY" else 3, fam, members, currency, "SEC %s %s" % (form, accn), groups)
        if p == "FY" and (lab, "9M", fam) in picked:
            nine, accn9 = picked[(lab, "9M", fam)][0], picked[(lab, "9M", fam)][1]
            a = {m[0]: m for m in members}
            b = {m[0]: m for m in nine}
            if set(a) != set(b):
                notes.append("%s 第四季度%s：全年和前三季度的成员不同，不推算" % (lab, {"product": "产品", "segment": "分部", "geography": "地区"}[fam]))
                continue
            q4 = [[k, a[k][1], a[k][2], a[k][3], round(a[k][4] - b[k][4], 6), a[k][5] + b[k][5], "derived"] for k in a]
            rows += _segment_rows(lab, "Q4", u["end"], 3, fam, q4, currency,
                                  "SEC %s %s − %s %s" % (form, accn, forms.get(accn9, ""), accn9), groups, basis="derived")
    return rows, notes, problems


def _segment_rows(lab, fp, end, months, fam, members, currency, source, groups, basis=None):
    out, seen = [], set()
    for member, qname, name, group, value, half, b in members:
        key = member
        n = 2
        while key in seen:
            key = "%s~%d" % (member, n)
            n += 1
        seen.add(key)
        g = group if fam == "product" else ""
        if fam == "product" and not g:
            g = groups.get(key, "")
        out.append({"fiscal_year": str(lab), "fiscal_period": fp, "period_end": end, "months": months, "axis": fam, "member": key,
                    "name_en": name, "name_zh": "", "group": g, "value": value, "currency": currency, "unit": "millions",
                    "basis": basis or b, "source": source})
    return out


def check_segments(seg_rows, income_rows):
    """每个期间、每个方向的收入构成之和 = 同期利润表营收（容差：每个成员 1 个百万单位的一半，加上营收本身的半个单位；
    推算的期间按两期各算一次）。返回问题列表。"""
    inc = {(r["fiscal_year"], r["fiscal_period"]): r for r in income_rows}
    sums = {}
    for r in seg_rows:
        k = (r["fiscal_year"], r["fiscal_period"], r["axis"])
        s, n = sums.get(k, (0.0, 0))
        sums[k] = (s + float(r["value"]), n + 1)
    probs = []
    for (fy, fp, axis), (s, n) in sorted(sums.items()):
        r = inc.get((fy, fp))
        if not r or r.get("revenue") is None:
            continue
        terms = n + 1
        tol = 0.5 * terms * (2 if fp == "Q4" else 1) + 1e-6
        if abs(s - float(r["revenue"])) > tol:
            probs.append("%s %s %s：收入构成之和 %.1f ≠ 营收 %.1f" % (fy, fp, axis, s, float(r["revenue"])))
    return probs


# ---------------- GPT：判断合计成员、产品归类 ----------------
def gpt_aggregates(gpt, company, item):
    """收入构成拆不开时：让 GPT 判断哪些成员是合计或另一种拆法。返回要去掉的成员科目列表（未核对）。"""
    members = [{"id": q, "name": n, "value_millions": round(v, 3)} for q, n, v in item["members"]]
    instructions = ("你在核对美国上市公司 XBRL 里的收入构成。同一个维度上列了几组成员：有的成员是其他成员的合计（例如“产品销售合计”、"
                    "“国际”、“某产品组合”），有的是另一种拆法（例如按收入类型拆）。请选出应该去掉的成员，使剩下的成员互不重叠、"
                    "合起来正好等于总收入，并且尽量细（保留到具体产品或地区）。只能从给定成员里选，用它们的 id。只输出 JSON。")
    user = {"company": company.get("name_en"), "total_revenue_millions": round(item["total"], 3), "members": members,
            "output_format": {"exclude": ["<member id>"], "reason": "一句话"}}
    schema = {"type": "object", "additionalProperties": False, "required": ["exclude", "reason"],
              "properties": {"exclude": {"type": "array", "items": {"type": "string"}}, "reason": {"type": "string"}}}
    r = gpt.respond(instructions, json.dumps(user, ensure_ascii=False), schema=schema, max_tokens=3000)
    return [x for x in ((r.get("data") or {}).get("exclude") or []) if isinstance(x, str)]


def verify_aggregates(item, exclude):
    """去掉这些成员后，其余成员之和必须在四舍五入范围内等于总收入（不能是“只列了一部分”）。"""
    names = {q for q, n, v in item["members"]}
    if not exclude or not set(exclude) <= names:
        return False, "提议的成员不在清单里"
    keep = [(q, v) for q, n, v in item["members"] if q not in exclude]
    s = sum(v for q, v in keep)
    tol = 0.5 * (len(keep) + 1) + 1e-6
    if abs(s - item["total"]) > tol:
        return False, "去掉后其余成员之和 %.1f ≠ 总收入 %.1f" % (s, item["total"])
    return True, ""


def gpt_groups(gpt, company, products, areas):
    """产品归到治疗领域。products: [(成员科目, 名称)]。返回 {成员科目: 领域}（未核对）。"""
    instructions = ("给医药公司的每个产品或收入项目选一个治疗领域，只能从给定领域里选；不是具体药品的收入（合作收入、特许权使用费、"
                    "其他收入）选 Other。只输出 JSON。")
    user = {"company": company.get("name_en"), "areas": areas, "items": [{"id": q, "name": n} for q, n in products],
            "output_format": {"groups": [{"id": "<item id>", "area": "<one of areas>"}]}}
    schema = {"type": "object", "additionalProperties": False, "required": ["groups"],
              "properties": {"groups": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                                   "required": ["id", "area"],
                                                                   "properties": {"id": {"type": "string"}, "area": {"type": "string"}}}}}}
    r = gpt.respond(instructions, json.dumps(user, ensure_ascii=False), schema=schema, max_tokens=4000)
    return {g["id"]: g["area"] for g in ((r.get("data") or {}).get("groups") or []) if isinstance(g, dict)}


def verify_groups(products, proposed, areas):
    """每个产品恰好一个领域，领域在给定清单里。返回 (通过的 {成员: 领域}, 问题列表)。"""
    ok, probs = {}, []
    ids = {q for q, n in products}
    for q, area in proposed.items():
        if q not in ids:
            probs.append("未知的产品 %s" % q)
        elif area not in areas:
            probs.append("%s 的领域 %s 不在清单里" % (q, area))
        else:
            ok[q] = area
    missing = ids - set(ok)
    if missing:
        probs.append("%d 个产品没有归类" % len(missing))
    return (ok if not probs else {}), probs


# ---------------- 一家公司 ----------------
class Run:
    """一次运行里一家公司的数据：身份、companyfacts、收入构成状态、组装结果。"""

    def __init__(self, company, ctx, sec, res):
        self.co, self.ctx, self.sec, self.res = company, ctx, sec, res
        self.cid = company["id"]
        self.state = ctx.load_state(self.cid)
        self.seg_path = os.path.join(ctx.state_dir(self.cid), SEG_FILE)
        self.seg = _read_json(self.seg_path, {"filings": {}})

    def load(self):
        info, subs = identify(self.co, self.ctx, self.sec, self.state)
        self.info, self.subs = info, subs
        self.state.update({k: v for k, v in info.items() if k != "kind"})
        self.state["form_type"] = info["kind"]
        return info

    def facts(self, refresh=True):
        """companyfacts（每次运行下载一次）+ 以前从 XBRL 实例补进来的数。"""
        if refresh or not hasattr(self, "_cf"):
            self._cf = self.sec.json("https://data.sec.gov/api/xbrl/companyfacts/CIK%s.json" % self.info["cik"], ttl=0)
        self.f = F.Facts(self._cf)
        for accn, ext in (self.seg.get("filings") or {}).items():       # 以前从实例补进来的数
            extra = {}
            for tag, start, end, val, kind in ext.get("extra") or []:
                extra.setdefault(tag, {})[(start, end)] = (val, kind)
            if extra:
                self.f.add_instance(accn, ext.get("form", ""), ext.get("filed", ""), extra)
        return self.f

    def segments(self, filings, built):
        """处理还没拆过（或提取规则改过）的申报。返回本次新处理的申报数。"""
        done = self.seg.setdefault("filings", {})
        exclude = {k: set(v) for k, v in (self.state.get("segment_exclude") or {}).items()}
        stmt_rev = {}
        for (lab, p), u in built.used.items():
            if u.get("revenue") is not None:
                stmt_rev[(u["start"], u["end"])] = u["revenue"]
        n = 0
        for fl in filings:
            old = done.get(fl["accn"])
            if old and old.get("v") == EXTRACT_VERSION:
                continue
            ext, plain = extract_filing(self.sec, self.info["cik"], fl, stmt_rev, exclude)
            missing = fl["accn"] not in self.f.filings
            extra = []
            for tag, periods in plain.items():
                for (start, end), (val, kind) in periods.items():
                    if missing or (tag in ("CostOfGoodsAndServicesSold", "CostOfRevenue") and
                                   not any(r[1] == fl["accn"] for r in self.f.by_tag.get(tag, {}).get((start, end), []))):
                        extra.append([tag, start, end, val, kind])
            if missing:
                self.res.notes.append(("数据说明", ["companyfacts 里没有 %s %s（%s），已用这份申报的 XBRL 实例补上" % (
                    fl["form"], fl["accn"], fl["filed"])]))
            ext["extra"] = extra
            done[fl["accn"]] = ext
            n += 1
        return n

    def resolve_pending(self):
        """收入构成拆不开的：让 GPT 判断合计成员，核对通过就记下来，并把相关申报重新拆。返回是否有新的判定。"""
        gpt = self.ctx.gpt
        if gpt is None or not self.ctx.gpt_ok("segments"):
            return False
        learned = self.state.setdefault("segment_exclude", {})
        changed = False
        tried = set()
        for accn, ext in sorted((self.seg.get("filings") or {}).items(), key=lambda x: x[1].get("filed", ""), reverse=True):
            for item in ext.get("pending") or []:
                fam = item["family"]
                if fam in tried or any(q in set(learned.get(fam, [])) for q, n, v in item["members"]):
                    continue
                tried.add(fam)
                try:
                    proposal = gpt_aggregates(gpt, self.co, item)
                except Exception as e:  # GPT 偶发失败只提醒
                    self.res.warnings.append("GPT 判断收入构成的合计成员失败：%s" % e)
                    continue
                ok, why = verify_aggregates(item, proposal)
                if ok:
                    learned[fam] = sorted(set(learned.get(fam, [])) | set(proposal))
                    changed = True
                    self.res.notes.append(("GPT 判定（已核对）", ["%s 方向去掉合计成员：%s" % (fam, "、".join(proposal))]))
                else:
                    self.res.warnings.append("GPT 提议的收入构成拆法没有通过核对（%s），这一方向本次不写" % why)
        if changed:
            for ext in (self.seg.get("filings") or {}).values():
                if ext.get("pending"):
                    ext["v"] = 0          # 有待判定的申报重新拆
        return changed

    def classify(self, seg_rows):
        """没有组的产品，让 GPT 归到治疗领域（只对医药类公司），核对通过才用。"""
        cats = set(self.co.get("categories") or [])
        if not cats & {"big_pharma_us", "big_pharma_eu", "rna", "cell_therapy", "gene_rare", "china_innovative"}:
            return {}
        groups = dict(self.state.get("product_groups") or {})
        todo = {}
        for r in seg_rows:
            if r["axis"] == "product" and not r["group"] and r["member"] not in groups and not r["member"].startswith("Other"):
                todo[r["member"]] = r["name_en"]
        gpt = self.ctx.gpt
        if todo and gpt is not None and self.ctx.gpt_ok("classify"):
            items = sorted(todo.items())
            try:
                ok, probs = verify_groups(items, gpt_groups(gpt, self.co, items, AREAS), AREAS)
                if ok:
                    groups.update(ok)
                    self.state["product_groups"] = groups
                    self.res.notes.append(("GPT 判定（已核对）", ["%d 个产品归入治疗领域" % len(ok)]))
                else:
                    self.res.warnings.append("GPT 给产品归类没有通过核对：%s" % "；".join(probs[:3]))
            except Exception as e:  # GPT 偶发失败只提醒
                self.res.warnings.append("GPT 给产品归类失败：%s" % e)
        return groups

    def save(self):
        self.ctx.save_state(self.cid, self.state)
        _write_json(self.seg_path, self.seg, compact=True)


def build(company, ctx, sec, res, write_state=True, force=False):
    """取数、拆分、组装、核对。返回 rows（没有新申报且不需要重建时为 None）。"""
    run = Run(company, ctx, sec, res)
    info = run.load()
    if info["kind"] == "20-F":
        res.skipped = "20-F 外国发行人：第 5 阶段接入"
        if write_state:
            run.save()
        return None
    if info["kind"] is None:
        res.error("近三年没有 10-K/10-Q 或 20-F 申报，无法确定数据来源")
        return None
    facts = run.facts()
    built = F.build_statements(facts, FIRST_YEAR, run.state.get("mapping"))
    first_start = built.calendar[0][1] if built.calendar else "%d-01-01" % FIRST_YEAR
    filings = periodic_filings(run.subs, facts, first_start)
    known = set((run.seg.get("filings") or {}))
    new = [f["accn"] for f in filings if f["accn"] not in known]
    h = code_hash()
    have_data = os.path.exists(os.path.join(ctx.data_dir(company["id"]), "income.csv"))
    if not new and run.state.get("code_hash") == h and have_data and not force:
        res.notes.append(("SEC", ["没有新的 10-K/10-Q（最近一份：%s）" % (filings[-1]["filed"] if filings else "无")]))
        run.state["last_check"] = ctx.today.isoformat()
        if write_state:
            run.save()
        return None
    n = run.segments(filings, built)
    if run.resolve_pending():
        n += run.segments(filings, built)
    if any(ext.get("extra") for ext in run.seg["filings"].values()):
        facts = run.facts(refresh=False)
        built = F.build_statements(facts, FIRST_YEAR, run.state.get("mapping"))
    rows, tols = F.split_tol(built.rows)
    groups = run.classify([])
    seg_rows, seg_notes, seg_probs = build_segments(built, run.seg, facts.currency, groups)
    new_groups = run.classify(seg_rows)
    if new_groups != groups:
        seg_rows, seg_notes, seg_probs = build_segments(built, run.seg, facts.currency, new_groups)
    rows["segments"] = seg_rows
    # 核对
    from ..core import schema as S
    for name in ("income", "balance", "cashflow", "segments"):
        probs = S.problems(name, rows[name])
        res.check("统一格式核对：%s" % name, not probs, "；".join(probs[:6]))
    for e in built.errors:
        res.error(e)
    seg_check = check_segments(seg_rows, rows["income"])
    res.check("收入构成之和 = 营收", not seg_check, "；".join(seg_check[:6]))
    res.warnings += built.warns + seg_probs[:10]
    fye = run.state.get("fiscal_year_end")
    if built.calendar and built.calendar[-2:] and company.get("fiscal_year_end_month"):
        ends = [e for lab, s, e in built.calendar if e]
        if ends and nominal_month(ends[-1]) != company["fiscal_year_end_month"]:
            res.warnings.append("公司清单的财年截止月 %s 与申报（%s）不一致" % (company["fiscal_year_end_month"], ends[-1]))
    lines = ["CIK %s，%s；%d 份 10-K/10-Q（本次新拆 %d 份）" % (info["cik"], info["name"], len(filings), n),
             "利润表 %d 行、资产负债表 %d 行、现金流量表 %d 行、收入构成 %d 行" % (
                 len(rows["income"]), len(rows["balance"]), len(rows["cashflow"]), len(seg_rows))]
    if rows["income"]:
        lines.append("期间：%s %s ~ %s %s" % (rows["income"][0]["fiscal_year"], rows["income"][0]["fiscal_period"],
                                           rows["income"][-1]["fiscal_year"], rows["income"][-1]["fiscal_period"]))
    res.notes.append(("SEC 取数", lines + built.notes + seg_notes[:10]))
    empty = [k for k in ("revenue", "gross_profit", "operating_income", "net_income", "eps_diluted")
             if rows["income"] and all(r.get(k) is None for r in rows["income"])]
    if empty:
        res.notes.append(("没有披露（留空）", ["%s：XBRL 里没有对应的标准科目" % "、".join(empty)]))
    run.state["code_hash"] = h
    run.state["last_check"] = ctx.today.isoformat()
    run.state["periods"] = len(rows["income"])
    if write_state:
        run.save()
    run.tols = tols
    res._run = run
    return rows


# ---------------- 适配器接口 ----------------
def update(company, ctx):
    res = Result(company["id"])
    try:
        sec = make_client(ctx)
        if run_reference_needed(company, ctx):
            ref = reference_backtest(ctx, sec)
            res.check("礼来回测（解析器检验）", ref["ok"], ref["summary"])
            if not ref["ok"]:
                return res
        rows = build(company, ctx, sec, res)
    except SecError as e:
        res.warnings.append("无法访问 SEC：%s（本次不更新）" % e)
        return res
    if rows is not None and res.ok:
        res.rows, res.replace = rows, True
    return res


def run_reference_needed(company, ctx):
    """程序改过（指纹变了）以后第一次运行时要先过礼来回测。"""
    return ctx.load_state(company["id"]).get("code_hash") != code_hash()


def backtest(company, ctx):
    """回测：先用礼来（CIK 0000059478）的 SEC 数据跑一遍解析器，核心科目必须与 data/lly 里已核对的数据在四舍五入范围内一致；
    再对这家公司做勾稽、季度/全年一致、收入构成之和 = 营收的核对（不写数据）。"""
    res = Result(company["id"])
    try:
        sec = make_client(ctx)
        ref = reference_backtest(ctx, sec)
        res.check("礼来回测（解析器检验）", ref["ok"], ref["summary"])
        if ref.get("lines") and not ref.get("reported"):
            res.notes.append(("礼来回测：SEC 数据 vs 已核对数据", ref["lines"]))
            ref["reported"] = True
        if company["id"] == LLY["id"]:
            return res
        rows = build(company, ctx, sec, res, write_state=False, force=True)
    except SecError as e:
        res.error("无法访问 SEC：%s" % e)
        return res
    if rows is not None:
        old = _existing(ctx, company["id"])
        if old:
            diffs = F.compare_reference(rows, getattr(res, "_run").tols, old)
            if diffs["mismatches"]:
                res.warnings.append("与现有数据不同 %d 处（可能是重述或规则改进）：%s" % (len(diffs["mismatches"]), "；".join(diffs["mismatches"][:5])))
    st = ctx.load_state(company["id"])
    st["backtest"] = {"date": ctx.today.isoformat(), "hash": code_hash(), "ok": res.ok}
    ctx.save_state(company["id"], st)
    return res


def _existing(ctx, cid):
    out = {}
    for name in ("income", "balance"):
        p = os.path.join(ctx.data_dir(cid), name + ".csv")
        if os.path.exists(p):
            import csv
            with open(p, encoding="utf-8", newline="") as f:
                out[name] = list(csv.DictReader(f))
    return out


def reference_backtest(ctx, sec):
    """礼来回测：每次运行只做一次。"""
    key = (ctx.root, code_hash())
    if key in _REFERENCE:
        return _REFERENCE[key]
    out = {"ok": False, "summary": "", "lines": []}
    ref = _existing(ctx, LLY["id"])
    if not ref.get("income"):
        out.update(ok=True, summary="没有礼来已核对数据，跳过")
        _REFERENCE[key] = out
        return out
    cf = sec.json("https://data.sec.gov/api/xbrl/companyfacts/CIK%s.json" % LLY["cik"], ttl=0)
    built = F.build_statements(F.Facts(cf), FIRST_YEAR)
    rows, tols = F.split_tol(built.rows)
    from ..core import schema as S
    probs = [p for n in ("income", "balance", "cashflow") for p in S.problems(n, rows[n])] + built.errors
    cmp = F.compare_reference(rows, tols, ref)
    out["ok"] = not cmp["mismatches"] and not probs and cmp["core_compared"] > 0
    out["summary"] = "比较 %d 个数（核心科目 %d 个），不一致 %d 个%s" % (
        cmp["compared"], cmp["core_compared"], len(cmp["mismatches"]), "；勾稽问题 %d 个" % len(probs) if probs else "")
    missing = ["已核对数据有、SEC 标准科目里没有（留空，不算不一致）：%s" % "、".join(
        "%s %d 个" % (k, v) for k, v in sorted(cmp["missing"].items()))] if cmp["missing"] else []
    out["lines"] = ([out["summary"]] + ["不一致：" + m for m in cmp["mismatches"][:20]] + ["勾稽：" + p for p in probs[:10]] +
                    cmp["notes"][:10] + missing)
    _REFERENCE[key] = out
    return out


def check(company, ctx):
    """数据核对：收入构成之和 = 同期营收。"""
    d = ctx.data_dir(company["id"])
    if not os.path.exists(os.path.join(d, "income.csv")):
        return []
    import csv
    with open(os.path.join(d, "income.csv"), encoding="utf-8", newline="") as f:
        inc = list(csv.DictReader(f))
    p = os.path.join(d, "segments.csv")
    seg = []
    if os.path.exists(p):
        with open(p, encoding="utf-8", newline="") as f:
            seg = list(csv.DictReader(f))
    return check_segments(seg, inc)


def selftest(company, ctx):
    try:
        sec = make_client(ctx)
        st = ctx.load_state(company["id"])
        info, subs = identify(company, ctx, sec, st)
        return {"sec": "ok", "cik": info["cik"], "kind": info["kind"]}
    except SecError as e:
        return {"sec": str(e)}
