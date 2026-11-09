"""读 10-K/10-Q 的 XBRL 实例（EDGAR 提取的 *_htm.xml）和标签、定义链接库，取出收入构成。只用标准库。

- 实例：上下文（期间 + 维度成员）、单位、事实（科目、上下文、数值、精度 decimals）。
- 标签链接库（*_lab.xml；有的公司嵌在 .xsd 里）：科目/成员 -> 公司自己写的名称（jnj:StelaraMember -> “STELARA”）。
- 定义链接库（*_def.xml）：维度下成员的上下级关系（有的公司写了，例如吉利德“产品 -> HIV 合计 -> 各产品”）。

收入构成的取法（每份申报的本期：10-Q 的本季度 3 个月和年初至今，10-K 的全年）
1. 只看收入科目（Revenues、RevenueFromContractWithCustomer…）带一个维度的数；“合并项目”维度为“经营分部”的视同不带
   （只是说明口径），为抵销、公司层面、调节项的不要。
2. 三个方向：产品（ProductOrServiceAxis）、业务分部（StatementBusinessSegmentsAxis）、地区（StatementGeographicalAxis、
   GeographicDistributionAxis）。分部 × 子分部、产品组合 × 产品这类两个维度的数，作为产品方向（组 = 外层成员）。
3. 同一个维度上常常既有合计成员又有明细成员（“美国 + 国际”和“美国、欧洲、日本……”同时出现）：找各成员之和等于总收入
   （容差：每个数半个舍入单位，按 decimals）的最细一组——先看全部成员；再看定义链接库里的末级成员；再试去掉一部分
   合计类成员（成员少时逐个组合试，成员多时只试名称像合计、或是标准合计成员的），取保留成员最多的一组。
   找不到就这一期这个方向不写（提醒），不会写出重复相加的数。
"""
import io
import itertools
import re
import xml.etree.ElementTree as ET

XBRLI = "http://www.xbrl.org/2003/instance"
LINK = "http://www.xbrl.org/2003/linkbase"
XLINK = "http://www.w3.org/1999/xlink"
XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
DOMAIN_MEMBER = "http://xbrl.org/int/dim/arcrole/domain-member"
LABEL_ROLES = ("http://www.xbrl.org/2003/role/terseLabel", "http://www.xbrl.org/2003/role/label",
               "http://www.xbrl.org/2003/role/verboseLabel")
STANDARD_NS = {"http://fasb.org/us-gaap/": "us-gaap", "http://fasb.org/srt/": "srt", "http://xbrl.sec.gov/dei/": "dei",
               "http://xbrl.sec.gov/country/": "country", "http://xbrl.sec.gov/ecd/": "ecd", "http://xbrl.sec.gov/stpr/": "stpr"}

REVENUE_CONCEPTS = ("us-gaap:Revenues", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                    "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax")
COST_CONCEPTS = ("us-gaap:CostOfGoodsAndServicesSold", "us-gaap:CostOfRevenue")
AXES = {
    "product": ("srt:ProductOrServiceAxis",),
    "segment": ("us-gaap:StatementBusinessSegmentsAxis",),
    "geography": ("srt:StatementGeographicalAxis", "us-gaap:GeographicDistributionAxis", "srt:GeographicDistributionAxis"),
}
SUB_AXES = ("us-gaap:SubsegmentsAxis", "us-gaap:ReportingUnitAxis")       # 分部下的子分部，作为产品方向（组 = 分部）
NEUTRAL = {"ConsolidationItemsAxis": ("us-gaap:OperatingSegmentsMember", "srt:ReportableGeographicalComponentsMember")}
AGGREGATE_WORDS = re.compile(r"^(total\b.*|.*\btotal|products?|(net |gross )?product sales|net product revenues?|product revenues?|"
                             r"services?|service revenues?|international|int'?l|non-?u\.?s\.?|outside (the )?u\.?s\.?|ous|"
                             r"rest of (the )?world|worldwide|global|americas|reportable segments?\b.*|segments? total|.*\bportfolio)$", re.I)
