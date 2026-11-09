"""适配器接口（所有数据来源共用的约定）。

每个适配器是 agent/adapters/ 下的一个模块，按公司清单里的 source 选用，提供这些函数：

    update(company, ctx)   -> Result   每天运行：找新报告 → 取回 → 解析成统一格式 → 核对，返回要写入的行
    backtest(company, ctx) -> Result   可选：用已核对的期间检验解析器（不改数据）
    check(company, ctx)    -> list     可选：适配器自己的数据核对，返回问题列表
    selftest(company, ctx) -> dict     可选：检查这个来源能否访问

约定
- 适配器不直接写统一格式文件：把行放进 Result.rows（文件名 -> 行列表，格式见 agent/core/schema.py），由框架核对后写入；
  核对不过（有 fatal 检查没过，或统一格式勾稽不平）就整家公司本次不写入，并开 issue。
- 适配器自己的状态放在 ctx.state_dir(公司 id) 下，用 ctx.load_state / ctx.save_state 读写。
- GPT 统一用 ctx.gpt（共享用量统计和每月预算）；判断性任务前先看 ctx.gpt_ok(kind)，预算用完时跳过非必要任务（事件检索）。
- 任何网络或 GPT 的偶发失败都只记 warning，不让整个运行失败；只有数据可能写错时才是 fatal。
"""
import datetime as dt
import json
import os


class Result:
    def __init__(self, company_id):
        self.company = company_id
        self.rows = {}          # 统一格式：文件名 -> 新行（按唯一键覆盖）；replace=True 时整份替换
        self.replace = False
        self.events = []        # 新的重大事件（events.json 同格式）
        self.checks = []        # {name, ok, detail, fatal}
        self.warnings = []
        self.notes = []         # 报告里的说明段落：[(标题, [行])]
        self.changed = []       # 适配器自己改过的文件（例如礼来的细数据）
        self.skipped = None     # 没运行的原因（例如适配器还没做好）

    def check(self, name, ok, detail="", fatal=True):
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail, "fatal": fatal})
        return ok

    def error(self, msg, detail=""):
        return self.check(msg, False, detail)

    @property
    def errors(self):
        return [c for c in self.checks if not c["ok"] and c["fatal"]]

    @property
    def ok(self):
        return not self.errors


class Context:
    """一次运行的上下文：路径、配置、日期、共享的 GPT 客户端和每月预算。"""

    def __init__(self, root, cfg, today=None, gpt=None, force_events=False, mode="auto"):
        self.root, self.cfg, self.mode = root, cfg, mode
        self.today = today or dt.date.today()
        self.gpt = gpt
        self.force_events = force_events
        self.budget = Budget(os.path.join(root, "state", "global.json"), cfg, self.today)

    def data_dir(self, cid):
        return os.path.join(self.root, "data", cid)

    def state_dir(self, cid):
        return os.path.join(self.root, "state", cid)

    def load_state(self, cid, name="state.json", default=None):
        p = os.path.join(self.state_dir(cid), name)
        if not os.path.exists(p):
            return dict(default or {})
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def save_state(self, cid, obj, name="state.json"):
        p = os.path.join(self.state_dir(cid), name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        text = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        old = None
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                old = f.read()
        if old != text:
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)

    def gpt_ok(self, kind="events"):
        """有没有 GPT 可用、预算是否允许做这类任务。财务数据相关的任务（读报告抽数）不受预算限制。"""
        if self.gpt is None or not self.gpt.available:
            return False
        return kind != "events" or self.budget.allows()


class Budget:
    """按月累计 GPT 花费（按 config 里的单价估算），超过每月上限时暂停事件检索。"""

    def __init__(self, path, cfg, today):
        self.path, self.month = path, today.strftime("%Y-%m")
        g = (cfg or {}).get("gpt", {})
        self.limit = float(g.get("monthly_budget_usd", 15))
        self.price = g.get("price_usd", {"input_per_million": 1.25, "output_per_million": 10.0, "per_web_search": 0.01})
        self.state = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.state = json.load(f)

    def spent(self):
        return float((self.state.get("gpt_spend") or {}).get(self.month, 0.0))

    def allows(self):
        return self.spent() < self.limit

    def cost(self, usage):
        p = self.price
        return (usage.get("input_tokens", 0) * p["input_per_million"] + usage.get("output_tokens", 0) * p["output_per_million"]) / 1e6 \
            + usage.get("web_searches", 0) * p["per_web_search"]

    def add(self, usage):
        c = self.cost(usage)
        spend = self.state.setdefault("gpt_spend", {})
        spend[self.month] = round(spend.get(self.month, 0.0) + c, 4)
        return c

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        text = json.dumps(self.state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        old = None
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                old = f.read()
        if old != text:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(text)
