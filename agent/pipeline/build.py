"""把一个新季度的申报数据（已解析的表格）组装成 7 个数据文件里的新行，并做全部核对。

只有全部“必过”核对通过，这个季度才会写入 data/；否则整季不写，并在报告里列出原因。
口径说明见 agent/SPEC.md 的“数据口径”一节。金额单位：百万美元；增速、构成：小数。
"""
from .datasets import last_year, quarter_end

IS_FIELDS = ["revenue", "cost_of_sales", "gross_margin", "rnd", "msa", "acquired_iprd", "impairment_restructuring",
             "operating_income", "other_net", "pretax_income", "income_tax", "net_income", "eps_diluted", "diluted_shares_k"]
NG_FIELDS = ["ng_gross_margin", "ng_operating_income", "ng_net_income", "ng_eps_diluted"]
REGIONS = ["U.S.", "Outside U.S.", "Total Revenue"]


class Result:
    def __init__(self, quarter):
        self.quarter = quarter
        self.rows = {}       # 数据集 -> 新行
        self.drop = {}       # 数据集 -> 函数(row) 为 True 的旧行会被删掉
        self.checks = []     # {name, ok, detail, fatal}
        self.warnings = []
        self.notes = []      # 写入报告的说明（例如哪些产品是从“其他”里拆出来的）
        self.terms = {}      # 产品 -> 算出它用到了几个各自四舍五入过的公布数（回测时据此算舍入误差上限）

    def check(self, name, ok, detail="", fatal=True):
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail, "fatal": fatal})
        return ok

    @property
    def ok(self):
        return all(c["ok"] for c in self.checks if c["fatal"])

    def failed(self):
        return [c for c in self.checks if not c["ok"]]


def _close(a, b, tol):
    return a is not None and b is not None and abs(a - b) <= tol


def _col(d, key, i):
    v = d.get(key) or []
    return v[i] if i < len(v) else None


# ---------------- 利润表 ----------------
def _is_record(income, recons, i):
    rec = {k: _col(income, k, i) for k in IS_FIELDS}
    for k in ("acquired_iprd", "impairment_restructuring"):
        if rec[k] is None:
            rec[k] = 0.0
    if rec["gross_margin"] is None and rec["revenue"] is not None and rec["cost_of_sales"] is not None:
        rec["gross_margin"] = rec["revenue"] - rec["cost_of_sales"]
    if rec["operating_income"] is None and None not in (rec["gross_margin"], rec["rnd"], rec["msa"]):
        rec["operating_income"] = rec["gross_margin"] - rec["rnd"] - rec["msa"] - rec["acquired_iprd"] - rec["impairment_restructuring"]
    for k in NG_FIELDS:
        rec[k] = _col(recons, k, i)
    return rec


def _check_is(res, rec, label, tol):
    req = ["revenue", "cost_of_sales", "rnd", "msa", "operating_income", "other_net", "pretax_income", "income_tax", "net_income", "eps_diluted", "diluted_shares_k"]
    missing = [k for k in req if rec.get(k) is None]
    if not res.check("%s 利润表字段齐全" % label, not missing, "缺少：" + "、".join(missing) if missing else ""):
        return
    g, o = rec["gross_margin"], rec["operating_income"]
    res.check("%s 毛利 = 收入 − 销售成本" % label, _close(g, rec["revenue"] - rec["cost_of_sales"], tol), "%s vs %s" % (g, rec["revenue"] - rec["cost_of_sales"]))
    calc_o = g - rec["rnd"] - rec["msa"] - rec["acquired_iprd"] - rec["impairment_restructuring"]
    res.check("%s 营业利润 = 毛利 − 各项费用" % label, _close(o, calc_o, tol), "%s vs %s" % (o, calc_o))
    res.check("%s 税前利润 = 营业利润 + 其他收支" % label, _close(rec["pretax_income"], o + rec["other_net"], tol), "%s vs %s" % (rec["pretax_income"], o + rec["other_net"]))
    res.check("%s 净利润 = 税前利润 − 所得税" % label, _close(rec["net_income"], rec["pretax_income"] - rec["income_tax"], tol), "%s vs %s" % (rec["net_income"], rec["pretax_income"] - rec["income_tax"]))
    ng_missing = [k for k in NG_FIELDS if rec.get(k) is None]
    res.check("%s 非 GAAP 指标齐全" % label, not ng_missing, "缺少：" + "、".join(ng_missing) if ng_missing else "", fatal=False)