AGGREGATE_MEMBERS = {"us-gaap:ProductMember", "us-gaap:ServiceMember", "srt:NonUsMember", "srt:AmericasMember",
                     "srt:NorthAmericaMember", "srt:EuropeMember", "srt:AsiaPacificMember"}


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _ns(tag):
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def _as_file(data):
    return io.BytesIO(data if isinstance(data, bytes) else data.encode("utf-8"))


def half_unit(decimals, value=None):
    """半个舍入单位（百万）：按 decimals（-6 表示精确到百万）；没有 decimals 或写的是 INF 时，按数值本身能整除的最大单位推断
    （100 万、10 万、1 万、1000、1 元）。"""
    try:
        d = int(decimals)
        if d <= 0:
            return 0.5 * 10 ** (-d) / 1e6
    except (TypeError, ValueError):
        pass
    if value is None:
        return 0.0
    v = abs(value)
    for p in (1e6, 1e5, 1e4, 1e3):
        if abs(v / p - round(v / p)) < 1e-6:
            return 0.5 * p / 1e6
    return 0.5e-6


class Instance:
    """一份 XBRL 实例。facts: [{concept, ctx, unit, decimals, value}]；contexts: id -> {start, end, instant, dims {轴: 成员}}；
    dei: 封面信息（DocumentPeriodEndDate、DocumentFiscalPeriodFocus 等）。科目、成员一律写成“前缀:本地名”，标准前缀统一。"""

    def __init__(self, data):
        self.contexts, self.units, self.facts, self.prefixes, self.dei = {}, {}, [], {}, {}
        self._parse(data)

    def _prefix(self, uri):
        for base, p in STANDARD_NS.items():
            if uri.startswith(base):
                return p
        return self.prefixes.get(uri) or uri.rstrip("/").rsplit("/", 1)[-1]

    def qname(self, text, nsmap):
        if ":" not in text:
            return text
        p, local = text.split(":", 1)
        uri = nsmap.get(p)
        return "%s:%s" % (self._prefix(uri) if uri else p, local)

    def _parse(self, data):
        nsmap = {}
        for ev, item in ET.iterparse(_as_file(data), events=("start-ns", "end")):
            if ev == "start-ns":
                p, uri = item
                nsmap[p] = uri
                if p and uri not in self.prefixes:
                    self.prefixes[uri] = p
                continue
            el = item
            ns, tag = _ns(el.tag), _local(el.tag)
            if ns == XBRLI and tag == "context":
                self._context(el, nsmap)
                el.clear()
            elif ns == XBRLI and tag == "unit":
                measures = [m.text.strip() for m in el.iter("{%s}measure" % XBRLI) if m.text]
                self.units[el.get("id")] = "/".join(m.split(":")[-1] for m in measures)
                el.clear()
            elif el.get("contextRef") is not None:
                text = (el.text or "").strip()
                concept = "%s:%s" % (self._prefix(ns), tag)
                if concept.startswith("dei:") and text:
                    self.dei.setdefault(tag, text)
                if el.get(XSI_NIL) == "true" or not text:
                    el.clear()
                    continue
                try:
                    value = float(text)
                except ValueError:
                    el.clear()
                    continue
                self.facts.append({"concept": concept, "ctx": el.get("contextRef"),
                                   "unit": self.units.get(el.get("unitRef"), el.get("unitRef")),
                                   "decimals": el.get("decimals"), "value": value})
                el.clear()

    def _context(self, el, nsmap):
        c = {"start": None, "end": None, "instant": None, "dims": {}}
        for x in el.iter():
            t = _local(x.tag)
            if t == "startDate":
                c["start"] = x.text.strip()
            elif t == "endDate":
                c["end"] = x.text.strip()
            elif t == "instant":
                c["instant"] = x.text.strip()
            elif t == "explicitMember":
                c["dims"][self.qname(x.get("dimension"), nsmap)] = self.qname(x.text.strip(), nsmap)
            elif t == "typedMember":
                c["dims"][self.qname(x.get("dimension"), nsmap)] = "typed"
        self.contexts[el.get("id")] = c

    def fact_rows(self):
        """事实 + 上下文：{concept, start, end, dims, value, decimals, unit}（instant 的 start 为 None）。"""
        for f in self.facts:
            c = self.contexts.get(f["ctx"])
            if c is not None:
                yield dict(f, start=c["start"], end=c["end"] or c["instant"], dims=c["dims"])


