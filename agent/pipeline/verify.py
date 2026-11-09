"""对 GPT 输出做确定性核对：网址来源、原文引用、数字一致。GPT 的任何结果都必须先过这里才会被采用。"""
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit


def norm_url(u):
    """统一网址写法：https、去掉末尾斜杠、查询串和锚点，主机名小写。"""
    try:
        p = urlsplit((u or "").strip())
    except ValueError:
        return ""
    if p.scheme not in ("http", "https") or not p.netloc:
        return ""
    path = re.sub(r"/+$", "", p.path) or "/"
    return urlunsplit(("https", p.netloc.lower(), path, "", ""))


def host_allowed(u, domains):
    host = urlsplit(u).netloc.lower().split(":")[0]
    return any(host == d or host.endswith("." + d) for d in domains)


def norm_text(s):
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-").replace("−", "-")
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_in(quote, text):
    q = norm_text(quote)
    return len(q) >= 12 and q in norm_text(text)


_NUM = r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?"


def _f(m):
    return float(m.group(1).replace(",", "") + ("." + m.group(2) if m.group(2) else ""))


def amounts_in(text):
    """英文原文里的金额（换算成百万美元）、百分比、每股金额。"""
    t = norm_text(text)
    money, pct, plain = [], [], []
    for m in re.finditer(r"\$?\s*" + _NUM + r"\s*(billion|million|bn|mn)?", t):
        v = _f(m)
        unit = m.group(3)
        plain.append(v)
        if unit in ("billion", "bn"):
            money.append(v * 1000)
        elif unit in ("million", "mn"):
            money.append(v)
        else:
            money.append(v)
    for m in re.finditer(_NUM + r"\s*(%|percent)", t):
        pct.append(_f(m))
    return money, pct, plain


def numbers_supported(title_zh, evidence):
    """中文标题里的每个数字都要能在英文引用里找到：'4.1 亿美元' ↔ '$410 million' 或 '$0.41 billion'；'3.29 美元' ↔ '$3.29'；'54%' ↔ '54%'。"""
    joined = " ".join(evidence or [])
    money, pct, plain = amounts_in(joined)
    t = unicodedata.normalize("NFKC", title_zh or "")
    problems = []
    consumed = set()
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*亿美元", t):
        a = float(m.group(1)) * 100
        consumed.update(range(m.start(), m.end()))
        if not any(abs(v - a) <= max(1.0, a * 0.006) or abs(v - a) <= 50 and "约" in t[max(0, m.start() - 3):m.start()] for v in money):
            problems.append(m.group(0))
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*美元", t):
        if any(i in consumed for i in range(m.start(), m.end())):
            continue
        a = float(m.group(1))
        consumed.update(range(m.start(), m.end()))
        if not any(abs(v - a) <= 0.005 for v in plain):
            problems.append(m.group(0))
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*%", t):
        a = float(m.group(1))
        consumed.update(range(m.start(), m.end()))
        if not any(abs(v - a) <= 0.05 for v in pct):
            problems.append(m.group(0))
    for m in re.finditer(r"\d+(?:\.\d+)?", t):
        if any(i in consumed for i in range(m.start(), m.end())):
            continue
        # 其余数字（年份、季度序号、剂量、试验编号等）只要原文出现过即可
        v = float(m.group(0))
        if not any(abs(x - v) < 1e-9 for x in plain) and not (1900 <= v <= 2100) and v not in (1, 2, 3, 4):
            problems.append(m.group(0))
    return problems


def is_date(s):
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", s or ""))


def quarter_of_date(d):
    return "%sQ%d" % (d[:4], (int(d[5:7]) - 1) // 3 + 1)


def has_cjk(s):
    return bool(re.search(r"[一-鿿]", s or ""))


_CJK = "[一-鿿]"


def cjk_spacing(s):
    """中文与英文、数字之间加一个空格，和现有标题的写法一致：“用于12岁” → “用于 12 岁”。"""
    s = re.sub("(" + _CJK + ")([A-Za-z0-9$])", r"\1 \2", s or "")
    return re.sub("([A-Za-z0-9%])(" + _CJK + ")", r"\1 \2", s)


def slug(s, n=24):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:n].strip("-") or "event"