def _annual_row(year, months, rec):
    row = {"year": str(year), "months": str(months), "period_end": "%d-%s" % (year, {3: "03-31", 6: "06-30", 9: "09-30", 12: "12-31"}[months])}
    for k in IS_FIELDS + NG_FIELDS:
        row[k] = rec.get(k)
    return row


# ---------------- 分产品 ----------------
def _bucket_key(area, product):
    p = product.lower()
    return p == area.lower() or p == ("other " + area).lower() or (area == "Other" and p == "other")


def _quarter_products(res, per, ytd_prior, q):
    """返回 {(领域, 产品): [美国, 美国以外, 合计]}。第四季度 = 全年（10-K）− 前三季度（第三季度 10-Q）。"""
    n = int(q[5])
    if n < 4:
        return {k: list(v) for k, v in per["products"]["current"].items()}
    fy = per["products"]["current"]
    if not ytd_prior:
        res.check("第四季度需要第三季度 10-Q 的前三季度产品表", False, "未找到第三季度 10-Q 的前三季度分产品表")
        return {}
    nine = ytd_prior["current"]
    out, new_keys = {}, []
    for k, v in fy.items():
        base = nine.get(k)
        if base is None:
            new_keys.append(k[1])
            base = (0.0, 0.0, 0.0)
        out[k] = [round(v[i] - base[i], 1) for i in range(3)]
    gone = [k[1] for k in nine if k not in fy]
    res.check("全年与前三季度的产品清单一致", not gone, "前三季度有、全年没有：" + "、".join(gone) if gone else "")
    if new_keys:
        res.warnings.append("全年产品表新增了单列的产品（前三季度没有）：%s；其第四季度值按全年值计。" % "、".join(new_keys))
    return out


def _carve(res, prods, selected, known_areas, classify, combined_names, tol):
    """把新闻稿“重点产品”的合计从所在领域的“其他”里拆出来（只有合计，没有美国/国际拆分）。"""
    split_names = {p for (a, p) in prods if not _bucket_key(a, p)}
    buckets = {a: (a, p) for (a, p) in prods if _bucket_key(a, p)}
    plan = {}
    for name, vals in (selected or {}).items():
        if name in split_names or not vals:
            continue
        area = known_areas.get(name)
        if area is None and classify is not None:
            area = classify(name)
            if area:
                res.notes.append("新产品 %s 的治疗领域由 GPT 判定为 %s。" % (name, area))
        if area is None or area not in buckets:
            res.warnings.append("重点产品 %s 无法确定所属领域，留在“其他”里不单列。" % name)
            continue
        plan.setdefault(area, []).append((name, vals[0]))
    rows = []
    base_terms = 2 if res.quarter.endswith("Q4") else 1   # 第四季度的数是全年−前三季度
    for (area, product), v in prods.items():
        if (area, product) == buckets.get(area) and area in plan:
            carved = sum(t for _, t in plan[area])
            rest = v[2] - carved
            if rest < -tol:
                res.warnings.append("%s 的“其他”减去重点产品后为负（%.0f），本季度不拆分该领域。" % (area, rest))
                rows.append((area, combined_names.get(area, product), v[0], v[1], v[2]))
                continue
            rows.append((area, combined_names.get(area, product), None, None, round(rest, 1)))
            # 余数 = “其他”（第四季度是全年−前三季度，两个数）− 每个拆出的产品：每个公布数都各自四舍五入到百万
            res.terms[combined_names.get(area, product)] = base_terms + len(plan[area])
            for name, t in plan[area]:
                rows.append((area, name, None, None, t))
            res.notes.append("%s：从“其他”中拆出 %s（只有合计，没有美国/国际拆分）。" % (area, "、".join(n for n, _ in plan[area])))
        elif _bucket_key(area, product):
            rows.append((area, combined_names.get(area, product), v[0], v[1], v[2]))
            res.terms[combined_names.get(area, product)] = base_terms
        else:
            rows.append((area, product, v[0], v[1], v[2]))
    return rows


