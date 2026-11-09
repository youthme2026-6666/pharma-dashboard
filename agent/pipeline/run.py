"""看板维护的命令行入口（GitHub Actions 每天运行 auto）。

  python -m agent.pipeline.run auto       检查 SEC 新申报 → 回测解析器 → 补新季度 → GPT 更新事件 → 全量核对 → 生成网页 → 写报告
  python -m agent.pipeline.run backtest   用已核对的历史季度检验解析器（不改数据）
  python -m agent.pipeline.run events     只更新重大事件（GPT 联网搜索）
  python -m agent.pipeline.run check      只核对 data/ 里的数据
  python -m agent.pipeline.run site       只生成网页 _site/
  python -m agent.pipeline.run selftest   检查 OpenAI 密钥、模型、联网搜索，以及 SEC 能否访问

退出码：0 成功（可能有警告）；1 有必须人工处理的问题（报告写在 agent/output/report.md，工作流会据此开 issue）。
"""
import argparse
import copy
import datetime as dt
import glob
import hashlib
import json
import os
import sys
import traceback

from . import build as B
from . import filings as FL
from . import tasks as T
from . import verify as V
from .datasets import Store, last_year, next_quarter, quarter_end
from .gpt import GPT
from .sec import SEC, SecError
from .site import build_site

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT = os.path.join(ROOT, "agent")
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(AGENT, "output")
SITE = os.path.join(ROOT, "_site")
CACHE = os.path.join(AGENT, ".cache", "sec")
BACKTEST_QUARTERS = ["2025Q4", "2026Q1", "2026Q2", "2026Q3"]
STATE_DIR = AGENT   # state.json / learned.json 所在目录（测试时会换成临时目录）


def make_sec(cfg):
    return SEC(cache_dir=CACHE, min_interval=cfg["sec"]["min_interval_seconds"], retries=cfg["sec"]["retries"])


# ---------------- 小工具 ----------------
def load_json(path, default):
    if not os.path.exists(path):
        return copy.deepcopy(default)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    old = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            old = f.read()
    if old != text:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return True
    return False