def _href_concept(href):
    """链接库里 loc 的 href（…xsd#jnj_StelaraMember）-> “jnj:StelaraMember”。"""
    frag = href.split("#", 1)[-1]
    if "_" not in frag:
        return frag
    p, local = frag.split("_", 1)
    return "%s:%s" % (p, local)


def parse_labels(data):
    """标签链接库（或内嵌标签链接库的 .xsd）-> {科目: {角色: 英文文字}}。"""
    labels = {}
    root = ET.parse(_as_file(data)).getroot()
    for link in root.iter("{%s}labelLink" % LINK):
        locs, res, arcs = {}, {}, []
        for el in link:
            t = _local(el.tag)
            lab = el.get("{%s}label" % XLINK)
            if t == "loc":
                locs[lab] = _href_concept(el.get("{%s}href" % XLINK) or "")
            elif t == "label":
                lang = el.get("{http://www.w3.org/XML/1998/namespace}lang") or "en"
                if lang.lower().startswith("en"):
                    res.setdefault(lab, []).append((el.get("{%s}role" % XLINK), " ".join((el.text or "").split())))
            elif t == "labelArc":
                arcs.append((el.get("{%s}from" % XLINK), el.get("{%s}to" % XLINK)))
        for fr, to in arcs:
            c = locs.get(fr)
            if c:
                for role, text in res.get(to, []):
                    labels.setdefault(c, {})[role] = text
    return labels


def parse_definitions(data):
    """定义链接库（或内嵌的 .xsd）-> 父成员 -> [子成员]（所有角色里的 domain-member 关系合在一起）。"""
    children = {}
    root = ET.parse(_as_file(data)).getroot()
    for link in root.iter("{%s}definitionLink" % LINK):
        locs = {}
        for el in link:
            if _local(el.tag) == "loc":
                locs[el.get("{%s}label" % XLINK)] = _href_concept(el.get("{%s}href" % XLINK) or "")
        for el in link:
            if _local(el.tag) == "definitionArc" and el.get("{%s}arcrole" % XLINK) == DOMAIN_MEMBER:
                fr, to = locs.get(el.get("{%s}from" % XLINK)), locs.get(el.get("{%s}to" % XLINK))
                if fr and to and to not in children.setdefault(fr, []):
                    children[fr].append(to)
    return children


def best_label(labels, concept):
    """名称：优先简短标签，其次标准标签；去掉结尾的“[Member]”；都没有就把科目名拆成词。"""
    roles = labels.get(concept) or {}
    for r in LABEL_ROLES:
        if roles.get(r):
            return re.sub(r"\s*\[(Member|Domain|Axis)\]\s*$", "", roles[r]).strip()
    local = re.sub(r"(Member|Domain)$", "", concept.split(":", 1)[-1])
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", local).strip()


def member_key(qname):
    """segments.csv 的 member：成员科目的本地名去掉 Member（跨期稳定，名称改写也不受影响）。"""
    return re.sub(r"Member$", "", qname.split(":", 1)[-1]) or qname


# ---------------- 收入构成 ----------------
OTHER_MEMBER = "OtherNotItemized"        # 只列了一部分时，其余合并的一行（derived）
OTHER_REVENUE = "OtherRevenue"            # 拆的是小计时，总收入里小计以外的部分（derived）


