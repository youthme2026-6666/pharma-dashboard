"""多公司运行报告：总览表 + 每家公司的问题、提醒和说明。写到 agent/output/report.md，并交给 GitHub Actions（运行摘要、开 issue）。"""
import datetime as dt
import os


class RunReport:
    def __init__(self, mode):
        self.mode = mode
        self.items = []          # (company, Result)
        self.global_errors, self.global_warnings, self.sections = [], [], []

    def add(self, company, res):
        self.items.append((company, res))

    def error(self, msg):
        self.global_errors.append(msg)

    def warning(self, msg):
        self.global_warnings.append(msg)

    def section(self, title, lines):
        self.sections.append((title, [l for l in lines if l]))

    @property
    def failed(self):
        return bool(self.global_errors) or any(not r.ok for _, r in self.items)

    @property
    def changed(self):
        return [(c, r.changed) for c, r in self.items if r.changed]

    def markdown(self):
        warn = self.global_warnings or any(r.warnings for _, r in self.items)
        status = "失败，需要处理" if self.failed else ("成功（有提醒）" if warn else "成功")
        ch = self.changed
        out = ["# 医药行业财务看板 · 自动维护报告", "",
               "- 时间：%s UTC" % dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M"),
               "- 模式：%s" % self.mode, "- 结果：**%s**" % status,
               "- 数据变化：%s" % ("；".join("%s（%s）" % (c["name_zh"], "、".join(f)) for c, f in ch) if ch else "无"), ""]
        if self.items:
            out += ["## 各公司", "", "| 公司 | 来源 | 结果 | 数据变化 |", "|---|---|---|---|"]
            for c, r in self.items:
                res = r.skipped or ("需要处理" if not r.ok else ("有提醒" if r.warnings else "通过"))
                out.append("| %s %s | %s | %s | %s |" % (c["name_zh"], c["id"].upper(), c["source"], res, "、".join(r.changed) or "—"))
            out.append("")
        errs = list(self.global_errors) + ["%s：%s%s" % (c["name_zh"], e["name"], "（%s）" % e["detail"] if e["detail"] else "")
                                           for c, r in self.items for e in r.errors]
        if errs:
            out += ["## 需要处理的问题", ""] + ["- " + m for m in errs] + [""]
        warns = list(self.global_warnings) + ["%s：%s" % (c["name_zh"], w) for c, r in self.items for w in r.warnings]
        if warns:
            out += ["## 提醒", ""] + ["- " + m for m in warns] + [""]
        for c, r in self.items:
            if r.notes:
                out += ["## %s %s" % (c["name_zh"], c["name_en"]), ""]
                for title, lines in r.notes:
                    out += ["### " + title, ""] + ["- " + l for l in lines if l] + [""]
        for title, lines in self.sections:
            out += ["## " + title, ""] + ["- " + l for l in lines] + [""]
        return "\n".join(out)

    def write(self, out_dir):
        os.makedirs(out_dir, exist_ok=True)
        md = self.markdown()
        with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as f:
            f.write(md)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
                f.write(md + "\n")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
                f.write("changed=%s\nfailed=%s\n" % ("true" if self.changed else "false", "true" if self.failed else "false"))
        if not os.environ.get("DASH_QUIET"):
            print(md)
        return md