def code_hash():
    h = hashlib.sha1()
    for p in sorted(glob.glob(os.path.join(AGENT, "pipeline", "*.py"))):
        with open(p, "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:12]


class Report:
    def __init__(self, mode):
        self.mode, self.sections, self.fatal, self.warn = mode, [], [], []
        self.changed = []

    def add(self, title, lines):
        self.sections.append((title, [l for l in lines if l]))

    def error(self, msg):
        self.fatal.append(msg)

    def warning(self, msg):
        self.warn.append(msg)

    def markdown(self):
        status = "失败，需要处理" if self.fatal else ("成功（有提醒）" if self.warn else "成功")
        out = ["# 礼来看板自动维护报告", "", "- 时间：%s UTC" % dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M"),
               "- 模式：%s" % self.mode, "- 结果：**%s**" % status,
               "- 数据变化：%s" % ("、".join(self.changed) if self.changed else "无"), ""]
        if self.fatal:
            out += ["## 需要处理的问题", ""] + ["- " + m for m in self.fatal] + [""]
        if self.warn:
            out += ["## 提醒", ""] + ["- " + m for m in self.warn] + [""]
        for title, lines in self.sections:
            out += ["## " + title, ""] + ["- " + l for l in lines] + [""]
        return "\n".join(out)

    def write(self):
        os.makedirs(OUT, exist_ok=True)
        md = self.markdown()
        with open(os.path.join(OUT, "report.md"), "w", encoding="utf-8") as f:
            f.write(md)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
                f.write(md + "\n")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
                f.write("changed=%s\nfailed=%s\n" % ("true" if self.changed else "false", "true" if self.fatal else "false"))
        if not os.environ.get("DASH_QUIET"):
            print(md)


# ---------------- 数据核对（全量） ----------------
def check_data(store, cfg):
    """对 data/ 做结构与勾稽核对，返回问题列表（空表示通过）。"""
    p = []
    f = lambda v: float(v) if v not in (None, "") else None
    qs = store.quarters()
    first = cfg.get("first_quarter", "2021Q1")
    want, q = [], first
    while q <= qs[-1]:
        want.append(q)
        q = next_quarter(q)
    for name in ("income_statement", "balance_sheet"):
        got = sorted(r["quarter"] for r in store.rows[name])
        if got != want:
            p.append("%s 的季度不连续或有重复：缺 %s" % (name, sorted(set(want) - set(got))))
    for name in ("product_revenue", "price_volume"):
        got = sorted({r["quarter"] for r in store.rows[name]})
        if got != want:
            p.append("%s 的季度不完整：缺 %s" % (name, sorted(set(want) - set(got))))
    rev = {}
    for r in store.rows["income_statement"]:
        g = lambda k: f(r[k])
        rev[r["quarter"]] = g("revenue")
        if abs(g("revenue") - g("cost_of_sales") - g("gross_margin")) > 2:
            p.append("%s 毛利不等于收入减成本" % r["quarter"])
        if abs(g("gross_margin") - g("rnd") - g("msa") - g("acquired_iprd") - g("impairment_restructuring") - g("operating_income")) > 2:
            p.append("%s 营业利润勾稽不平" % r["quarter"])
        if abs(g("operating_income") + g("other_net") - g("pretax_income")) > 2:
            p.append("%s 税前利润勾稽不平" % r["quarter"])
        if abs(g("pretax_income") - g("income_tax") - g("net_income")) > 2:
            p.append("%s 净利润勾稽不平" % r["quarter"])
    for r in store.rows["balance_sheet"]:
        ta, tl, te, tle = f(r["total_assets"]), f(r["total_liabilities"]), f(r["total_equity"]), f(r["total_liabilities_equity"])
        if abs(ta - tl - te) > 2 or abs(ta - tle) > 2:
            p.append("%s 资产负债表不平" % r["quarter"])
    sums = {}
    for r in store.rows["product_revenue"]:
        sums[r["quarter"]] = sums.get(r["quarter"], 0) + f(r["total"])
        u, i, t = f(r["us"]), f(r["intl"]), f(r["total"])
        if u is not None and i is not None and abs(u + i - t) > 2.5:  # 礼来工作簿里“其他”类是倒挤出来的，四舍五入可差 2
            p.append("%s %s 美国 + 国际 ≠ 合计" % (r["quarter"], r["product"]))
        if (u is None) != (i is None):
            p.append("%s %s 美国/国际只填了一个" % (r["quarter"], r["product"]))
    for q2, s in sums.items():
        if q2 in rev and abs(s - rev[q2]) > max(6, rev[q2] * 0.001):
            p.append("%s 分产品合计 %.1f 与总收入 %.1f 不符" % (q2, s, rev[q2]))
    for q2 in qs:
        t = store.row("price_volume", quarter=q2, region="Total Revenue")
        if not t or abs(f(t["revenue"]) - rev[q2]) > 6:
            p.append("%s 分地区收入合计与总收入不符" % q2)
    full = {}
    for r in store.rows["income_statement"]:
        full.setdefault(r["quarter"][:4], []).append(r)
    for r in store.rows["income_annual"]:
        if r["months"] == "12" and len(full.get(r["year"], [])) == 4:
            s = sum(f(x["revenue"]) for x in full[r["year"]])
            if abs(s - f(r["revenue"])) > 3:
                p.append("%s 年四个季度收入之和与全年不符" % r["year"])
    ids, domains = set(), cfg["events"]["allowed_domains"]
    for e in store.rows["events"]:
        if e["id"] in ids:
            p.append("事件 id 重复：%s" % e["id"])
        ids.add(e["id"])
        if not V.is_date(e.get("date")) or e.get("type") not in cfg["events"]["types"] or not V.host_allowed(V.norm_url(e.get("url")), domains):
            p.append("事件格式不对：%s" % e.get("id"))
    return p


# ---------------- 回测 ----------------
def compare_with_seed(store, res):
    """把重新解析出的季度与已核对数据比较，返回差异列表。"""
    q, diffs = res.quarter, []
    f = lambda v: float(v) if v not in (None, "") else None
    old = store.row("income_statement", quarter=q)
    new = (res.rows.get("income_statement") or [None])[0]
    if old and new:
        for k in B.IS_FIELDS + B.NG_FIELDS:
            tol = 0.011 if "eps" in k else (1001 if k == "diluted_shares_k" else 1.01)
            if new.get(k) is None or f(old[k]) is None or abs(new[k] - f(old[k])) > tol:
                diffs.append("%s 利润表 %s：解析 %s，已有 %s" % (q, k, new.get(k), old[k]))
    old = store.row("balance_sheet", quarter=q)
    new = (res.rows.get("balance_sheet") or [None])[0]
    if old and new:
        for k, v in old.items():
            if k not in ("quarter", "period_end") and abs((new.get(k) or 0) - f(v)) > 1.01:
                diffs.append("%s 资产负债表 %s：解析 %s，已有 %s" % (q, k, new.get(k), v))
    for row in res.rows.get("price_volume") or []:
        old = store.row("price_volume", quarter=q, region=row["region"])
        if not old:
            continue
        for k in ("revenue", "price", "rate", "volume", "total_change"):
            if row.get(k) is None or f(old[k]) is None:
                continue
            tol = 1.01 if k == "revenue" else 0.0101
            if abs(row[k] - f(old[k])) > tol:
                diffs.append("%s 分地区 %s %s：解析 %s，已有 %s" % (q, row["region"], k, row[k], old[k]))
    for row in res.rows.get("income_annual") or []:
        old = store.row("income_annual", year=row["year"], months=row["months"])
        if old:
            for k in ("revenue", "net_income", "operating_income"):
                if row.get(k) is not None and abs(row[k] - f(old[k])) > 1.01:
                    diffs.append("%s 年度（%s 个月）%s：解析 %s，已有 %s" % (row["year"], row["months"], k, row[k], old[k]))
    new_rows = res.rows.get("product_revenue") or []
    split = {r["product"] for r in new_rows if not r["product"].endswith("(combined)")}
    names = {}
    for r in new_rows:
        if r["product"].endswith("(combined)"):
            names[r["area"]] = r["product"]
    agg, count = {}, {}
    for r in store.rows["product_revenue"]:
        if r["quarter"] == q:
            key = r["product"] if r["product"] in split else names.get(r["area"], r["area"])
            agg[key] = agg.get(key, 0) + f(r["total"])
            count[key] = count.get(key, 0) + 1
    for r in new_rows:
        # 两边都是若干个各自四舍五入到百万的公布数加减出来的，每个数最多差 0.5：
        # SEC 一侧见 res.terms（第四季度 = 全年 − 前三季度，再减去拆出的产品）；已核对一侧是归并进来的产品个数。
        # 例：2025Q4 Other Oncology (combined) = SEC 2 个数 vs 工作簿 9 个产品之和，上限 5.5，实际差 2.4。
        ours = res.terms.get(r["product"], 2 if q.endswith("Q4") else 1)
        tol = max(2.01, 0.5 * (ours + count.get(r["product"], 1)) + 0.01)
        if abs(r["total"] - agg.get(r["product"], 0)) > tol:
            diffs.append("%s 产品 %s：解析 %s，已有（按同口径归并）%.1f" % (q, r["product"], r["total"], agg.get(r["product"], 0)))
    return diffs


# ---------------- 一个季度 ----------------
def gather(sec, cik, subs, q, cfg):
    """取回并解析某季度所需的申报文件。返回 (状态, 内容, 说明)；状态为 ready / waiting / broken。"""
    per_f = FL.periodic_for(subs, q)
    k8s = FL.earnings_8ks(subs, q, cfg["sec"].get("earnings_8k_window_days", 75))
    if not per_f or not k8s:
        lacking = [x for x, ok in (("10-K" if q.endswith("Q4") else "10-Q", per_f), ("业绩 8-K", k8s)) if not ok]
        return "waiting", None, "%s 尚未发布：%s" % (q, "、".join(lacking))
    k8 = None
    for f in k8s:
        k8 = FL.load_8k(sec, cik, f, q)
        if k8:
            break
    if not k8:
        return "broken", None, "%s：找到了 8-K，但附件里没有认出本季度的利润表（新闻稿格式可能变了）" % q
    per = FL.load_periodic(sec, cik, per_f, q)
    if per["missing"]:
        return "broken", None, "%s：%s 里没有认出 %s（格式可能变了）" % (q, per_f["form"], "、".join(per["missing"]))
    ytd_prior = None
    if q.endswith("Q4"):
        q3 = "%sQ3" % q[:4]
        f3 = FL.periodic_for(subs, q3)
        if not f3:
            return "broken", None, "%s：找不到同年第三季度 10-Q（计算第四季度需要前三季度数据）" % q
        ytd_prior = FL.load_periodic(sec, cik, f3, q3)["products_ytd"]
    return "ready", {"k8": k8, "per": per, "ytd_prior": ytd_prior}, ""


def q4_growth(store, per, ytd_prior):
    fy = per["products"]["revenue"].get("current")
    nine = (ytd_prior or {}).get("revenue", {}).get("current")
    out = {}
    if fy and nine:
        for i, g in enumerate(B.REGIONS):
            out[g] = fy[i] - nine[i]
    return out


def build_one(store, q, got, cfg, gpt, report, learned, sec=None, cik=None, backtest=False):
    k8, per, ytd_prior = got["k8"], got["per"], got["ytd_prior"]
    classify = None
    if gpt.available and not backtest:
        def classify(name):
            try:
                area = T.classify_area(gpt, name, cfg["areas"], T.product_context(k8["text"], name))
            except Exception as e:  # 判定失败时该产品留在“(combined)”里
                report.warning("GPT 判定 %s 的治疗领域失败：%s" % (name, e))
                area = None
            if area:
                learned.setdefault("product_areas", {})[name] = area
            return area
    q4 = None
    if q.endswith("Q4") and gpt.available:
        rev = q4_growth(store, per, ytd_prior)
        growth = {}
        for g, v in rev.items():
            base = store.region_revenue(last_year(q), g)
            growth[g] = (v / base - 1) if base else None
        try:
            q4, problems = T.q4_prv(gpt, k8["text"], growth)
            for m in problems:
                report.warning("第四季度价格/销量拆分（GPT）未采用：" + m)
        except Exception as e:  # GPT 相关的任何失败都只提醒，不影响财务数据
            report.warning("第四季度价格/销量拆分：GPT 调用失败（%s），季度只给合计增速" % e)
    ccfg = dict(cfg, learned=learned)
    res = B.build_quarter(store, q, k8, per, ytd_prior, cfg=ccfg, classify=classify, q4_prv=q4)
    if sec is not None and not backtest:
        xbrl_checks(sec, cik, q, res, report)
    return res


def xbrl_checks(sec, cik, q, res, report):
    """用 SEC 的 XBRL 数据库再核对一次总数（总资产、季度收入、净利润）。取不到时只提醒。"""
    pe = quarter_end(q)
    inc = (res.rows.get("income_statement") or [{}])[0]
    bs = (res.rows.get("balance_sheet") or [{}])[0]
    start = {"1": "%s-01-01", "2": "%s-04-01", "3": "%s-07-01", "4": "%s-10-01"}[q[5]] % q[:4]
    targets = [(["Assets"], None, bs.get("total_assets"))]
    if not q.endswith("Q4"):  # 10-K 的 XBRL 一般不单独标第四季度，只核对期末总资产
        targets += [(["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"], start, inc.get("revenue")),
                    (["NetIncomeLoss"], start, inc.get("net_income"))]
    for tags, st, ours in targets:
        if ours is None:
            continue
        vals, errors = [], []
        for tag in tags:
            try:
                facts = sec.concept(cik, "us-gaap", tag).get("units", {}).get("USD", [])
            except SecError as e:
                errors.append(str(e))
                continue
            vals = [x["val"] / 1e6 for x in facts if x.get("end") == pe and (st is None or x.get("start") == st)]
            if vals:
                break
        if not vals:
            report.warning("XBRL 里取不到 %s 在 %s 的数值，跳过该项核对%s" % ("/".join(tags), q, "（%s）" % errors[0] if errors else ""))
            continue
        res.check("XBRL 核对 %s" % tags[0], any(abs(v - ours) <= 1.5 for v in vals), "XBRL %s vs 解析 %s" % (vals[:3], ours))


# ---------------- 各模式 ----------------
def run_backtest(sec, cik, subs, store, cfg, report, quarters=None):
    ok, lines = True, []
    for q in quarters or BACKTEST_QUARTERS:
        if not store.row("income_statement", quarter=q):
            continue
        status, got, why = gather(sec, cik, subs, q, cfg)
        if status != "ready":
            ok = False
            lines.append("%s：%s" % (q, why))
            continue
        res = build_one(store, q, got, cfg, GPT(cfg, api_key=""), report, {}, backtest=True)
        diffs = compare_with_seed(store, res)
        failed = [c["name"] + ("（%s）" % c["detail"] if c["detail"] else "") for c in res.failed() if c["fatal"]]
        if diffs or failed:
            ok = False
            lines += ["%s：%s" % (q, d) for d in (failed + diffs)[:20]]
        else:
            lines.append("%s：与已核对数据一致（%d 项核对全部通过）" % (q, len(res.checks)))
    report.add("解析器回测（用已核对的季度检验）", lines or ["没有可回测的季度"])
    return ok


def run_events(store, cfg, gpt, report, state, today):
    if not gpt.available:
        report.warning("没有设置 OPENAI_API_KEY，跳过重大事件更新")
        return 0
    try:
        new, rejected, meta = T.find_events(gpt, store, cfg, today=today)
    except Exception as e:  # GPT 相关的任何失败都只提醒，不影响财务数据
        report.warning("重大事件更新失败（GPT）：%s" % e)
        return 0
    if new:
        store.rows["events"].extend(new)
    state["events_last_run"] = today.isoformat()
    lines = ["检索范围 %s ~ %s，模型 %s，GPT 检索到的来源 %d 个" % (meta["start"], meta["end"], meta["model"], meta["sources"])]
    lines += ["新增：%s %s（%s）" % (e["date"], e["title"], e["url"]) for e in new]
    lines += ["未采用：%s —— %s" % ((r["item"] or {}).get("title", "?") if isinstance(r["item"], dict) else "?", r["reason"]) for r in rejected]
    report.add("重大事件（GPT 联网检索 + 规则核对）", lines)
    return len(new)


def auto(args, cfg, report):
    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    state = load_json(os.path.join(STATE_DIR, "state.json"), {})
    learned = load_json(os.path.join(STATE_DIR, "learned.json"), {"product_areas": {}})
    store = Store(DATA)
    gpt = GPT(cfg)
    cik = cfg["company"]["cik"]
    appended = []
    try:
        sec = make_sec(cfg)
        subs = sec.submissions(cik, include_older=True)
        state["sec_fail_count"] = 0
    except SecError as e:
        # 偶发的网络问题只提醒；连续 3 天访问不了才算需要处理
        n = state.get("sec_fail_count", 0) + 1
        state["sec_fail_count"] = n
        (report.error if n >= 3 else report.warning)("无法访问 SEC（连续第 %d 次）：%s" % (n, e))
        sec = subs = None
    if subs is not None:
        latest = store.latest_quarter()
        due, q = [], next_quarter(latest)
        while dt.date.fromisoformat(quarter_end(q)) < today and len(due) < 8:
            due.append(q)
            q = next_quarter(q)
        ready = []
        for q in due:
            status, got, why = gather(sec, cik, subs, q, cfg)
            if status == "waiting":
                report.add("新季度", [why])
                break
            if status == "broken":
                report.error(why)
                break
            ready.append((q, got))
        bt = state.get("backtest") or {}
        stale = bt.get("hash") != code_hash() or not bt.get("ok") or bt.get("date", "") < (today - dt.timedelta(days=30)).isoformat()
        if ready or stale:
            ok = run_backtest(sec, cik, subs, store, cfg, report)
            state["backtest"] = {"date": today.isoformat(), "hash": code_hash(), "ok": ok}
            if not ok:
                report.error("解析器回测没通过：用 SEC 文件重新解析已核对的季度，结果对不上（详见“解析器回测”）。为避免写入错误数据，本次不追加新季度。")
                ready = []
        for q, got in ready:
            res = build_one(store, q, got, cfg, gpt, report, learned, sec=sec, cik=cik)
            lines = ["%s：%s（%s）" % ("通过" if c["ok"] else ("未通过" if c["fatal"] else "提醒"), c["name"], c["detail"]) if c["detail"] and not c["ok"] else
                     "%s：%s" % ("通过" if c["ok"] else ("未通过" if c["fatal"] else "提醒"), c["name"]) for c in res.checks]
            report.add("%s 核对（共 %d 项）" % (q, len(res.checks)), [l for l in lines if not l.startswith("通过")] or ["全部通过"])
            for w in res.warnings:
                report.warning("%s：%s" % (q, w))
            if res.notes:
                report.add("%s 说明" % q, res.notes)
            if not res.ok:
                report.error("%s 有核对未通过，整季不写入（见“%s 核对”）" % (q, q))
                break
            B.apply(store, res)
            appended.append(q)
            state.setdefault("quarters", {})[q] = {"8-K": got["k8"]["accession"], got["per"]["form"]: got["per"]["accession"], "added": today.isoformat()}
            if gpt.available:
                try:
                    known = {r["product"] for r in store.rows["product_revenue"]}
                    ev, why = T.earnings_note(gpt, got["k8"]["text"], q, got["k8"]["filing_date"], got["k8"]["url"], known)
                    if ev and not any(e["id"] == ev["id"] for e in store.rows["events"]):
                        store.rows["events"].append(ev)
                        report.add("%s 业绩说明" % q, ["新增：" + ev["title"]])
                    else:
                        report.add("%s 业绩说明" % q, [why])
                except Exception as e:  # GPT 相关的任何失败都只提醒，不影响财务数据
                    report.warning("%s 业绩说明：GPT 调用失败（%s）" % (q, e))
        if appended:
            report.add("新季度", ["已写入：" + "、".join(appended)])
    # 重大事件：每 refresh_every_days 天一次，或者有新季度时
    last_ev = state.get("events_last_run", "2000-01-01")
    if args.events or appended or last_ev <= (today - dt.timedelta(days=cfg["events"]["refresh_every_days"])).isoformat():
        run_events(store, cfg, gpt, report, state, today)
    # 全量核对，不通过就不保存
    problems = check_data(store, cfg)
    if problems:
        report.error("数据核对没通过，本次不保存任何修改：" + "；".join(problems[:10]))
        return finish(report, state, learned, store, today, save=False)
    return finish(report, state, learned, store, today, save=True, gpt=gpt, appended=appended)


def finish(report, state, learned, store, today, save=True, gpt=None, appended=None):
    if save:
        changed = store.save()
        report.changed = changed
        if changed:
            state["data_updated"] = today.isoformat()
        hb = state.get("heartbeat", "2000-01-01")
        if hb <= (today - dt.timedelta(days=load_json(os.path.join(AGENT, "config.json"), {}).get("heartbeat_days", 25))).isoformat():
            state["heartbeat"] = today.isoformat()   # 定期提交一次，避免 GitHub 因仓库长期无活动停用定时任务
        save_json(os.path.join(STATE_DIR, "state.json"), state)
        save_json(os.path.join(STATE_DIR, "learned.json"), learned)
    if gpt is not None and gpt.usage["calls"]:
        report.add("GPT 用量", ["模型 %s；调用 %d 次，输入 %d tokens，输出 %d tokens，联网搜索 %d 次" % (
            gpt.model, gpt.usage["calls"], gpt.usage["input_tokens"], gpt.usage["output_tokens"], gpt.usage["web_searches"])] + gpt.log[-8:])
    info = build_site(ROOT, SITE, updated=state.get("data_updated") or today.isoformat(), data_dir=store.dir)
    report.add("网页", ["已生成 _site/index.html（%.0f KB）" % (info["bytes"] / 1024)])
    report.write()
    return 1 if report.fatal else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="礼来看板自动维护")
    ap.add_argument("mode", nargs="?", default="auto", choices=["auto", "backtest", "events", "check", "site", "selftest"])
    ap.add_argument("quarters", nargs="*", help="backtest 时指定季度，例如 2026Q3")
    ap.add_argument("--today", help="假定今天的日期（测试用），YYYY-MM-DD")
    ap.add_argument("--events", action="store_true", help="auto 模式下强制更新事件")
    args = ap.parse_args(argv)
    cfg = load_json(os.path.join(AGENT, "config.json"), {})
    report = Report(args.mode)
    try:
        if args.mode == "auto":
            return auto(args, cfg, report)
        if args.mode == "check":
            problems = check_data(Store(DATA), cfg)
            report.add("数据核对", problems or ["全部通过"])
            if problems:
                report.error("数据核对没通过")
            report.write()
            return 1 if problems else 0
        if args.mode == "site":
            info = build_site(ROOT, SITE)
            print("已生成 %s（%.0f KB）" % (os.path.join(SITE, "index.html"), info["bytes"] / 1024))
            return 0
        if args.mode == "backtest":
            sec = make_sec(cfg)
            store = Store(DATA)
            ok = run_backtest(sec, cfg["company"]["cik"], sec.submissions(cfg["company"]["cik"], include_older=True), store, cfg, report, args.quarters or None)
            if not ok:
                report.error("解析器回测没通过")
            report.write()
            return 0 if ok else 1
        if args.mode == "events":
            today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
            state = load_json(os.path.join(STATE_DIR, "state.json"), {})
            store = Store(DATA)
            gpt = GPT(cfg)
            run_events(store, cfg, gpt, report, state, today)
            problems = check_data(store, cfg)
            if problems:
                report.error("事件写入后数据核对没通过：" + "；".join(problems[:5]))
                report.write()
                return 1
            return finish(report, state, load_json(os.path.join(STATE_DIR, "learned.json"), {"product_areas": {}}), store, today, gpt=gpt)
        if args.mode == "selftest":
            out = {}
            gpt = GPT(cfg)
            out["openai"] = T.selftest(gpt) if gpt.available else "没有设置 OPENAI_API_KEY"
            try:
                sec = SEC(cache_dir=CACHE)
                subs = sec.submissions(cfg["company"]["cik"])
                out["sec"] = {"filings": len(subs), "latest": subs[0]["filing_date"] if subs else None}
            except SecError as e:
                out["sec"] = str(e)
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
    except Exception as e:  # 任何意外都写进报告，便于工作流开 issue
        report.error("程序异常：%s\n\n```\n%s\n```" % (e, traceback.format_exc()[-3000:]))
        report.write()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