def _scope(dims):
    """事实的口径：consolidated（合并口径，没有“合并项目”维度）/ segments（合并项目 = 经营分部，只是分部口径）/ None（抵销、调节项等，不要）。
    返回 (口径, 去掉合并项目维度后的维度)。"""
    out, scope = {}, "consolidated"
    for axis, member in dims.items():
        local = axis.split(":", 1)[-1]
        if local in NEUTRAL:
            if member not in NEUTRAL[local]:
                return None, None
            scope = "segments"
            continue
        out[axis] = member
    return scope, out


def _custom_axis(axis):
    """公司自定义的轴：名称像“产品组合/领域/类别”的是组（group），像“产品/品牌”的是产品（product），其余不用。标准轴返回 None。"""
    prefix, local = axis.split(":", 1) if ":" in axis else ("", axis)
    if prefix in ("us-gaap", "srt", "dei", "country", "ecd"):
        return None
    if re.search(r"Portfolio|Franchise|Therap|Area|Category|Group|Division|Business|Platform", local):
        return "group"
    if re.search(r"Product|Brand|Drug|Medicine|Compound", local):
        return "product"
    return None


def _axis_family(axis):
    for fam, names in AXES.items():
        if axis in names:
            return fam
    return None


def _descendants(children, node, seen=None):
    seen = seen if seen is not None else set()
    for c in children.get(node, []):
        if c not in seen:
            seen.add(c)
            _descendants(children, c, seen)
    return seen


def looks_aggregate(qname, label):
    """名称或标准成员表明是合计（“产品合计”“国际”“非美国”“服务”等）。"""
    return qname in AGGREGATE_MEMBERS or bool(AGGREGATE_WORDS.search(label or ""))


def find_partition(members, total, total_half, children=None, labels=None, max_drop=4, min_cover=0.9):
    """members: {键: (值, 半个舍入单位, 成员科目, ...)}。返回 (选中的键, 方法, 是否不全)；拆不出来返回 (None, 原因, False)。
    1) 全部成员之和 = 总数；2) 定义链接库里的末级成员之和 = 总数；3) 去掉 1~4 个合计成员后之和 = 总数（保留成员最多的一组）；
    4) 只列了一部分：去掉名称像合计的成员后，之和小于总数、且至少占总数九成 —— 接受，差额作为“其他（未单列）”另列。
    “=”的容差：参与的每个数半个舍入单位。"""
    labels = labels or {}
    keys = list(members)
    if not keys:
        return None, "没有成员", False

    def gap(sel):
        s = sum(members[k][0] for k in sel)
        return s - total, sum(members[k][1] for k in sel) + total_half + 1e-6

    def exact(sel):
        d, tol = gap(sel)
        return abs(d) <= tol

    name = lambda k: best_label(labels, members[k][2])
    agg = lambda k: looks_aggregate(members[k][2], name(k))
    if exact(keys):
        return keys, "全部成员", False
    if children:
        present = {members[k][2] for k in keys}
        leaves = [k for k in keys if not (_descendants(children, members[k][2]) & present)]
        if leaves != keys and exact(leaves):
            return leaves, "定义链接库的末级成员", False
    cands = keys if len(keys) <= 14 else [k for k in keys if agg(k)][:14]
    for n in range(1, min(max_drop, len(cands)) + 1):
        found = []
        for drop in itertools.combinations(cands, n):
            keep = [k for k in keys if k not in drop]
            if not keep or not exact(keep):
                continue
            if any(agg(k) for k in keep) and not all(agg(k) for k in drop):
                continue            # 留下了像合计的成员、却去掉了像明细的成员：方向反了，不要
            found.append((sum(agg(k) for k in drop), drop, keep))
        if found:
            found.sort(key=lambda x: -x[0])
            sig = lambda f: sorted((name(k), round(members[k][0], 3)) for k in f[2])
            if len(found) > 1 and found[0][0] == found[1][0] and sig(found[0]) != sig(found[1]):
                return None, "有多种拆法都对得上总数，无法判断（%s）" % " / ".join(
                    "+".join(name(k) for k in f[2]) for f in found[:2]), False
            return found[0][2], "去掉合计成员（%s）" % "、".join(name(k) for k in found[0][1]), False
    keep = [k for k in keys if not agg(k)]
    if keep and total > 0:
        d, tol = gap(keep)
        covered = sum(members[k][0] for k in keep)
        if d < -tol and covered >= min_cover * total and all(members[k][0] > -tol for k in keep):
            return keep, "只列了一部分（占 %.0f%%），其余合并为“其他（未单列）”" % (100.0 * covered / total), True
    return None, "成员之和对不上总收入", False


