"""读写 data/ 下的 7 个数据文件，保持现有的列顺序、排序和数字格式（看板直接读这些文件）。"""
import csv
import io
import json
import os

NAMES = ["income_statement", "income_annual", "product_revenue", "price_volume", "price_volume_annual", "balance_sheet", "events"]
REGION_ORDER = ["U.S.", "Outside U.S.", "Europe", "Japan", "China", "ROW", "Total Revenue"]
QUARTER_END = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}


def fmt(v):
    """None -> ''；整数值不带小数点；其余最多保留 6 位有效小数，不用科学计数法。"""
    if v is None or v == "":
        return ""
    if isinstance(v, str):
        return v
    x = float(v)
    if x == 0:
        return "0"
    if abs(x - round(x)) < 1e-9:
        return str(int(round(x)))
    s = ("%.6f" % x).rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def num(v):
    if v is None or v == "":
        return None
    return float(v)


def quarter_end(q):
    return "%s-%s" % (q[:4], QUARTER_END[int(q[5])])


def next_quarter(q):
    y, n = int(q[:4]), int(q[5])
    return "%dQ%d" % (y + (n == 4), 1 if n == 4 else n + 1)


def prev_quarter(q):
    y, n = int(q[:4]), int(q[5])
    return "%dQ%d" % (y - (n == 1), 4 if n == 1 else n - 1)


def last_year(q):
    return "%d%s" % (int(q[:4]) - 1, q[4:])


class Store:
    """data/ 目录的内存副本。rows 里的值是字符串（与看板读到的一致），写回时统一格式化。"""

    def __init__(self, data_dir):
        self.dir = data_dir
        self.columns, self.rows = {}, {}
        for n in NAMES:
            if n == "events":
                with open(os.path.join(data_dir, "events.json"), encoding="utf-8") as f:
                    self.rows[n] = json.load(f)
                continue
            with open(os.path.join(data_dir, n + ".csv"), encoding="utf-8", newline="") as f:
                r = csv.DictReader(f)
                self.columns[n] = list(r.fieldnames)
                self.rows[n] = [dict(x) for x in r]

    # ---------- 查询 ----------
    def quarters(self):
        return sorted(r["quarter"] for r in self.rows["income_statement"])

    def latest_quarter(self):
        q = self.quarters()
        return q[-1] if q else None

    def row(self, name, **where):
        for r in self.rows[name]:
            if all(str(r.get(k)) == str(v) for k, v in where.items()):
                return r
        return None

    def region_revenue(self, quarter, region):
        """某季度某地区收入；“美国以外”在旧数据里没有单独一行时 = 合计 − 美国。"""
        r = self.row("price_volume", quarter=quarter, region=region)
        if r and r.get("revenue") not in (None, ""):
            return float(r["revenue"])
        if region == "Outside U.S.":
            t, u = self.region_revenue(quarter, "Total Revenue"), self.region_revenue(quarter, "U.S.")
            return None if t is None or u is None else t - u
        return None

    def product_areas(self):
        out = {}
        for r in self.rows["product_revenue"]:
            out[r["product"]] = r["area"]
        return out

    # ---------- 修改 ----------
    def replace(self, name, keep, new_rows):
        """删掉 keep(row) 为 False 的行，再加入 new_rows（字典，值可以是数字）。"""
        cols = self.columns[name]
        kept = [r for r in self.rows[name] if keep(r)]
        for r in new_rows:
            kept.append({c: fmt(r.get(c)) for c in cols})
        self.rows[name] = kept
        self._sort(name)

    def _sort(self, name):
        rows = self.rows[name]
        if name == "product_revenue":
            rows.sort(key=lambda r: (r["quarter"], r["area"], r["product"].startswith("Other"), r["product"]))
        elif name == "price_volume":
            rows.sort(key=lambda r: (r["quarter"], REGION_ORDER.index(r["region"]) if r["region"] in REGION_ORDER else 99))
        elif name == "price_volume_annual":
            rows.sort(key=lambda r: (int(r["months"]) < 12, r["year"], REGION_ORDER.index(r["region"]) if r["region"] in REGION_ORDER else 99))
        elif name == "income_annual":
            rows.sort(key=lambda r: (int(r["months"]) < 12, r["year"]))
        elif name in ("income_statement", "balance_sheet"):
            rows.sort(key=lambda r: r["quarter"])

    # ---------- 写回 ----------
    def dump(self, name):
        if name == "events":
            lines = [json.dumps(e, ensure_ascii=False) for e in sorted(self.rows[name], key=lambda e: (e["date"], e["id"]))]
            return "[\n" + ",\n".join("  " + x for x in lines) + "\n]\n"
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=self.columns[name], lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        for r in self.rows[name]:
            w.writerow(r)
        return buf.getvalue()

    def save(self, out_dir=None):
        d = out_dir or self.dir
        os.makedirs(d, exist_ok=True)
        changed = []
        for n in NAMES:
            path = os.path.join(d, n + (".json" if n == "events" else ".csv"))
            text = self.dump(n)
            old = None
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    old = f.read()
            if old != text:
                with open(path, "w", encoding="utf-8", newline="") as f:
                    f.write(text)
                changed.append(n)
        return changed
