"""每家公司统一格式数据的读写：data/<公司>/income.csv、balance.csv、cashflow.csv、segments.csv、events.json、meta.json。

写回时列顺序固定、按期间排序、数字格式统一（整数不带小数点），内容没变就不改文件，GitHub 上看到的改动就是真实的数据变化。
"""
import csv
import io
import json
import os


def _read_text(p):
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return f.read()

from ..pipeline.datasets import fmt
from . import schema as S


def _sort_key(name, r):
    pe = str(r.get("period_end") or "")
    if name == "balance":
        return (pe,)
    months = float(r.get("months") or 0)
    if name == "segments":
        return (pe, months, str(r.get("axis")), str(r.get("group") or ""), str(r.get("member")))
    return (pe, months)


class UnifiedStore:
    def __init__(self, data_dir):
        self.dir = data_dir
        self.rows = {n: self._read(n) for n in S.FILES}
        self.events = self._read_events()

    # ---------- 读取 ----------
    def _read(self, name):
        p = os.path.join(self.dir, name + ".csv")
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8", newline="") as f:
            return [dict(r) for r in csv.DictReader(f)]

    def _read_events(self):
        p = os.path.join(self.dir, "events.json")
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    # ---------- 修改 ----------
    def upsert(self, name, new_rows):
        """按唯一键覆盖或追加；返回实际变化的行数。"""
        keys = S.KEYS[name]
        index = {tuple(str(r.get(k)) for k in keys): i for i, r in enumerate(self.rows[name])}
        changed = 0
        for r in new_rows:
            r = {k: fmt(r.get(k)) if k in S.NUMERIC else ("" if r.get(k) is None else str(r.get(k))) for k in S.FILES[name]}
            key = tuple(str(r.get(k)) for k in keys)
            if key in index:
                if self.rows[name][index[key]] != r:
                    self.rows[name][index[key]] = r
                    changed += 1
            else:
                index[key] = len(self.rows[name])
                self.rows[name].append(r)
                changed += 1
        return changed

    def replace(self, name, rows):
        """整份替换（适配器能一次给出全部历史时用，例如礼来由细数据换算）。"""
        self.rows[name] = []
        return self.upsert(name, rows)

    # ---------- 核对与写回 ----------
    def problems(self):
        out = []
        for n in S.FILES:
            out += S.problems(n, self.rows[n])
        return out

    def dump(self, name):
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=S.FILES[name], lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        for r in sorted(self.rows[name], key=lambda r: _sort_key(name, r)):
            w.writerow(r)
        return buf.getvalue()

    def save(self):
        """写回四个统一格式文件；返回内容有变化的文件名。"""
        os.makedirs(self.dir, exist_ok=True)
        changed = []
        for n in S.FILES:
            p = os.path.join(self.dir, n + ".csv")
            text = self.dump(n)
            old = _read_text(p)
            if old != text:
                with open(p, "w", encoding="utf-8", newline="") as f:
                    f.write(text)
                changed.append(n)
        return changed

    def latest(self, name="income", months=None):
        rows = [r for r in self.rows[name] if months is None or float(r.get("months") or 0) == months]
        return max(rows, key=lambda r: _sort_key(name, r)) if rows else None


def write_meta(data_dir, meta):
    p = os.path.join(data_dir, "meta.json")
    text = json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    old = _read_text(p)
    if old != text:
        os.makedirs(data_dir, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return True
    return False