def filing_breakdown(inst, labels, children, period_end, statement_revenue=None, exclude=None):
    """一份申报里各期间的收入构成（period_end 为 None 时取全部期间，包括以后申报里的上年同期对比数）。
    exclude: {方向: {成员科目}}——以前判定过（例如 GPT 提议、核对通过）的合计成员，先去掉再拆。
    返回 (结果, 提醒, 待判定)：结果 {(start, end): {方向: {"members": [...], "method", "concept", "total"}}}；
    待判定 [{period, family, total, members: [(成员科目, 名称, 值)]}]——拆不出来的，可交给 GPT 判断哪些是合计成员。"""
    by_period, totals = {}, {}
    for r in inst.fact_rows():
        if r["concept"] not in REVENUE_CONCEPTS or not r["start"] or len(r["unit"] or "") != 3:
            continue
        if period_end is not None and r["end"] != period_end:
            continue
        key = (r["start"], r["end"])
        scope, dims = _scope(r["dims"])
        if scope is None:
            continue
        value = r["value"] / 1e6                    # 一律换算成百万（容差也是百万）
        if not dims:
            if scope == "consolidated":
                totals.setdefault((key, r["concept"]), (value, half_unit(r["decimals"], r["value"])))
            continue
        by_period.setdefault(key, []).append((r["concept"], scope, dims, value, half_unit(r["decimals"], r["value"])))
    out, warns, pending = {}, [], []
    names = {"product": "产品", "segment": "分部", "geography": "地区"}
    for key, facts in sorted(by_period.items()):
        # 合并总收入：同一科目的不带维度的数；没有就用别的收入科目的（例如明细标在 RevenueFromContract…、合计标在 Revenues），再没有用利润表的
        any_total = next((totals[(key, c)] for c in REVENUE_CONCEPTS if (key, c) in totals), None)
        if any_total is None and statement_revenue and key in statement_revenue:
            any_total = (statement_revenue[key], 0.5)
        axes, why, fails = {}, {}, {}
        for concept in REVENUE_CONCEPTS:
            tot = totals.get((key, concept)) or any_total
            if tot is None:
                continue
            got, reasons, failed = _breakdowns([f for f in facts if f[0] == concept], tot, labels, children, exclude or {})
            for fam, b in got.items():
                b = dict(b, concept=concept, total=tot[0])
                if any_total and tot is not any_total and any_total[0] - tot[0] > tot[1] + any_total[1] + 1e-6:
                    # 拆的是一个列示出来的小计（例如“产品收入”），总收入里其余部分 = 两个列示的合计之差，另列一行
                    b["members"] = b["members"] + [{"member": OTHER_REVENUE, "qname": "", "name": "Other revenue", "group": "",
                                                    "value": any_total[0] - tot[0], "half": tot[1] + any_total[1], "basis": "derived"}]
                    b["method"] += "；其余收入 = 总收入 − 该小计"
                    b["total"] = any_total[0]
                    b["cover"] = tot[0] / any_total[0] if any_total[0] else 0
                else:
                    b["cover"] = 1.0
                rank = (b["cover"] > 0.999, not b["partial"], b["cover"])
                if fam not in axes or rank > axes[fam]["_rank"]:
                    axes[fam] = dict(b, _rank=rank)
            for fam, r in reasons.items():
                why.setdefault(fam, r)
            for fam, f in failed.items():
                fails.setdefault(fam, dict(f, total=tot[0], total_half=tot[1]))
        for fam, r in why.items():
            if fam not in axes:
                warns.append("%s~%s %s：%s" % (key[0], key[1], names[fam], r))
                if fam in fails:
                    pending.append(dict(fails[fam], period=key, family=fam))
        if axes:
            for b in axes.values():
                b.pop("_rank", None)
            out[key] = axes
    return out, warns, pending


