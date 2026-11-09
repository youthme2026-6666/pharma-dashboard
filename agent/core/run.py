"""医药行业财务看板的多公司入口（GitHub Actions 每天运行 auto）。

  python -m agent.core.run auto       逐家公司：适配器更新 → 统一格式核对 → 写入；然后生成网站、写报告
  python -m agent.core.run backtest   逐家公司回测解析器（不改数据）
  python -m agent.core.run check      核对公司清单和每家公司的数据
  python -m agent.core.run site       只生成网站 _site/
  python -m agent.core.run selftest   检查 OpenAI、SEC、港交所、巨潮能否访问
可选：--company lly（只跑指定公司，可重复）  --today YYYY-MM-DD  --events（强制做一次事件检索）

退出码：0 成功（可能有提醒）；1 有必须人工处理的问题（报告写在 agent/output/report.md，工作流据此开 issue）。
某家公司出问题只影响它自己：它这次的数据不写入，其他公司照常更新。
"""
import argparse
import datetime as dt
import json
import os
import sys
import traceback
import urllib.request

from .. import adapters
from ..adapters.base import Context, Result
from ..pipeline import tasks as T
from ..pipeline.gpt import GPT
from ..pipeline.sec import SEC, SecError
from . import registry as RG
from .report import RunReport
from .site import build as build_site
from .store import UnifiedStore, write_meta

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_cfg(root):
    with open(os.path.join(root, "agent", "config.json"), encoding="utf-8") as f:
        return json.load(f)


def apply(co, res, ctx):
    """把适配器返回的统一格式行写入 data/<公司>/；统一格式核对不过就不写。"""
    if not res.ok or not res.rows:
        return
    d = ctx.data_dir(co["id"])
    st = UnifiedStore(d)
    for name, rows in res.rows.items():
        (st.replace if res.replace else st.upsert)(name, rows)
    probs = st.problems()
    if probs:
        res.error("统一格式核对没通过，本次不写入", "；".join(probs[:8]))
        return
    changed = st.save()
    latest = st.latest("income")
    meta = {"id": co["id"], "name_zh": co["name_zh"], "name_en": co["name_en"], "source": co["source"],
            "currency": (latest or {}).get("currency") or co.get("currency"),
            "latest_period": "%s %s" % (latest["fiscal_year"], latest["fiscal_period"]) if latest else None,
            "latest_period_end": (latest or {}).get("period_end"),
            "rows": {n: len(st.rows[n]) for n in st.rows}, "events": len(st.events)}
    if write_meta(d, meta):
        changed.append("meta")
    res.changed += ["统一格式 %s" % c for c in changed]


def _usage_delta(gpt, before):
    return {k: gpt.usage[k] - before.get(k, 0) for k in gpt.usage} if gpt else {}


def run_company(co, mode, ctx):
    ad = adapters.get(co["source"])
    res = Result(co["id"])
    if ad is None:
        if co["status"] == "active":
            res.error("数据来源 %s 的适配器还不存在，但公司已标为上线" % co["source"])
        else:
            res.skipped = "适配器未完成，跳过"
        return res
    before = dict(ctx.gpt.usage) if ctx.gpt else {}
    try:
        if mode == "auto":
            res = ad.update(co, ctx)
            apply(co, res, ctx)
        elif mode == "backtest":
            res = ad.backtest(co, ctx) if hasattr(ad, "backtest") else res
            if not hasattr(ad, "backtest"):
                res.skipped = "没有回测"
        elif mode == "check":
            probs = (ad.check(co, ctx) if hasattr(ad, "check") else []) + UnifiedStore(ctx.data_dir(co["id"])).problems()
            res.check("数据核对", not probs, "；".join(probs[:10]))
            if not probs:
                res.notes.append(("数据核对", ["全部通过"]))
    except Exception as e:  # 一家公司的程序异常不影响其他公司
        res = Result(co["id"])
        res.error("程序异常：%s" % e, "```\n%s\n```" % traceback.format_exc()[-2500:])
    used = _usage_delta(ctx.gpt, before)
    if used.get("calls"):
        cost = ctx.budget.add(used)
        res.notes.append(("GPT 用量", ["调用 %d 次，输入 %d tokens，输出 %d tokens，联网搜索 %d 次，约 %.2f 美元" % (
            used["calls"], used["input_tokens"], used["output_tokens"], used["web_searches"], cost)]))
    return res


