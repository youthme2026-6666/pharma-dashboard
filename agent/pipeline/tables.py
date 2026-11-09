"""把 SEC 申报文件（HTML）里的表格拆成“表 → 行 → 单元格文字”，并解析其中的数字。

只用标准库。单元格里单独的 "$"、")"、"%" 会被丢掉（报表排版常把它们放在独立的格子里），
与解析礼来 10-Q 转换件时的规则一致，所以同一套行级解析逻辑对两种来源都适用。
"""
import re
from html.parser import HTMLParser

_WS = re.compile(r"\s+")


def clean_text(s):
    return _WS.sub(" ", s.replace("\xa0", " ").replace("​", "")).strip()


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self._stack = []
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._stack.append([])
        elif tag == "tr" and self._stack:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag in ("br", "p", "div") and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(clean_text("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._stack:
            self._stack[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._stack:
            self.tables.append(self._stack.pop())

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


_FOOTNOTE = re.compile(r"^\(\d\)$")


def merge_cells(row):
    """SEC 的 HTML 常把符号拆进单独的格子：'$' 丢掉；'%' 和 ')' 接到前一格（'39' '%' → '39%'，'(17' ')' '%' → '(17)%'）；
    '(' 接到后一格。这样和礼来文件里“一格一个值”的写法一致。
    （单独一格的 '(7)' 可能是负 7，也可能是脚注，这里不猜，交给知道每行该有几个数的解析函数处理。）"""
    out, pending = [], ""
    for c in row:
        if c in ("", "$"):
            continue
        if c in ("%", ")", ")%", "%)") and out:
            out[-1] = out[-1] + c
            continue
        if c == "(":
            pending += "("
            continue
        out.append(pending + c)
        pending = ""
    return out


def html_tables(html):
    """返回 [表[行[单元格文字]]]：去掉空格子和单独的 '$'，把拆开的 '%'、括号、脚注并回相邻格子。"""
    p = _TableParser()
    p.feed(html)
    p.close()
    return [[merge_cells(row) for row in t] for t in p.tables]


def html_text(html):
    """整篇文字（用于在新闻稿里查找一次性项目说明）。"""
    class _T(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []

        def handle_data(self, d):
            self.parts.append(d)

        def handle_starttag(self, tag, attrs):
            if tag in ("p", "br", "div", "tr", "li"):
                self.parts.append("\n")

    t = _T()
    t.feed(html)
    return re.sub(r"[ \t\xa0]+", " ", "".join(t.parts))


_NUM = re.compile(r"^\(?\s*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*\)?\s*%?\s*\)?$")
_DASH = re.compile(r"^[—–-]+\s*%?$")


def num(s):
    """'4,885' -> 4885.0；'(2)' / '(10)%' -> 负数；'—' / '— %' -> 0；'NM' 等 -> None。"""
    if s is None:
        return None
    t = str(s).strip().replace("−", "-")
    if not t:
        return None
    if _DASH.match(t):
        return 0.0
    m = _NUM.match(t)
    if m:
        v = float(m.group(1).replace(",", ""))
        return -v if t.startswith("(") else v
    m = re.match(r"^-\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*%?$", t)
    if m:
        return -float(m.group(1).replace(",", ""))
    return None


def is_pct_or_nm(s):
    t = str(s).strip()
    return t.endswith("%") or t.upper() == "NM"


def norm_label(s):
    """去掉 '(Note 6)'、产品名后的脚注 '(1)'、多余空格。"""
    t = re.sub(r"\(Note \d+\)", "", str(s or ""), flags=re.I)
    t = re.sub(r"(?<=[A-Za-z])\(\d+\)", "", t)
    return clean_text(t)


def values(row, drop_pct=False):
    """一行里第一格之后的数字（可选去掉百分比和 NM 格）。"""
    cells = row[1:]
    if drop_pct:
        cells = [c for c in cells if not is_pct_or_nm(c)]
    out = []
    for c in cells:
        v = num(c)
        if v is not None:
            out.append(v)
    return out


def drop_footnote(row, expected):
    """产品名后面的脚注如果单独占了一格（'Zepbound', '(1)', ...），这一行会多出一个数；
    只在“正好多一个、且多出来的是 (数字) 形式”时去掉它。expected 可以是一个数或几个可能的数。"""
    exp = expected if isinstance(expected, (list, tuple, set)) else [expected]
    cells = row[1:]
    if len(cells) >= 2 and _FOOTNOTE.match(cells[0].strip()) and (len(values(row)) - 1) in exp and len(values(row)) not in exp:
        return [row[0] + cells[0].strip()] + cells[1:]
    return row


def first_labels(table):
    return [norm_label(r[0]) if r else "" for r in table]