def _breakdowns(facts, tot, labels, children, exclude):
    """一个收入科目、一个期间：各方向的拆分。候选：单一维度（合并口径、分部口径）、两个维度（分部 × 子分部、产品组合 × 产品）；
    能对上总数的优先，其次是只列了一部分的（覆盖越多越好）；同等情况下成员多的优先。
    返回 ({方向: 拆分}, {方向: 拆不出来的原因}, {方向: 待判定的成员})。"""
    total, total_half = tot
    single = {}       # (方向, 口径) -> {键: (值, 半单位, 成员, None)}
    nested = {}       # (外层轴, 内层轴, 口径) -> {键: (值, 半单位, 内层成员, 外层成员)}
    for concept, scope, dims, value, half in facts:
        if len(dims) == 1:
            (axis, member), = dims.items()
            fam = _axis_family(axis)
            if fam and member not in exclude.get(fam, ()):
                single.setdefault((fam, scope), {}).setdefault(member, (value, half, member, None))
        elif len(dims) == 2:
            axes = sorted(dims)
            seg = [a for a in axes if _axis_family(a) == "segment"]
            inner = [a for a in axes if a in SUB_AXES or _axis_family(a) == "product"]
            outer = None
            if seg and inner and seg[0] != inner[0]:
                outer, inner_ax = seg[0], inner[0]
            elif len(inner) == 1:
                o = [a for a in axes if a != inner[0]][0]
                kind = _custom_axis(o)
                if kind == "group":          # 公司自定义的产品组合轴（例如艾伯维 KeyProductPortfolioAxis）：组合 × 产品
                    outer, inner_ax = o, inner[0]
                elif kind == "product":      # 公司自定义的产品名称轴（例如百济神州 ProductNameAxis）：产品名在自定义轴上
                    outer, inner_ax = inner[0], o
            if outer and dims[inner_ax] not in exclude.get("product", ()):
                k = "%s|%s" % (dims[outer], dims[inner_ax])
                nested.setdefault((outer, inner_ax, scope), {}).setdefault(k, (value, half, dims[inner_ax], dims[outer]))
    cands = []        # (方向, 成员表, 方法前缀, 组函数)
    for (fam, scope), mem in single.items():
        cands.append((fam, mem, "", scope))
    for (outer, inner, scope), mem in nested.items():
        vals = dict(mem)
        for m, v in (single.get(("product", scope)) or {}).items():     # 同一产品轴上单独列示的成员（例如“其他”）一并考虑
            if not any(x[2] == m for x in vals.values()):
                vals[m] = v
        cands.append(("product", vals, "（%s × %s）" % (outer.split(":")[-1], inner.split(":")[-1]),
                      scope if _custom_axis(inner) != "product" else scope + "-type"))
    best, why, failed = {}, {}, {}
    for fam, mem, suffix, scope in cands:
        if len(mem) < 2:
            continue
        sel, how, partial = find_partition(mem, total, total_half, children, labels)
        if not sel:
            why.setdefault(fam, how)
            if not suffix and scope == "consolidated" and fam not in failed:
                failed[fam] = {"members": [(v[2], best_label(labels, v[2]), v[0]) for v in mem.values()]}
            continue
        covered = sum(mem[k][0] for k in sel)
        score = (not partial, covered, len(sel), scope.startswith("consolidated"))
        if fam not in best or score > best[fam][0]:
            if suffix:      # 组 = 外层成员（产品名在自定义轴上时，外层是收入类型，不作为组）
                groups = {k: best_label(labels, mem[k][3]) if mem[k][3] and not scope.endswith("-type") else "" for k in sel}
            else:
                groups = _groups_from_children(sel, mem, children, labels)
            best[fam] = (score, _result(mem, sel, how + suffix, partial, total, labels, lambda k: groups.get(k, "")))
    out = {fam: b[1] for fam, b in best.items()}
    for fam in out:
        why.pop(fam, None)
        failed.pop(fam, None)
    return out, why, failed