def _get(url, ua, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def selftest(cfg, gpt):
    """检查四个外部来源：OpenAI、SEC、港交所披露易、巨潮资讯。"""
    out = {"openai": T.selftest(gpt) if gpt.available else "没有设置 OPENAI_API_KEY"}
    ua = os.environ.get("SEC_USER_AGENT", "")
    try:
        subs = SEC(cache_dir=os.path.join(ROOT, "agent", ".cache", "sec")).submissions(cfg["company"]["cik"])
        out["sec"] = {"filings": len(subs), "latest": subs[0]["filing_date"] if subs else None}
    except SecError as e:
        out["sec"] = str(e)
    try:
        st, body = _get("https://www1.hkexnews.hk/search/prefix.do?&callback=callback&lang=EN&type=A&name=01801&market=SEHK", ua or "PharmaDashboard")
        out["hkex"] = {"status": st, "innovent_found": b'"code":"01801"' in body}
    except Exception as e:  # noqa: BLE001 只报告
        out["hkex"] = str(e)
    try:
        st, body = _get("http://www.cninfo.com.cn/new/data/szse_stock.json", ua or "PharmaDashboard")
        out["cninfo"] = {"status": st, "stocks": len(json.loads(body.decode("utf-8")).get("stockList", []))}
    except Exception as e:  # noqa: BLE001
        out["cninfo"] = str(e)
    return out


def main(argv=None, root=None):
    root = root or ROOT
    ap = argparse.ArgumentParser(description="医药行业财务看板 · 多公司维护")
    ap.add_argument("mode", nargs="?", default="auto", choices=["auto", "backtest", "check", "site", "selftest"])
    ap.add_argument("--company", action="append", default=[], help="只处理这些公司（公司清单里的 id），可重复")
    ap.add_argument("--today", help="假定今天的日期（测试用），YYYY-MM-DD")
    ap.add_argument("--events", action="store_true", help="强制做一次重大事件检索")
    args = ap.parse_args(argv)
    cfg = load_cfg(root)
    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    report = RunReport(args.mode)
    gpt = GPT(cfg)
    if args.mode == "selftest":
        print(json.dumps(selftest(cfg, gpt), ensure_ascii=False, indent=2))
        return 0
    reg = RG.load(root)
    probs = RG.problems(reg)
    if probs:
        report.error("公司清单格式有问题：" + "；".join(probs[:10]))
    ctx = Context(root, cfg, today=today, gpt=gpt, force_events=args.events, mode=args.mode)
    if args.mode != "site" and not probs:
        targets = RG.companies(reg, status=("active", "onboarding"))
        if args.company:
            unknown = set(args.company) - {c["id"] for c in reg["companies"]}
            if unknown:
                report.error("公司清单里没有：%s" % "、".join(sorted(unknown)))
            targets = [c for c in reg["companies"] if c["id"] in args.company]
        for co in targets:
            report.add(co, run_company(co, args.mode, ctx))
        planned = RG.companies(reg, status="planned")
        if planned and args.mode == "auto" and not args.company:
            report.section("待接入", ["%d 家公司在清单里等待接入（适配器完成后自动开始）" % len(planned)])
        if args.mode == "auto":
            ctx.budget.save()
            report.section("GPT 预算", ["本月已用约 %.2f 美元，上限 %.0f 美元" % (ctx.budget.spent(), ctx.budget.limit)])
    if args.mode in ("auto", "site") and not probs:
        info = build_site(root, os.path.join(root, "_site"), reg, today.isoformat())
        report.section("网页", ["已生成 %d 个页面（共 %.0f KB）" % (len(info["pages"]), info["bytes"] / 1024)])
    report.write(os.path.join(root, "agent", "output"))
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