# ---------------- 主函数 ----------------
def build_quarter(store, q, k8, per, ytd_prior=None, cfg=None, classify=None, q4_prv=None):
    """k8：8-K 新闻稿解析结果 {income, recons, selected}；per：10-Q/10-K 解析结果 {form, income, bs, products, products_ytd, prv}。"""
    cfg = cfg or {}
    tol = cfg.get("checks", {}).get("identity_tolerance", 2)
    xtol = cfg.get("checks", {}).get("cross_source_tolerance", 2)
    gtol = cfg.get("checks", {}).get("growth_tolerance", 0.011)
    res = Result(q)
    y, n = int(q[:4]), int(q[5])
    pe = quarter_end(q)

    # 1) 利润表（季度）
    income, recons = k8["income"], k8.get("recons") or {}
    rec = _is_record(income, recons, 0)
    _check_is(res, rec, q, tol)
    res.check("非 GAAP 调节表自洽（报告净利润 + 调整项 = 非 GAAP 净利润）", recons.get("recon_ok", False), fatal=False)
    pis = per.get("income") or {}
    if n < 4 and pis:
        for k in ("revenue", "net_income", "eps_diluted"):
            res.check("8-K 与 10-Q 一致：%s" % k, _close(rec.get(k), _col(pis, k, 0), 0.011 if k == "eps_diluted" else xtol), "%s vs %s" % (rec.get(k), _col(pis, k, 0)))
    res.rows["income_statement"] = [dict(rec, quarter=q, period_end=pe)]
    res.drop["income_statement"] = lambda r: r["quarter"] == q

    # 2) 利润表（年度 / 年初至今）
    cur_i, pri_i = (0, 1) if n == 1 else (2, 3)
    ycur, ypri = _is_record(income, recons, cur_i), _is_record(income, recons, pri_i)
    if n == 4 and ycur.get("revenue") is None and pis:
        ycur = dict(_is_record(pis, {}, 0), **{k: None for k in NG_FIELDS})
        res.warnings.append("8-K 没有全年列，全年利润表改用 10-K（非 GAAP 全年值留空）。")
    _check_is(res, ycur, "%d 年%s" % (y, "全年" if n == 4 else "年初至今"), tol)
    if n == 4 and pis:
        for k in ("revenue", "net_income"):
            res.check("8-K 全年与 10-K 一致：%s" % k, _close(ycur.get(k), _col(pis, k, 0), xtol), "%s vs %s" % (ycur.get(k), _col(pis, k, 0)))
    months = 3 * n
    ann = [_annual_row(y, months, ycur)]
    if n < 4:
        ann.append(_annual_row(y - 1, months, ypri))
    else:
        old = store.row("income_annual", year=str(y - 1), months="12")
        if old is None and ypri.get("revenue") is not None:
            ann.append(_annual_row(y - 1, 12, ypri))
        elif old is not None:
            res.check("上年全年收入与已有数据一致", _close(float(old["revenue"]), ypri.get("revenue"), xtol), "%s vs %s" % (old["revenue"], ypri.get("revenue")), fatal=False)
    res.rows["income_annual"] = ann
    res.drop["income_annual"] = lambda r: (r["year"] in (str(y), str(y - 1)) and int(r["months"]) < 12) or (r["year"] == str(y) and int(r["months"]) == 12)

    # 3) 资产负债表
    bs = per.get("bs")
    if res.check("找到资产负债表", bool(bs)):
        res.check("资产 = 负债 + 权益", _close(bs["total_assets"], bs["total_liabilities"] + bs["total_equity"], tol), "%s vs %s" % (bs["total_assets"], bs["total_liabilities"] + bs["total_equity"]))
        res.check("资产合计 = 负债和权益合计", _close(bs["total_assets"], bs["total_liabilities_equity"], tol))
        neg = [k for k in ("other_current_assets", "other_noncurrent_assets", "other_current_liabilities", "other_noncurrent_liabilities") if bs[k] < -tol]
        res.check("“其他”类不为负", not neg, "、".join(neg), fatal=False)
        row = {k: v for k, v in bs.items() if not k.startswith("_")}
        res.rows["balance_sheet"] = [dict(row, quarter=q, period_end=pe)]
        res.drop["balance_sheet"] = lambda r: r["quarter"] == q

    # 4) 分产品收入
    prods = _quarter_products(res, per, ytd_prior, q)
    if prods:
        total = sum(v[2] for v in prods.values())
        res.check("分产品合计 = 总收入", _close(total, rec.get("revenue"), xtol + 1), "%.1f vs %s" % (total, rec.get("revenue")))
        negs = [p for (a, p), v in prods.items() if v[2] < -tol]
        res.check("产品收入不为负", not negs, "、".join(negs), fatal=False)
        known = store.product_areas()
        known.update((cfg.get("learned") or {}).get("product_areas") or {})
        rows = _carve(res, prods, k8.get("selected"), known, classify, cfg.get("combined_bucket_names") or {}, tol)
        res.check("拆分后合计不变", _close(sum(r[4] for r in rows), total, 1.0))
        res.rows["product_revenue"] = [{"quarter": q, "area": a, "product": p, "us": u, "intl": i, "total": t} for a, p, u, i, t in rows]
        res.drop["product_revenue"] = lambda r: r["quarter"] == q

    # 5) 价格 / 汇率 / 销量（季度）
    prv = per.get("prv") or {}
    if n < 4:
        rev = per["products"]["revenue"].get("current")
    else:
        fy_rev = per["products"]["revenue"].get("current")
        nine_rev = (ytd_prior or {}).get("revenue", {}).get("current")
        rev = tuple(round(fy_rev[i] - nine_rev[i], 1) for i in range(3)) if fy_rev and nine_rev else None
    if res.check("找到分地区收入", bool(rev)):
        res.check("美国 + 美国以外 = 合计", _close(rev[0] + rev[1], rev[2], xtol), "%s" % (rev,))
        res.check("分地区合计 = 利润表收入", _close(rev[2], rec.get("revenue"), xtol), "%s vs %s" % (rev[2], rec.get("revenue")))
        pv = []
        for i, region in enumerate(REGIONS):
            base = store.region_revenue(last_year(q), region)
            growth = round(rev[i] / base - 1, 4) if base else None
            comp = (prv.get("period") or {}).get(region) if n < 4 else (q4_prv or {}).get(region)
            row = {"quarter": q, "period_end": pe, "region": region, "revenue": rev[i], "price": None, "rate": None, "volume": None,
                   "total_change": round(growth, 2) if growth is not None else None}
            if comp:
                row.update({k: comp.get(k) for k in ("price", "rate", "volume")})
                if comp.get("total_change") is not None:
                    row["total_change"] = comp["total_change"]
                parts = [comp.get(k) for k in ("price", "rate", "volume")]
                if None not in parts and row["total_change"] is not None:
                    res.check("%s：价格 + 汇率 + 销量 ≈ 合计变化" % region, abs(sum(parts) - row["total_change"]) <= 0.015, "%s" % parts, fatal=n < 4)
                if growth is not None and row["total_change"] is not None:
                    res.check("%s：披露的增速与收入算出的增速一致" % region, abs(growth - row["total_change"]) <= gtol, "%.4f vs %.4f" % (growth, row["total_change"]), fatal=n < 4)
            elif n < 4:
                res.check("%s：找到价格/汇率/销量拆分" % region, False)
            pv.append(row)
        if n == 4 and not q4_prv:
            res.notes.append("第四季度：10-K 只披露全年的价格/汇率/销量拆分，季度只给出由收入算出的合计增速。")
        res.rows["price_volume"] = pv
        res.drop["price_volume"] = lambda r: r["quarter"] == q

    # 6) 价格 / 汇率 / 销量（年度 / 年初至今）
    if n < 4:
        ytd_rev = (per.get("products_ytd") or {}).get("revenue", {}).get("current") if n > 1 else rev
        ytd = prv.get("ytd") or {}
    else:
        r10k = prv.get("revenue") or {}
        ytd_rev = tuple(r10k.get(g) for g in REGIONS) if r10k else per["products"]["revenue"].get("current")
        ytd = prv.get("ytd") or {}
    if res.check("找到年初至今的分地区收入和拆分", bool(ytd_rev) and all(ytd.get(g) for g in REGIONS)):
        res.check("年初至今收入 = 利润表年初至今收入", _close(ytd_rev[2], ycur.get("revenue"), xtol), "%s vs %s" % (ytd_rev[2], ycur.get("revenue")))
        res.rows["price_volume_annual"] = [{"year": str(y), "months": str(months), "region": g, "revenue": ytd_rev[i],
                                            **{k: ytd[g].get(k) for k in ("price", "rate", "volume", "total_change")}} for i, g in enumerate(REGIONS)]
        res.drop["price_volume_annual"] = lambda r: r["year"] == str(y)
    return res


def apply(store, res):
    for name, rows in res.rows.items():
        drop = res.drop.get(name, lambda r: False)
        store.replace(name, lambda r, d=drop: not d(r), rows)