def _result(mem, sel, how, partial, total, labels, group_of):
    rows, seen = [], {}
    for k in sel:
        q = mem[k][2]
        key = member_key(q)
        seen[key] = seen.get(key, 0) + 1
        rows.append({"member": key, "qname": q, "name": best_label(labels, q), "group": group_of(k), "value": mem[k][0],
                     "half": mem[k][1], "basis": "reported"})
    for r in rows:                      # 同一个成员出现在不同的外层下（例如两个分部都有“其他”），键加上组名区分
        if seen[r["member"]] > 1 and r["group"]:
            r["member"] = "%s/%s" % (re.sub(r"\W+", "", r["group"]), r["member"])
    if partial:
        rest = total - sum(r["value"] for r in rows)
        rows.append({"member": OTHER_MEMBER, "qname": "", "name": "Other (not itemized)", "group": "", "value": rest,
                     "half": sum(r["half"] for r in rows), "basis": "derived"})
    return {"members": rows, "method": how, "partial": partial}


GENERIC_GROUP = re.compile(r"^(products?|product sales|net product (sales|revenues?)|product revenues?|services?|service revenues?|"
                           r"revenues?|net revenues?|total revenues?|product and service|products and services)$", re.I)


def _groups_from_children(sel, mem, children, labels):
    """定义链接库里末级成员往上找最近的、不是泛称（“产品”“产品销售”等）的上级，作为组（例如吉利德“Total HIV”→“HIV”）。"""
    groups = {}
    if not children:
        return groups
    parents = {}
    for p, kids in children.items():
        for k in kids:
            parents.setdefault(k, []).append(p)
    for k in sel:
        frontier, seen = list(parents.get(mem[k][2], [])), set()
        while frontier:
            p = frontier.pop(0)
            if p in seen:
                continue
            seen.add(p)
            if p.endswith(("Domain", "Axis")):
                continue
            name = re.sub(r"^total\s+", "", best_label(labels, p), flags=re.I).strip()
            if name and not GENERIC_GROUP.match(name):
                groups[k] = name
                break
            frontier.extend(parents.get(p, []))
    return groups


def plain_facts(inst):
    """不带维度的 us-gaap 数（companyfacts 漏了某份申报时用来补）+ 只按“产品/服务”标准成员拆开、没有合计的营业成本之和。
    返回 {科目: {(start, end): (值, 单位种类)}}。"""
    out = {}
    split = {}
    for r in inst.fact_rows():
        c = r["concept"]
        if not c.startswith("us-gaap:"):
            continue
        tag = c.split(":", 1)[1]
        unit = r["unit"] or ""
        kind = "shares" if unit == "shares" else ("per_share" if "/shares" in unit else "money")
        if not r["dims"]:
            out.setdefault(tag, {}).setdefault((r["start"], r["end"]), (r["value"], kind))
        elif c in COST_CONCEPTS and list(r["dims"]) == ["srt:ProductOrServiceAxis"]:
            split.setdefault((tag, r["start"], r["end"]), {})[r["dims"]["srt:ProductOrServiceAxis"]] = r["value"]
    for (tag, start, end), parts in split.items():
        if set(parts) == {"us-gaap:ProductMember", "us-gaap:ServiceMember"} and (start, end) not in out.get(tag, {}):
            out.setdefault(tag, {})[(start, end)] = (sum(parts.values()), "money")
    return out
