"""港股（披露易，hkex.py）与 A 股（巨潮，cninfo.py）适配器共用的部分：定期报告 PDF → 统一格式。

每份报告只处理一次，结果存在 state/<公司>/reports/<报告>.json（报表行原文、对照、数值、核对结果），以后直接用：
1. 下载 PDF（缓存在 agent/.cache/<来源>/，不提交），用 pypdf 逐页抽文字（pypdf 只在这里用，导入放在函数里）。
2. 找合并报表：A 股“合并利润表 / 合并资产负债表 / 合并现金流量表”；港股 “Consolidated Statement of Profit or Loss /
   Financial Position（Balance Sheet）/ Cash Flows”。母公司报表、目录、五年摘要都不算（只认两列数的表）。
3. 程序逐行拆成“行名 + 本期数 + 上期数”（数字由程序从原文读出，GPT 不读数）。
4. 对照（统一格式的每个科目对应哪几行）：A 股合并报表是财政部统一格式，标准行名用固定对照表；对照表认不全时、
   以及港股（各家写法不同）交给 GPT（ctx.gpt 结构化输出，只给行号）；同一公司版式没变时沿用上一份的对照。
5. 核对（必过，任何一项不过，这份报告整份不写入，记为“需要处理”）：
   - GPT 给的行号必须存在、行名要符合科目（关键词核对，例如研发费用的行名要有“研发 / research”）；单位原文要在表头里；
   - 每个数都在该页原文里（程序从原文读出，括号是负数，“—/–”是零），单位统一换算成百万；
   - 毛利 = 营收 − 营业成本（有毛利行时）、净利润 = 税前利润 − 所得税、资产 = 负债 + 权益；
   - 整张表勾稽：A 股按统一格式的结构（营业总成本 = 各项成本费用之和、营业利润、利润总额、净利润 = 归母 + 少数股东，
     资产总计 = 流动 + 非流动，现金流入小计 − 流出小计 = 净额 …）；港股利润表从营收一行行加到税前利润；
   - 本期和上年同期两列都抽；上年同期（以及同一期间在别的报告里的数）要和以前的一致，不一致时报告里必须有重述说明
     （“重述 / 追溯调整 / restated / reclassified …”），否则算不过。
   容差按参与计算的四舍五入项数设定（每项半个最小单位），不为了通过而放宽。
6. 期间：A 股一季报 Q1、半年报 H1、三季报（前三季度）、年报 FY；港股中期 H1、年度 FY（有季度业绩也取）。推算的行
   basis=derived：Q2 = H1 − Q1，Q3 = 前三季度 − H1，Q4 = FY − 前三季度，H2 = FY − H1（每股收益不推算）。
   报告里的上年同期数也用来补更早的期间（例如 2023 年报里的 2022 年数）。

网络、GPT 的偶发失败只记提醒（下次再试）；找不到报表的报告只记提醒；数字核对不过才是“需要处理”。
"""
import datetime as dt
import hashlib
import io
import json
import logging
import os
import re
import time
import unicodedata
import urllib.error
import urllib.request

from .base import Result

PARSE_VERSION = 1        # 文字 → 报表行 的规则版本：改了就重新下载、重新拆行
CHECK_VERSION = 1        # 报表行 → 科目、核对 的规则版本：改了就用 state 里存的报表行重新核对（不用下载）
FIRST_YEAR = 2023        # 从 2023 财年的报告开始补（报告里的上年同期数再补到 2022 年）
MAX_DOCS_PER_RUN = 8     # 每家公司每次最多新处理几份报告（首次补数据分几天完成，避免超过工作流时限）
TIME_BUDGET = 300        # 每家公司每次处理新报告的时间上限（秒）
RUN_BUDGET = 900         # 一次运行里所有港股、A 股公司合计的时间上限（秒；工作流整体限时 30 分钟）
_RUN = {"start": None}
DEFAULT_UA = "PharmaDashboard/0.1 (research)"   # 没有 SEC_USER_AGENT 时用的标识（工作流里用 SEC_USER_AGENT）
REPORTS = "reports"      # state/<公司>/reports/<报告>.json
SPAN_ID = {3: "Q1", 6: "H1", 9: "9M", 12: "FY"}   # 年初至今的月数 → 报告编号里的写法

INCOME_FIELDS = ["revenue", "cost_of_sales", "gross_profit", "rnd", "sga", "operating_income", "pretax_income",
                 "income_tax", "net_income", "net_income_parent", "eps_diluted"]
BALANCE_FIELDS = ["cash", "short_term_investments", "receivables", "inventories", "total_current_assets", "total_assets",
                  "total_current_liabilities", "total_debt", "total_liabilities", "total_equity", "equity_parent"]
CASHFLOW_FIELDS = ["cfo", "capex", "dividends_paid", "buybacks"]
FIELDS = {"income": INCOME_FIELDS, "balance": BALANCE_FIELDS, "cashflow": CASHFLOW_FIELDS}
KINDS = ("income", "balance", "cashflow")
KIND_ZH = {"income": "利润表", "balance": "资产负债表", "cashflow": "现金流量表"}
REQUIRED = {"income": ["revenue", "pretax_income", "net_income"],
            "balance": ["total_assets", "total_liabilities", "total_equity"], "cashflow": ["cfo"]}
# 跨报告比对时，这些科目对不上就算“需要处理”；其余科目（对照有判断成分）对不上只提醒
CORE = {"income": ["revenue", "pretax_income", "net_income", "net_income_parent", "eps_diluted"],
        "balance": ["total_assets", "total_liabilities", "total_equity", "total_current_assets", "total_current_liabilities"],
        "cashflow": ["cfo"]}
EXPENSE = ("cost_of_sales", "rnd", "sga", "capex", "dividends_paid", "buybacks")   # 统一格式里记正数
NOT_ADDITIVE = ("eps_diluted",)


class FetchError(RuntimeError):
    pass


# ---------------------------------------------------------------- 小工具
def norm(s):
    """统一写法：全角转半角（NFKC）、弯引号转直引号、不换行空格转空格。"""
    s = unicodedata.normalize("NFKC", s or "")
    for a, b in (("\u2019", "'"), ("\u2018", "'"), ("\u201c", '"'), ("\u201d", '"'), ("\u00a0", " "), ("\u3000", " ")):
        s = s.replace(a, b)
    return s


def _sha(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


_NUM = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_VAL = re.compile(r"^(?:RMB|US\$|HK\$|\$|¥)?(\()?([-−])?(%s)(\))?$" % _NUM)
_DASH = re.compile(r"^[-−–—]+$")
_YEAR = re.compile(r"^(?:19|20)\d\d$")
_NOTE = re.compile(r"^\d{1,2}(?:\.\d{1,2})?$")


def parse_value(tok):
    """报表里的一个数：'(1,296,044)' → -1296044；'-116,288,242.80' → -116288242.8；'—' '–' → 0；'RMB(0.46)' → -0.46。
    不是数返回 None。"""
    t = (tok or "").strip()
    if not t:
        return None
    if _DASH.match(t):
        return 0.0
    m = _VAL.match(t)
    if not m or bool(m.group(1)) != bool(m.group(4)):
        return None
    v = float(m.group(3).replace(",", ""))
    return -v if (m.group(1) or m.group(2)) else v


def decimals(tok):
    m = re.search(r"\.(\d+)", tok or "")
    return len(m.group(1)) if m else 0


def month_end(y, m):
    return dt.date(y + m // 12, m % 12 + 1, 1) - dt.timedelta(days=1)


def fy_start(fy, fye):
    """财年 fy（以截止日所在年份命名）的第一天。"""
    return month_end(fy - 1, fye) + dt.timedelta(days=1)


def span_end(fy, months, fye):
    s = fy_start(fy, fye)
    k = s.month - 1 + months - 1
    return month_end(s.year + k // 12, k % 12 + 1)


def _read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    old = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            old = f.read()
    if old != text:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return True
    return False


# ---------------------------------------------------------------- 网络
class Http:
    """声明身份（User-Agent 用 SEC_USER_AGENT）、限速、重试、缓存。"""

    def __init__(self, cache_dir, min_interval=1.0, retries=3, timeout=120, opener=None):
        self.cache_dir = cache_dir
        self.min_interval, self.retries, self.timeout = min_interval, retries, timeout
        self._open = opener or urllib.request.urlopen
        self._last = 0.0
        self.ua = os.environ.get("SEC_USER_AGENT", "").strip() or DEFAULT_UA
        self.fetched = 0

    def _fetch(self, url, data=None, headers=None):
        delay = 3.0
        for attempt in range(self.retries + 1):
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            h = {"User-Agent": self.ua, "Accept": "*/*"}
            h.update(headers or {})
            req = urllib.request.Request(url, data=data, headers=h, method="POST" if data is not None else "GET")
            self._last = time.time()
            try:
                with self._open(req, timeout=self.timeout) as r:
                    body = r.read()
                self.fetched += 1
                return body
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise FetchError("HTTP %s：%s" % (e.code, url))
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                if attempt < self.retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise FetchError("无法访问 %s：%s" % (url, e))
        raise FetchError("无法访问 %s" % url)

    def get(self, url, data=None, headers=None, ttl=None, suffix=""):
        """ttl=None 永久缓存（PDF 不会变）；ttl=0 不用缓存；data 不为空时用 POST。"""
        key = _sha(url + "\n" + (data.decode("utf-8") if data else ""))[:24] + suffix
        path = os.path.join(self.cache_dir, key) if self.cache_dir else None
        if path and ttl != 0 and os.path.exists(path) and (ttl is None or time.time() - os.path.getmtime(path) < ttl):
            with open(path, "rb") as f:
                return f.read()
        body = self._fetch(url, data, headers)
        if path and ttl != 0:
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(path + ".tmp", "wb") as f:
                f.write(body)
            os.replace(path + ".tmp", path)
        return body

    def json(self, url, data=None, headers=None, ttl=3600):
        body = self.get(url, data=data, headers=headers, ttl=ttl, suffix=".json")
        try:
            return json.loads(body.decode("utf-8"))
        except ValueError:
            raise FetchError("返回的不是 JSON：%s" % url)


def extract_pages(data):
    """PDF 字节 → 每页文字。pypdf 只在港股、A 股适配器里用（AGENTS.md 规则 1）。"""
    import pypdf
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    r = pypdf.PdfReader(io.BytesIO(data))
    if r.is_encrypted:
        r.decrypt("")        # 只设了权限的公开文件，空口令即可打开
    out = []
    for p in r.pages:
        try:
            out.append(p.extract_text() or "")
        except Exception:    # noqa: BLE001 个别页抽不出文字不影响其他页
            out.append("")
    return out


def extract_layout(data, numbers):
    """指定页的“保留横向位置”的文字（pypdf 的 layout 模式），用来判断一行只有一个数时它在哪一列。"""
    import pypdf
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    r = pypdf.PdfReader(io.BytesIO(data))
    if r.is_encrypted:
        r.decrypt("")
    out = {}
    for n in numbers:
        try:
            out[n] = r.pages[n - 1].extract_text(extraction_mode="layout") or ""
        except Exception:    # noqa: BLE001
            out[n] = ""
    return out


def pdf_layout(http, url, numbers):
    lpath = os.path.join(http.cache_dir, "layout", _sha(url)[:24] + ".json") if http.cache_dir else None
    cached = (_read_json(lpath, {}) or {}) if lpath else {}
    have = cached.get("pages", {}) if cached.get("v") == PARSE_VERSION else {}
    need = [n for n in numbers if str(n) not in have]
    if need:
        body = http.get(url, suffix=".pdf")
        have.update({str(k): v for k, v in extract_layout(body, need).items()})
        if lpath:
            _write_json(lpath, {"v": PARSE_VERSION, "url": url, "pages": have})
    return {int(k): v for k, v in have.items() if int(k) in numbers}


def pdf_pages(http, url):
    """下载 PDF 并逐页抽文字（抽出的文字也缓存）。"""
    tpath = os.path.join(http.cache_dir, "text", _sha(url)[:24] + ".json") if http.cache_dir else None
    if tpath and os.path.exists(tpath):
        cached = _read_json(tpath, {})
        if cached.get("v") == PARSE_VERSION:
            return cached["pages"]
    body = http.get(url, suffix=".pdf")
    if not body.startswith(b"%PDF"):
        raise FetchError("下载的不是 PDF：%s" % url)
    pages = extract_pages(body)
    if tpath:
        _write_json(tpath, {"v": PARSE_VERSION, "url": url, "pages": pages})
    return pages


# ---------------------------------------------------------------- 找合并报表
_PAGE_NO = re.compile(r"^(?:[-–—]\s*\d{1,3}\s*[-–—]|\d{1,3}\s*/\s*\d{1,3}|\d{1,3}|page \d{1,3})$", re.I)
_EN_CAPS = re.compile(r"^(?:(?:UNAUDITED|AUDITED|INTERIM|CONDENSED|Unaudited|Audited|Interim|Condensed)\s+)*"
                      r"(?:CONSOLIDATED|Consolidated)\s+(?:STATEMENTS?|Statements?|INCOME|Income|BALANCE|Balance|CASH|Cash)\b")
_EN_TITLE = re.compile(r"^(?:(?:unaudited|audited|interim|condensed)\s+)*consolidated\s+(.+)$")
_SENTENCE = re.compile(r"\b(?:has|have|is|are|was|were|which|should|presents?|prepared|refer)\b")
_TITLE_TAIL = re.compile(r"^(?:and )?(?:other )?(?:comprehensive (?:income|loss|expense)|position|financial position|flows|cash flows|"
                         r"sheets?|profit or loss(?: and other comprehensive (?:income|loss))?)\b(?:\s+(?:for|as at|at)\b.*)?$")
_STOP = re.compile(r"^(?:公司负责人|企业负责人|法定代表人|主管会计工作负责人)|^(?:notes?(?: to the .*)?:?|附注|财务报表附注.*|"
                   r"(?:合并|母公司|公司)?(?:所有者|股东)权益变动表(?:\s*[\(（]续[\)）])?)$", re.I)


def clean_pages(pages):
    """每页拆行，去掉页码行和每页重复的页眉页脚（例如“江苏恒瑞医药股份有限公司 2026 年第三季度报告”）。"""
    split = [[l.strip() for l in norm(p).splitlines() if l.strip()] for p in pages]
    edge = {}
    for ls in split:
        for l in set(ls[:2] + ls[-2:]):
            k = re.sub(r"\d+", "#", l)
            edge[k] = edge.get(k, 0) + 1
    many = max(3, int(len([ls for ls in split if ls]) * 0.3))
    repeated = {k for k, n in edge.items() if n >= many and len(k) > 3}
    out = []
    for ls in split:
        keep = []
        for i, l in enumerate(ls):
            at_edge = i < 2 or i >= len(ls) - 2
            if at_edge and (_PAGE_NO.match(l) or re.sub(r"\d+", "#", l) in repeated):
                continue
            keep.append(l)
        out.append(keep)
    return out


def title_kind(line, nxt=""):
    """这一行是不是报表标题：income / balance / cashflow / oci（单独的综合收益表）/ other（母公司表、权益变动表）；
    不是返回 None。标题折成两行时把下一行接上再认。返回 (kind, 用掉的行数)。"""
    for text, used in ((line, 1), (line + " " + nxt, 2)):
        k = _title_kind(text)
        if k:
            if used == 1 and nxt and _title_kind(line + " " + nxt) == k and _TITLE_TAIL.match(norm(nxt).strip().lower()):
                return k, 2              # 标题折成两行：...PROFIT OR LOSS AND OTHER / COMPREHENSIVE INCOME
            return k, used
        if used == 1 and not re.match(r"^(?:(?:unaudited|audited|interim|condensed)\s+)*consolidated\s+statements?\s+of\b",
                                      re.sub(r"\s+", " ", norm(line).strip().lower())):
            return None, 0
    return None, 0


def _title_kind(text):
    zh = re.sub(r"\s+", "", norm(text))
    m = re.match(r"^(?:[一二三四五六七八九十\d]+[、.]|\([一二三四五六七八九十\d]+\))?(合并|母公司|公司)(年初到报告期末|本报告期|本期)?"
                 r"(利润表|资产负债表|现金流量表|(?:所有者|股东)权益变动表)(?:\(续\))?(?:\d{4}年.*|\(.*\))?$", zh)
    if m and len(zh) <= 40:
        if m.group(1) != "合并" or "权益变动" in m.group(3) or m.group(2) in ("本报告期", "本期"):
            return "other"               # 母公司报表、权益变动表、只有本季度的利润表（深市三季报另有年初至今的表）
        return {"利润表": "income", "资产负债表": "balance", "现金流量表": "cashflow"}[m.group(3)]
    orig = re.sub(r"\s+", " ", norm(text).strip())
    l = orig.lower()
    if len(l) > 140 or re.search(r"\s\d{1,3}$", l) or _SENTENCE.search(l):
        return None                      # 目录行（末尾是页码）或正文句子
    if not _EN_CAPS.match(orig):
        return None                      # 正文段落里折到行首的“consolidated statement of …”（小写）不是标题
    m = _EN_TITLE.match(l)
    if not m:
        return None
    rest = m.group(1)
    if re.match(r"statements? of profit or loss\b|income statements?\b|statements? of (?:operations|income)\b", rest):
        return "income"
    if re.match(r"statements? of comprehensive (?:income|loss)\b", rest):
        return "oci"
    if re.match(r"statements? of financial position\b|balance sheets?\b", rest):
        return "balance"
    if re.match(r"statements? of cash ?flows?\b|cash ?flow statements?\b", rest):
        return "cashflow"
    if re.match(r"statements? of changes in (?:equity|shareholders)", rest):
        return "other"
    return None


def find_blocks(pages):
    """按顺序找出各张合并报表的文字块：[(kind, [(页码, 行)])]。同一张表跨页（续页重复标题）合并成一块。"""
    blocks, cur = [], None
    for pno, lines in enumerate(pages, 1):
        i = 0
        while i < len(lines):
            line = lines[i]
            kind, used = title_kind(line, lines[i + 1] if i + 1 < len(lines) else "")
            if kind:
                if cur and cur[0] == kind and pno - cur[3] <= 1:
                    i += used                       # 续页重复的标题（“合并资产负债表（续）”）
                    continue
                if cur:
                    blocks.append(cur)
                cur = [kind, [], pno, pno] if kind in ("income", "balance", "cashflow", "oci") else None
                i += used
                continue
            if cur:
                if _STOP.match(line) or pno - cur[2] > 5:
                    blocks.append(cur)
                    cur = None
                else:
                    cur[1].append((pno, line))
                    cur[3] = pno
            i += 1
    if cur:
        blocks.append(cur)
    return [(b[0], b[1]) for b in blocks if b[1]]


# ---------------------------------------------------------------- 逐行拆数
def split_line(line):
    """'Cost of sales (1,296,044) (833,452)' → ('Cost of sales', [(-1296044.0, '(1,296,044)'), ...])。从右往左取数。"""
    toks = line.split()
    vals, i = [], len(toks)
    while i > 0:
        t = toks[i - 1]
        if t.lower() in ("yuan", "cents", "cent") and i > 1 and parse_value(toks[i - 2]) is not None:
            i -= 1
            continue
        v = parse_value(t)
        if v is None:
            break
        vals.insert(0, (v, t))
        i -= 1
    return " ".join(toks[:i]), vals


_HEADERISH = re.compile(r"^(?:notes?|附注|项目|rmb.?'?000|rmb|rmb million|us\$.?'?000|\(?(?:un)?audited\)?|\(?未经审计\)?|\(?经审计\)?)"
                        r"(?:\s+(?:notes?|rmb.?'?000|rmb|\(?(?:un)?audited\)?|rmb cents?|cents?|yuan))*$", re.I)
_DATEISH = re.compile(r"单位|编制单位|审计类型|币种|\bended\b|^as (?:at|of)\b|^at\b|^for the\b|^(?:six|three|nine|twelve) months|"
                      r"^year ended|\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\b|"
                      r"\d{4}\s*年|^\d{1,2}\s+\w+$|^\(?(?:un)?audited\)?|^\(?expressed in", re.I)
_SECTION_WORDS = {"assets", "liabilities", "equity", "non-current assets", "current assets", "current liabilities",
                  "non-current liabilities", "capital and reserves", "equity and liabilities", "liabilities and equity",
                  "operating activities", "investing activities", "financing activities", "cash flows from operating activities",
                  "cash flows from investing activities", "cash flows from financing activities", "revenue", "income", "expenses"}
_ENUM = re.compile(r"^(?:\d+[.、]|\(\d+\)|[一二三四五六七八九十]+、|\([一二三四五六七八九十]+\)|其中:|加:|减:|less:|add:)", re.I)
_CONNECT = re.compile(r"(?:\b(?:of|for|to|and|on|in|at|from|by|the|with|using|through|attributable|less|net|or|before|after)|[(\-–—/,、及和与或])$", re.I)


def _is_header(text):
    t = text.strip()
    return bool(_HEADERISH.match(t) or _DATEISH.search(t))


def _is_section(text):
    t = re.sub(r"[:：]$", "", text.strip()).lower()
    return t in _SECTION_WORDS or text.strip().endswith((":", "："))


def _cjk(s):
    return bool(re.search(r"[一-鿿]", s or ""))


# A 股统一格式报表的行名（财会〔2019〕6 号等），用来判断中文行名折行：上一行本身是完整科目时不接，接上后是（某个科目的开头）才接
CAS_ITEMS = set("""货币资金 结算备付金 拆出资金 交易性金融资产 以公允价值计量且其变动计入当期损益的金融资产 衍生金融资产 应收票据 应收账款
应收票据及应收账款 应收款项融资 预付款项 应收保费 应收分保账款 应收分保合同准备金 其他应收款 应收利息 应收股利 买入返售金融资产 存货
数据资源 合同资产 持有待售资产 一年内到期的非流动资产 其他流动资产 流动资产合计 发放贷款和垫款 债权投资 可供出售金融资产 其他债权投资
持有至到期投资 长期应收款 长期股权投资 其他权益工具投资 其他非流动金融资产 投资性房地产 固定资产 在建工程 生产性生物资产 油气资产
使用权资产 无形资产 开发支出 商誉 长期待摊费用 递延所得税资产 其他非流动资产 非流动资产合计 资产总计 短期借款 向中央银行借款 拆入资金
交易性金融负债 以公允价值计量且其变动计入当期损益的金融负债 衍生金融负债 应付票据 应付账款 应付票据及应付账款 预收款项 合同负债
卖出回购金融资产款 吸收存款及同业存放 代理买卖证券款 代理承销证券款 应付职工薪酬 应交税费 其他应付款 应付利息 应付股利
应付手续费及佣金 应付分保账款 持有待售负债 一年内到期的非流动负债 其他流动负债 流动负债合计 保险合同准备金 长期借款 应付债券 优先股
永续债 租赁负债 长期应付款 长期应付职工薪酬 预计负债 递延收益 递延所得税负债 其他非流动负债 非流动负债合计 负债合计 实收资本 股本
其他权益工具 资本公积 库存股 其他综合收益 专项储备 盈余公积 一般风险准备 未分配利润 归属于母公司所有者权益合计 归属于母公司股东权益合计
少数股东权益 所有者权益合计 股东权益合计 负债和所有者权益总计 负债和股东权益总计 营业总收入 营业收入 利息收入 已赚保费 手续费及佣金收入
营业总成本 营业成本 利息支出 手续费及佣金支出 退保金 赔付支出净额 提取保险责任准备金净额 保单红利支出 分保费用 税金及附加 销售费用
管理费用 研发费用 财务费用 利息费用 其他收益 投资收益 对联营企业和合营企业的投资收益 以摊余成本计量的金融资产终止确认收益 汇兑收益
净敞口套期收益 公允价值变动收益 信用减值损失 资产减值损失 资产处置收益 营业利润 营业外收入 营业外支出 利润总额 所得税费用 净利润
持续经营净利润 终止经营净利润 归属于母公司股东的净利润 归属于母公司所有者的净利润 少数股东损益 其他综合收益的税后净额
归属母公司所有者的其他综合收益的税后净额 不能重分类进损益的其他综合收益 重新计量设定受益计划变动额 权益法下不能转损益的其他综合收益
其他权益工具投资公允价值变动 企业自身信用风险公允价值变动 将重分类进损益的其他综合收益 权益法下可转损益的其他综合收益
其他债权投资公允价值变动 金融资产重分类计入其他综合收益的金额 其他债权投资信用减值准备 现金流量套期储备 外币财务报表折算差额
归属于少数股东的其他综合收益的税后净额 综合收益总额 归属于母公司所有者的综合收益总额 归属于少数股东的综合收益总额 每股收益
基本每股收益 稀释每股收益 销售商品、提供劳务收到的现金 客户存款和同业存放款项净增加额 向中央银行借款净增加额 向其他金融机构拆入资金净增加额
收到原保险合同保费取得的现金 收到再保业务现金净额 保户储金及投资款净增加额 收取利息、手续费及佣金的现金 拆入资金净增加额 回购业务资金净增加额
代理买卖证券收到的现金净额 收到的税费返还 收到其他与经营活动有关的现金 经营活动现金流入小计 购买商品、接受劳务支付的现金
客户贷款及垫款净增加额 存放中央银行和同业款项净增加额 支付原保险合同赔付款项的现金 拆出资金净增加额 支付利息、手续费及佣金的现金
支付保单红利的现金 支付给职工及为职工支付的现金 支付给职工以及为职工支付的现金 支付的各项税费 支付其他与经营活动有关的现金
经营活动现金流出小计 经营活动产生的现金流量净额 收回投资收到的现金 取得投资收益收到的现金 处置固定资产、无形资产和其他长期资产收回的现金净额
处置子公司及其他营业单位收到的现金净额 收到其他与投资活动有关的现金 投资活动现金流入小计 购建固定资产、无形资产和其他长期资产支付的现金
投资支付的现金 质押贷款净增加额 取得子公司及其他营业单位支付的现金净额 支付其他与投资活动有关的现金 投资活动现金流出小计
投资活动产生的现金流量净额 吸收投资收到的现金 子公司吸收少数股东投资收到的现金 取得借款收到的现金 发行债券收到的现金
收到其他与筹资活动有关的现金 筹资活动现金流入小计 偿还债务支付的现金 分配股利、利润或偿付利息支付的现金 子公司支付给少数股东的股利、利润
支付其他与筹资活动有关的现金 筹资活动现金流出小计 筹资活动产生的现金流量净额 汇率变动对现金及现金等价物的影响 现金及现金等价物净增加额
期初现金及现金等价物余额 期末现金及现金等价物余额""".split())


def _cas_known(text):
    return cas_core(text) in CAS_ITEMS


def _cas_prefix(text):
    c = cas_core(text)
    return len(c) >= 2 and any(i.startswith(c) for i in CAS_ITEMS)


def _joinable(prev, nxt):
    """上一行（只有文字）是不是这一行行名的前半截（行名折行）。"""
    p, n = prev.strip(), nxt.strip()
    if not p or not n or _is_section(p) or _is_header(p):
        return False
    if _cjk(p) and _cjk(n):
        if _cas_known(p) and not _cas_known(p + n):
            return False                 # 上一行本身是完整科目（深市报表里没数的科目也单独成行）
        if (_cas_known(p + n) or _cas_prefix(p + n)) and not _cas_known(n):
            return True                  # 购建固定资产、无形资产和其他长期资 / 产支付的现金
    if _ENUM.match(n):
        return False
    if n[0] in "-–—(" or n[0].islower() or n.startswith(("列)", "合计", "总计")):
        return True
    if _CONNECT.search(p) or p.count("(") > p.count(")"):
        return True
    words = n.split()
    if p.upper() == p and re.search(r"[A-Z]", p) and words and words[0].upper() == words[0] and re.search(r"[A-Z]", words[0]):
        return True                      # 全大写的标题折行：TOTAL ASSETS LESS CURRENT / LIABILITIES
    if _cjk(p) and _cjk(n) and len(n) <= 10 and (len(p) >= 12 or p.startswith("其中")) and (
            n.startswith(("的", "付的", "回的")) or p.endswith(("、", "和", "及", "与", "支", "收", "填", "以", "的"))):
        return True                      # 中文行名折行：购建固定资产、无形资产和其他长期资产支 / 付的现金
                                         # （深市报表里没有数的科目也单独成行，不能接到下一行上）
    return False


_NOTE_REF = (r"(?:附注|注释)?(?:[一二三四五六七八九十]+[、.]\s*(?:\d{1,3}(?:\(\d{1,3}\))?|\(?[一二三四五六七八九十\d]+\))?"
             r"|\d{1,3}(?:\.\d{1,2})?(?:\([0-9a-zA-Z]{1,4}\))*)")


def _clean_label(label):
    """去掉行名末尾（或整个行名就是）的附注号：'货币资金 七、1'、'经营活动产生的现金流量净额 79(1)'、'七、70'、'3.1(b)'。"""
    label = label.strip()
    if re.match(r"^%s$" % _NOTE_REF, label):
        return ""
    return re.sub(r"\s+%s$" % _NOTE_REF, "", label).strip()


def parse_block(lines):
    """报表文字块 → {header, lines: [{label, section, cur, prior, raw}], bad}。
    每行取最后两个数（本期、上期）；前面多出的小整数是附注号；只有文字的行是小标题或折行的行名；
    没有行名的数行是小计，行名记为“[小计] 小节名”。"""
    header, rows, bad = [], [], []
    pending, section, seen_value = "", "", False
    counts = {}
    for ri, (pno, raw) in enumerate(lines):
        label, vals = split_line(raw)
        if not label and len(vals) == 1 and re.match(r"^\d{1,3}$", vals[0][1]):
            continue                     # 夹在表里的页码
        if vals and all(_YEAR.match(t) for _, t in vals):
            if not seen_value:           # 只有年份的行是表头（NOTES 2026 2025）
                header.append(raw)
            continue
        if len(vals) == 1 and _NOTE.match(vals[0][1]) and label:
            vals = []                    # 只有附注号
        if not vals:
            text = label.strip()
            last = rows[-1]["label"] if rows else ""
            if last and not pending and ((last.count("(") > last.count(")") and text.count(")") > text.count("(")) or
                                         (_cjk(text) and not _cas_known(last) and _cas_known(last + text))):
                rows[-1]["label"] = last + " " + text      # 上一行行名的后半截折到了数字下面一行：'四、汇率变动对现金及现金等价物的' / '影响'
                continue
            if not seen_value and (_is_header(text) or not rows):
                header.append(text)
            if _is_header(text) and not _is_section(text):
                continue
            if pending and _joinable(pending, text):
                pending = pending + " " + text
            else:
                if pending:
                    section = pending
                pending = text
            continue
        seen_value = True
        if len(vals) > 2:
            extra = vals[:-2]
            if all(_NOTE.match(t) or _DASH.match(t) for _, t in extra):
                vals = vals[-2:]
            else:
                bad.append(raw)
                continue
        label = _clean_label(label)
        if len(vals) < 2:
            bad.append(raw)
            own = label
            label = (pending + " " + label).strip() if pending and _joinable(pending, label) else label
            rows.append({"label": label, "section": section, "cur": vals[0][1] if vals else "", "prior": "", "raw": ri, "page": pno,
                         "single": raw, "own": own})
            pending = ""
            continue
        if not label:
            if pending and not _is_section(pending):
                label = pending
            else:
                if pending:
                    section = pending
                base = "[小计] " + (section or (rows[-1]["label"] if rows else ""))
                counts[base] = counts.get(base, 0) + 1
                label = base if counts[base] == 1 else "%s #%d" % (base, counts[base])
            pending = ""
        elif pending:
            if _joinable(pending, label):
                if re.match(r"^[-–—]?\s*(?:basic|diluted)\b", label, re.I):
                    section = pending    # Profit per share / – Basic：既是折行也是下面几行的小节
                label = pending + " " + label
            else:
                section = pending
            pending = ""
        rows.append({"label": label, "section": section, "cur": vals[0][1], "prior": vals[1][1], "raw": ri, "page": pno})
    return {"header": header[:14], "lines": rows, "bad": bad, "raw": [l for _, l in lines]}


def _width(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _value_ends(line):
    """layout 文字里一行末尾的数，以及每个数右端的位置（按显示宽度，中文算两格）。"""
    toks = [(m.group(0), m.end()) for m in re.finditer(r"\S+", line)]
    out = []
    for t, end in reversed(toks):
        if parse_value(t) is None:
            break
        out.insert(0, (t, _width(line[:end])))
    return out


def resolve_columns(st, layout):
    """深市等报表里空着的格子不写“—”，一行只有一个数时看不出在哪一列：用 layout 文字里这个数右端的横向位置，
    和同一页两列都有数的行比较，离哪一列近就是哪一列，另一列记“—”（零）。判断不了的行保持原样（算读不出）。"""
    edges = {}
    for pno, text in layout.items():
        a, b = [], []
        for l in norm(text).splitlines():
            v = _value_ends(l)
            if len(v) >= 2 and not all(_YEAR.match(t) for t, _ in v[-2:]):
                a.append(v[-2][1])
                b.append(v[-1][1])
        if len(a) >= 3:
            a.sort()
            b.sort()
            edges[pno] = (a[len(a) // 2], b[len(b) // 2])
    fixed = set()
    for row in st["lines"]:
        if not row.get("single") or not row["cur"]:
            continue
        e = edges.get(row["page"])
        if not e or e[1] - e[0] < 4:
            continue
        hits = []
        for key in {re.sub(r"\s+", "", row.get("own") or "")[:4], re.sub(r"\s+", "", row["label"])[:4]} - {""}:
            hits = [v[0][1] for v in (_value_ends(l) for l in norm(layout.get(row["page"], "")).splitlines()
                                      if key in re.sub(r"\s+", "", l)) if len(v) == 1 and v[0][0] == row["cur"]]
            if len(hits) == 1:
                break
        if len(hits) != 1:
            continue
        dc, dp, half = abs(hits[0] - e[0]), abs(hits[0] - e[1]), (e[1] - e[0]) / 2.0
        if dc < dp and dc < half:
            row["prior"] = "—"
        elif dp < dc and dp < half:
            row["cur"], row["prior"] = "—", row["cur"]
        else:
            continue
        fixed.add(row.pop("single"))
    st["bad"] = [b for b in st["bad"] if b not in fixed]
    for row in st["lines"]:
        row.pop("own", None)


def parse_document(pages, layout=None):
    """整份报告 → {kind: statement}。同类表有多块时（例如年报里的五年摘要）取第一块合格的（至少 6 行两列数）。
    layout：给定页码列表、返回这些页 layout 模式文字的函数（用来给只有一个数的行定列）。"""
    cleaned = clean_pages(pages)
    found = {}
    for kind, lines in find_blocks(cleaned):
        st = parse_block(lines)
        if layout and any(r.get("single") for r in st["lines"]):
            try:
                resolve_columns(st, layout(sorted({p for p, _ in lines})))
            except Exception:    # noqa: BLE001 layout 读不出来就按原样（这些行算读不出）
                pass
        for r in st["lines"]:
            r.pop("single", None)
            r.pop("own", None)
        good = [r for r in st["lines"] if r["prior"] != ""]
        if len(good) < 6 or len(st["bad"]) > max(3, len(good) // 3):
            continue
        st["pages"] = sorted({p for p, _ in lines})
        st["kind"] = kind
        st["unit_text"] = "\n".join(st["header"] + st["raw"][:12])
        st["eps_cents"] = _eps_cents(st)
        old = found.get(kind)
        if old is None or (detect_unit(st)[0] is not None and detect_unit(old)[0] is None):
            found[kind] = st              # 同类表有几块时，取第一块表头写明单位的
    if "income" not in found and "oci" in found:
        found["income"] = found["oci"]      # 只有一张“综合收益表”（利润表在里面）
    found.pop("oci", None)
    return found


def line_value(line, col):
    v = parse_value(line["cur" if col == 0 else "prior"])
    return v


def signature(st):
    return _sha("\n".join("%s|%s" % (l["label"], l["section"]) for l in st["lines"]))[:16]


# ---------------------------------------------------------------- 单位
_CUR_ZH = {"人民币": "CNY", "美元": "USD", "港元": "HKD", "港币": "HKD"}


def unit_of(text):
    """金额单位原文 → (换算成百万的系数, 币种)；认不出返回 (None, None)。"""
    t = norm(text)
    m = re.search(r"单位\s*[:：]?\s*(人民币|美元|港元|港币)?\s*(百万元|万元|千元|元)", t) or \
        re.search(r"(人民币|美元|港元|港币)\s*(百万元|万元|千元|元)(?![一-鿿])", t)      # 审计报告式的表头：“2026年6月30日 人民币元”
    if m:
        cur = _CUR_ZH.get(m.group(1) or "")
        m2 = re.search(r"币种\s*[:：]?\s*(人民币|美元|港元|港币)", t)
        if m2:
            cur = _CUR_ZH[m2.group(1)]
        return {"元": 1e-6, "千元": 1e-3, "万元": 1e-2, "百万元": 1.0}[m.group(2)], cur
    cur_map = (("rmb", "CNY"), ("renminbi", "CNY"), ("us$", "USD"), ("usd", "USD"), ("us dollar", "USD"), ("united states dollar", "USD"),
               ("hk$", "HKD"), ("hkd", "HKD"), ("hong kong dollar", "HKD"))
    low = t.lower()

    def cur_in(s):
        for k, c in cur_map:
            if k in s:
                return c
        return None
    m = re.search(r"(rmb|renminbi|us\$|usd|hk\$|hkd)\s*'\s*000\b", low)
    if m:
        return 1e-3, cur_in(m.group(1))
    m = re.search(r"(rmb|renminbi|us\$|usd|hk\$|hkd)\s*(?:in\s+)?(million|thousand)s?\b", low)
    if m:
        return (1.0 if m.group(2) == "million" else 1e-3), cur_in(m.group(1))
    m = re.search(r"in (thousands|millions) of (renminbi|rmb|us dollars|united states dollars|hong kong dollars|hk\$|us\$)", low)
    if m:
        return (1.0 if m.group(1) == "millions" else 1e-3), cur_in(m.group(2))
    return None, None


def detect_unit(st):
    return unit_of(st.get("unit_text") or "\n".join(st["header"] + st.get("raw", [])[:12]))


def _eps_cents(st):
    """每股收益那几行（及上面几行表头）写的是不是 cents（分）。"""
    idx = [l["raw"] for l in st["lines"] if re.search(r"每股|per share|\beps\b|\bbasic\b|\bdiluted\b", (l["label"] + " " + l["section"]).lower())]
    if not idx:
        return False
    return bool(re.search(r"\bcents?\b", " ".join(st["raw"][max(0, min(idx) - 8): max(idx) + 1]).lower()))


def eps_scale(st, ids):
    """每股收益的单位：原文写 cents（分）时是 0.01 元。"""
    return 0.01 if ids and st.get("eps_cents") else 1.0


# ---------------------------------------------------------------- 对照：A 股统一格式
def cas_core(label):
    """A 股行名的核心部分：去掉括号说明、序号和“其中/加/减”：'1.归属于母公司股东的净利润(净亏损以"-"号填列)' → '归属于母公司股东的净利润'。"""
    s = norm(label).replace(" ", "")
    s = re.sub(r"\([^()]*\)", "", s)
    s = re.sub(r"\([^()]*\)", "", s)
    s = re.sub(r"\([^()]*$", "", s)          # 括号说明折到下一行、没有收尾的：'资产处置收益(损失以"-"'
    while True:
        t = re.sub(r"^(?:[一二三四五六七八九十]+[、.]|\(?[一二三四五六七八九十]+\)|\d+[.、]|\(\d+\)|其中:|加:|减:)", "", s)
        if t == s:
            return s
        s = t


CAS_RULES = {
    "income": {
        "revenue": ["营业收入", "营业总收入"], "cost_of_sales": ["营业成本"], "rnd": ["研发费用"], "sga": ("sum", ["销售费用", "管理费用"]),
        "operating_income": ["营业利润"], "pretax_income": ["利润总额"], "income_tax": ["所得税费用"], "net_income": ["净利润"],
        "net_income_parent": [re.compile(r"^归属于母公司(?:股东|所有者|普通股股东)的净利润$")], "eps_diluted": ["稀释每股收益"],
    },
    "balance": {
        "cash": ["货币资金"], "short_term_investments": ["交易性金融资产"], "receivables": ["应收账款"], "inventories": ["存货"],
        "total_current_assets": ["流动资产合计"], "total_assets": ["资产总计"], "total_current_liabilities": ["流动负债合计"],
        "total_debt": ("sum0", ["短期借款", "长期借款", "应付债券"]), "total_liabilities": ["负债合计"],
        "total_equity": [re.compile(r"^(?:所有者|股东)权益合计$")], "equity_parent": [re.compile(r"^归属于母公司(?:所有者|股东)权益合计$")],
    },
    "cashflow": {"cfo": ["经营活动产生的现金流量净额"], "capex": ["购建固定资产、无形资产和其他长期资产支付的现金"]},
}


def _find(cores, key, before=None):
    for i, c in enumerate(cores):
        if before is not None and i >= before:
            break
        if (key.match(c) if hasattr(key, "match") else c == key):
            return i
    return None


def cas_mapping(stmts):
    """A 股按统一格式的标准行名对照；返回 {kind: {field: [行号]}}（"sum0" 类科目一行都没有时记 "zero"）。"""
    out = {}
    for kind, st in stmts.items():
        cores = [cas_core(l["label"]) for l in st["lines"]]
        m = {}
        for f, rule in CAS_RULES.get(kind, {}).items():
            if isinstance(rule, tuple):
                ids = [i for i in (_find(cores, k) for k in rule[1]) if i is not None]
                if ids or rule[0] == "sum0":
                    m[f] = ids if ids else "zero"
                continue
            before = _find(cores, "流动资产合计") if f == "short_term_investments" else None
            for key in rule:
                i = _find(cores, key, before)
                if i is not None:
                    m[f] = [i]
                    break
        out[kind] = m
    return out


# ---------------------------------------------------------------- 对照：GPT（港股；A 股认不全时）
FIELD_HELP = {
    "revenue": "营业收入 / Revenue（A 股营业总收入与营业收入相同时用营业收入；不含其他收入、其他收益、利息收入）",
    "cost_of_sales": "营业成本 / Cost of sales / Cost of revenues",
    "gross_profit": "报表上印出的毛利行 / Gross profit（没有就空）",
    "rnd": "研发费用 / Research and development expenses",
    "sga": "销售费用和管理费用（Selling and marketing / Distribution，Administrative / General and administrative），几行都给",
    "operating_income": "报表上印出的营业利润 / Operating profit (loss) 行（没有就空，不要用别的行代替）",
    "pretax_income": "利润总额 / Profit (loss) before tax",
    "income_tax": "所得税费用 / Income tax expense (credit)",
    "net_income": "净利润 / Profit (loss) for the period/year（含少数股东）",
    "net_income_parent": "归属于母公司股东的净利润 / Profit attributable to owners (equity holders) of the Company（只取利润的归属，不取综合收益的归属；没有归属拆分就空）",
    "eps_diluted": "稀释每股收益 / Diluted earnings (loss) per share",
    "cash": "货币资金 / Cash and cash equivalents / Bank balances and cash（不含受限资金、定期存款单列的行）",
    "short_term_investments": "流动资产里的短期投资：交易性金融资产、理财产品、三个月以上定期存款（Time / Bank / Term deposits）、以公允价值计量且变动计入损益的流动金融资产",
    "receivables": "应收账款 / Trade receivables（应收账款和应收票据合在一行的也算）；不含其他应收款、预付款",
    "inventories": "存货 / Inventories",
    "total_current_assets": "流动资产合计 / Total current assets（港股常是没有名称的小计行）",
    "total_assets": "资产总计 / Total assets；报表没有这一行时，给非流动资产小计和流动资产小计两行（程序相加）",
    "total_current_liabilities": "流动负债合计 / Total current liabilities（或没有名称的小计行）",
    "total_debt": "有息借款：短期借款、长期借款、应付债券、可转换债券、Borrowings / Bank loans / Bonds / Notes（流动和非流动各行都给）；不含租赁负债、一年内到期的非流动负债",
    "total_liabilities": "负债合计 / Total liabilities；没有这一行时，给流动负债小计和非流动负债小计两行（程序相加）",
    "total_equity": "所有者权益合计 / Total equity（含少数股东权益）",
    "equity_parent": "归属于母公司所有者权益合计 / Equity attributable to owners of the Company（可以是没有名称的小计行；没有就空）",
    "cfo": "经营活动产生的现金流量净额 / Net cash from (used in) operating activities",
    "capex": "购建固定资产、无形资产和其他长期资产支付的现金 / Purchase(s) of property, plant and equipment 以及无形资产等长期资产的购置款（几行都给）",
    "dividends_paid": "单独列出的支付股利 / Dividends paid（A 股“分配股利、利润或偿付利息支付的现金”混有利息，不要用）",
    "buybacks": "回购本公司股份支付的现金 / Repurchase of shares（含为股份奖励计划购买本公司股份）",
}

GPT_INSTRUCTIONS = (
    "你在帮一个全自动的财务数据流程读上市公司定期报告里的合并报表。程序已经从 PDF 文字里把每张表逐行抽了出来："
    "每行是 [行号, 行名, 所在小节（和上一行相同时为空）, 本期数, 上期数]，数字是原文照抄（括号表示负数，“–/—”表示零）；"
    "行名以“[小计]”开头的是原文里没有名称的小计行。"
    "你的任务只有一个：判断统一格式的每个科目对应哪一行（或哪几行，程序会把它们相加），只给行号。"
    "规则：只能用给出的行号；报表上没有对应的行就给空数组，不要用相近的行凑；不要计算任何数字。"
    "unit 逐字抄写表头里的金额单位（例如 RMB'000、RMB million、US$'000、单位：元）；currency 是金额币种；"
    "eps_unit：每股收益以元计填 yuan，以分（cents）计填 cents。"
    "如果给了 previous（同一公司上一份报告的对照，行名列表），同样含义的行请选得一致。"
    "如果给了 feedback，说明上一次的回答没通过程序核对，请改正。只输出 JSON。科目定义：")


def _mapping_schema():
    def obj(fields):
        return {"type": "object", "additionalProperties": False, "required": list(fields),
                "properties": {f: {"type": "array", "items": {"type": "integer"}} for f in fields}}
    return {"type": "object", "additionalProperties": False,
            "required": ["unit", "currency", "eps_unit", "income", "balance", "cashflow"],
            "properties": {"unit": {"type": "string"}, "currency": {"type": "string", "enum": ["CNY", "USD", "HKD", "EUR"]},
                           "eps_unit": {"type": "string", "enum": ["yuan", "cents"]},
                           "income": obj(INCOME_FIELDS), "balance": obj(BALANCE_FIELDS), "cashflow": obj(CASHFLOW_FIELDS)}}


def gpt_mapping(gpt, company, doc, stmts, previous=None, feedback=None):
    """请 GPT 给出对照（只给行号）。返回 GPT 的原始回答（dict），由 verify_mapping 核对。"""
    payload = {"company": "%s %s" % (company.get("name_zh", ""), company.get("name_en", "")), "document": doc.get("title"),
               "statements": {k: {"header": st["header"][:10],
                                  "lines": [[i, l["label"], l["section"] if i == 0 or l["section"] != st["lines"][i - 1]["section"] else "",
                                             l["cur"], l["prior"]] for i, l in enumerate(st["lines"])]}
                              for k, st in stmts.items()}}
    if previous:
        payload["previous"] = previous
    if feedback:
        payload["feedback"] = feedback[:20]
    instr = GPT_INSTRUCTIONS + "；".join("%s＝%s" % (f, FIELD_HELP[f]) for k in KINDS for f in FIELDS[k]) + "。"
    r = gpt.respond(instr, json.dumps(payload, ensure_ascii=False), schema=_mapping_schema(), max_tokens=6000)
    return r.get("data") or {}


# 关键词核对：行名（连同所在小节）必须有“要有”的词，不能有“不能有”的词
GUARD = {
    "revenue": (r"收入|revenue|turnover|sales", r"其他收入|other income|成本|cost|毛利|gross|费用|expense|利息收入|interest income|finance income"),
    "cost_of_sales": (r"营业成本|cost of (?:sales|revenues?|goods|services)", r"营业总成本"),
    "gross_profit": (r"毛利|gross (?:profit|loss)", None),
    "rnd": (r"研发|研究|research|r&d|development", None),
    "sga": (r"销售|管理|selling|marketing|distribution|administrative|general", r"研发|research|财务|finance|cost of"),
    "operating_income": (r"营业利润|营业亏损|operating \(?(?:profit|loss|income)|(?:profit|loss) from operations", r"before|税前"),
    "pretax_income": (r"利润总额|亏损总额|before (?:income )?tax", None),
    "income_tax": (r"所得税|income tax|taxation|tax (?:expense|credit)", r"before|税前|利润总额"),
    "net_income": (r"净利润|净亏损|(?:profit|loss)\)?\s*for the (?:period|year)|net (?:profit|income|loss)",
                   r"归属|attributable|少数|non-controlling|综合收益|comprehensive|持续经营|continuing|before"),
    "net_income_parent": (r"归属于(?:母公司|上市公司)|owners|equity holders|shareholders of the|parent", r"综合收益|comprehensive|少数|non-controlling"),
    "eps_diluted": (r"稀释|diluted", None),
    "cash": (r"货币资金|cash|bank balances", r"受限|restricted|pledged|定期|time deposit|term deposit"),
    "short_term_investments": (r"交易性金融资产|理财|定期存款|time deposit|bank deposit|term deposit|fair value through profit|fvtpl|"
                               r"short-term investment|financial assets|结构性存款|investments?", r"non-current|非流动"),
    "receivables": (r"应收账款|receivable", r"^其他应收|^other receivables|prepayment|预付|finance lease"),
    "inventories": (r"存货|inventor", None),
    "total_current_assets": (r"流动资产合计|current assets", r"non-current|非流动|net current|less current"),
    "total_assets": (r"资产总计|total assets|current assets", r"less current|net current|net assets"),
    "total_current_liabilities": (r"流动负债合计|current liabilities", r"non-current|非流动|net current|less current"),
    "total_debt": (r"借款|债券|borrowing|loan|bond|notes|debenture|convertible", r"lease|租赁|应付票据|bills payable|interest payable|应付利息|receivable|应收"),
    "total_liabilities": (r"负债合计|total liabilities|current liabilities", r"assets|权益|equity|net current"),
    "total_equity": (r"权益合计|total equity|net assets|权益总计", r"归属|attributable|owners of|holders of|liabilities and|负债和"),
    "equity_parent": (r"归属于母公司|owners of|equity holders of|attributable to (?:owners|equity holders|shareholders)", r"少数|non-controlling"),
    "cfo": (r"经营活动|operating activities", r"before|working capital"),
    "capex": (r"购建|固定资产|无形资产|长期资产|property|plant|equipment|intangible|capital expenditure|construction in progress|long-term assets",
              r"处置|disposal|proceeds|收回|sale of"),
    "dividends_paid": (r"股利|股息|dividend", r"利息|interest|received|收到"),
    "buybacks": (r"回购|repurchase|buy-?back|purchase of (?:own |the company's |treasury )?shares|shares (?:held|purchased) (?:for|under)|treasury shares",
                 r"出售|发行|proceeds"),
}


def guard_problem(field, line):
    text = ("%s | %s" % (line["label"], line["section"])).lower()
    need, ban = GUARD[field]
    if not re.search(need, text):
        return "%s 对到的行“%s”行名不符合" % (field, line["label"][:40])
    if ban and re.search(ban, line["label"].lower()):
        return "%s 对到的行“%s”行名不符合" % (field, line["label"][:40])
    sec = (line["section"] or "").lower()
    if field == "net_income_parent" and re.search(r"综合收益|comprehensive", sec) and re.search(r"归属|attributable", sec):
        return "%s 对到的是综合收益的归属行“%s”" % (field, line["label"][:40])
    return None


def verify_mapping(stmts, ans, header_text):
    """核对 GPT 的对照；返回 (mapping, unit, problems)。mapping：{kind: {field: [行号]}}。"""
    probs, mapping = [], {}
    if not isinstance(ans, dict):
        return {}, None, ["GPT 没有给出对照"]
    for kind in KINDS:
        st = stmts.get(kind)
        m = (ans.get(kind) or {}) if isinstance(ans.get(kind), dict) else {}
        mapping[kind] = {}
        if not st:
            continue
        used = {}
        for f in FIELDS[kind]:
            ids = m.get(f) or []
            if not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
                probs.append("%s 的行号格式不对" % f)
                continue
            if len(set(ids)) != len(ids) or any(i < 0 or i >= len(st["lines"]) for i in ids):
                probs.append("%s 的行号不存在：%s" % (f, ids))
                continue
            if f not in ("total_assets", "total_liabilities", "sga", "total_debt", "capex") and len(ids) > 1:
                probs.append("%s 只能对一行：%s" % (f, ids))
                continue
            bad = [guard_problem(f, st["lines"][i]) for i in ids]
            bad = [b for b in bad if b]
            if bad:
                probs += bad
                continue
            for i in ids:
                if i in used and not {f, used[i]} & {"total_assets", "total_liabilities", "total_current_assets", "total_current_liabilities"}:
                    probs.append("第 %d 行同时对到了 %s 和 %s" % (i, used[i], f))
                used.setdefault(i, f)
            if ids:
                mapping[kind][f] = ids
    unit = (ans.get("unit") or "").strip()
    scale, cur = unit_of(unit)
    if not unit or norm(unit).lower().replace(" ", "") not in norm(header_text).lower().replace(" ", ""):
        probs.append("GPT 给的单位“%s”不在表头原文里" % unit)
    elif scale is None:
        probs.append("认不出 GPT 给的单位“%s”" % unit)
    return mapping, {"text": unit, "scale": scale, "currency": cur or ans.get("currency"),
                     "eps_unit": ans.get("eps_unit")}, probs


# ---------------------------------------------------------------- 数值与核对
def _tol(n_terms, unit):
    """容差：每个四舍五入的项半个最小单位（unit 已换算成百万）。"""
    return 0.5 * unit * (n_terms + 1) + 1e-9


def _step(st, ids=None):
    """报表数字的最小单位（原文的小数位）。"""
    toks = [st["lines"][i]["cur"] for i in (ids if ids is not None else range(len(st["lines"])))]
    d = max([decimals(t) for t in toks if t] or [0])
    return 10 ** (-d)


def compute_values(kind, st, m, scale, style, eps_mult=1.0):
    """按对照算出本期、上期的统一格式数（百万；费用、资本开支记正数）。返回 ({"cur":{}, "prior":{}}, problems)。"""
    out, probs = {"cur": {}, "prior": {}}, []
    for col, name in ((0, "cur"), (1, "prior")):
        for f in FIELDS[kind]:
            ids = m.get(f)
            if ids == "zero":
                out[name][f] = 0.0
                continue
            if not ids:
                out[name][f] = None
                continue
            vals = [line_value(st["lines"][i], col) for i in ids]
            if any(v is None for v in vals):
                probs.append("%s 对到的行没有读到%s的数" % (f, "本期" if col == 0 else "上期"))
                out[name][f] = None
                continue
            s = sum(vals)
            if f == "eps_diluted":
                v = round(s * eps_mult, 6)
            else:
                v = s * scale
                if f in EXPENSE and style == "ifrs":
                    v = -v
            out[name][f] = v
    return out, probs


def _get(st, cores, key):
    i = _find(cores, key)
    return None if i is None else i


def check_income(st, m, vals, scale, style):
    probs = []
    step = _step(st) * scale
    lines = st["lines"]
    for name in ("cur", "prior"):
        v = vals[name]
        col = 0 if name == "cur" else 1
        tag = "本期" if name == "cur" else "上期"
        for f in REQUIRED["income"]:
            if v.get(f) is None:
                probs.append("利润表缺少 %s（%s）" % (f, tag))
        if v.get("revenue") is not None and v["revenue"] < -step:
            probs.append("营收为负（%s）" % tag)
        for f in ("cost_of_sales", "rnd", "sga"):
            if v.get(f) is not None and v[f] < -step:
                probs.append("%s 为负（%s，可能是正负号或对照错了）" % (f, tag))
        if None not in (v.get("revenue"), v.get("cost_of_sales"), v.get("gross_profit")):
            if abs(v["revenue"] - v["cost_of_sales"] - v["gross_profit"]) > _tol(3, step):
                probs.append("毛利 ≠ 营收 − 营业成本（%s：%.3f − %.3f ≠ %.3f）" % (tag, v["revenue"], v["cost_of_sales"], v["gross_profit"]))
        # 所得税的正负号由“净利润 = 税前利润 − 所得税”确定（A 股费用记正数，港股报表里费用是负数）
        if m.get("income_tax") and None not in (v.get("pretax_income"), v.get("net_income")):
            raw = sum(line_value(lines[i], col) or 0 for i in m["income_tax"]) * scale
            first = raw if style == "cas" else -raw
            if abs(v["pretax_income"] - first - v["net_income"]) <= _tol(3, step):
                v["income_tax"] = first
            elif abs(v["pretax_income"] + first - v["net_income"]) <= _tol(3, step) and abs(first) > _tol(3, step):
                v["income_tax"] = -first
                probs.append("所得税的正负号与报表格式不一致（%s）" % tag)
            else:
                probs.append("净利润 ≠ 税前利润 − 所得税（%s：%.3f − %.3f ≠ %.3f）" % (tag, v["pretax_income"], first, v["net_income"]))
        elif m.get("income_tax") is None and None not in (v.get("pretax_income"), v.get("net_income")):
            if abs(v["pretax_income"] - v["net_income"]) > _tol(2, step):
                probs.append("没有对到所得税，但税前利润 ≠ 净利润（%s）" % tag)
        if v.get("gross_profit") is None and None not in (v.get("revenue"), v.get("cost_of_sales")):
            v["gross_profit"] = v["revenue"] - v["cost_of_sales"]       # 报表没印毛利：营收 − 营业成本
        probs += (cas_income_checks if style == "cas" else ifrs_income_checks)(st, m, col, scale, tag)
    return probs


_CAS_COSTS = ["营业成本", "税金及附加", "销售费用", "管理费用", "研发费用", "财务费用", "利息支出", "手续费及佣金支出", "退保金",
              "赔付支出净额", "提取保险责任准备金净额", "保单红利支出", "分保费用"]
_CAS_ADDS = ["其他收益", "投资收益", "净敞口套期收益", "公允价值变动收益", "信用减值损失", "资产减值损失", "资产处置收益", "汇兑收益"]


def _vals_by_core(st, col):
    out = {}
    for l in st["lines"]:
        c = cas_core(l["label"])
        if c not in out:
            out[c] = parse_value(l["cur"] if col == 0 else l["prior"])
    return out


def cas_income_checks(st, m, col, scale, tag):
    """A 股利润表按统一格式的结构勾稽。"""
    v = _vals_by_core(st, col)
    unit = _step(st)
    probs = []

    def near(a, b, n):
        return abs(a - b) <= _tol(n, unit)
    total_rev = v.get("营业总收入", v.get("营业收入"))
    costs = [v[k] for k in _CAS_COSTS if v.get(k) is not None]
    adds = [v[k] for k in _CAS_ADDS if v.get(k) is not None]
    if v.get("营业总成本") is not None and costs and not near(v["营业总成本"], sum(costs), len(costs)):
        probs.append("营业总成本 ≠ 各项成本费用之和（%s）" % tag)
    if None not in (total_rev, v.get("营业利润")):
        tc = v.get("营业总成本", sum(costs))
        if not near(total_rev - tc + sum(adds), v["营业利润"], len(adds) + 2):
            probs.append("营业利润 ≠ 营业总收入 − 营业总成本 + 其他收益、投资收益等（%s）" % tag)
    else:
        probs.append("利润表缺少营业总收入或营业利润，无法核对结构（%s）" % tag)
    if None not in (v.get("营业利润"), v.get("利润总额")):
        if not near(v["营业利润"] + (v.get("营业外收入") or 0) - (v.get("营业外支出") or 0), v["利润总额"], 3):
            probs.append("利润总额 ≠ 营业利润 + 营业外收入 − 营业外支出（%s）" % tag)
    if None not in (v.get("利润总额"), v.get("所得税费用"), v.get("净利润")):
        if not near(v["利润总额"] - v["所得税费用"], v["净利润"], 2):
            probs.append("净利润 ≠ 利润总额 − 所得税费用（%s）" % tag)
    parent = next((x for k, x in v.items() if re.match(r"^归属于母公司(?:股东|所有者|普通股股东)的净利润$", k)), None)
    if None not in (v.get("净利润"), parent, v.get("少数股东损益")):
        if not near(parent + v["少数股东损益"], v["净利润"], 2):
            probs.append("净利润 ≠ 归母净利润 + 少数股东损益（%s）" % tag)
    return probs


def ifrs_income_checks(st, m, col, scale, tag):
    """港股利润表：从营收那一行开始一行行加到税前利润（费用是负数）。等于当前累计数、或等于紧挨着的几行之和的行是小计，不重复加。"""
    if not m.get("revenue") or not m.get("pretax_income"):
        return []
    lines = st["lines"]
    start, end = min(m["revenue"]), m["pretax_income"][0]
    if end <= start:
        return ["税前利润行在营收行之前，无法核对（%s）" % tag]
    unit = _step(st)
    total = line_value(lines[start], col)
    if total is None:
        return ["营收行没有数（%s）" % tag]
    seg, n = [], 1
    for i in range(start + 1, end):
        x = line_value(lines[i], col)
        if x is None:
            return ["第 %d 行“%s”没有读到数（%s）" % (i, lines[i]["label"][:30], tag)]
        if abs(x - total) <= _tol(n, unit) and i != start + 1:
            seg = []
            continue
        s, partial = 0.0, False
        for k in range(1, len(seg) + 1):
            s += seg[-k]
            if k >= 2 and abs(x - s) <= _tol(k, unit):
                partial = True
                break
        if partial:
            seg = []
            continue
        total += x
        seg.append(x)
        n += 1
    pbt = line_value(lines[end], col)
    if pbt is None or abs(total - pbt) > _tol(n, unit):
        return ["利润表从营收加到税前利润对不上（%s：逐行相加 %.0f，税前利润 %s）" % (tag, total, lines[end]["cur" if col == 0 else "prior"])]
    return []


def check_balance(st, m, vals, scale, style):
    probs = []
    step = _step(st) * scale
    for name in ("cur", "prior"):
        v = vals[name]
        tag = "期末" if name == "cur" else "上期末"
        for f in REQUIRED["balance"]:
            if v.get(f) is None:
                probs.append("资产负债表缺少 %s（%s）" % (f, tag))
        if None not in (v.get("total_assets"), v.get("total_liabilities"), v.get("total_equity")):
            if abs(v["total_assets"] - v["total_liabilities"] - v["total_equity"]) > _tol(3 + len(m.get("total_assets", [])), step):
                probs.append("资产 ≠ 负债 + 权益（%s：%.3f ≠ %.3f + %.3f）" % (tag, v["total_assets"], v["total_liabilities"], v["total_equity"]))
        for f, tot in (("total_current_assets", "total_assets"), ("total_current_liabilities", "total_liabilities"),
                       ("cash", "total_current_assets"), ("receivables", "total_current_assets"), ("inventories", "total_current_assets"),
                       ("short_term_investments", "total_current_assets")):
            if None not in (v.get(f), v.get(tot)) and v[f] > v[tot] + step:
                probs.append("%s 大于 %s（%s）" % (f, tot, tag))
        for f in ("cash", "receivables", "inventories", "total_current_assets", "total_assets", "total_debt"):
            if v.get(f) is not None and v[f] < -step:
                probs.append("%s 为负（%s）" % (f, tag))
        probs += (cas_balance_checks if style == "cas" else ifrs_balance_checks)(st, 0 if name == "cur" else 1, tag, v, scale)
    return probs


def cas_balance_checks(st, col, tag, v, scale):
    b = _vals_by_core(st, col)
    unit = _step(st)
    probs = []

    def chk(a, parts, what):
        if b.get(a) is not None and all(b.get(p) is not None for p in parts):
            if abs(b[a] - sum(b[p] for p in parts)) > _tol(len(parts) + 1, unit):
                probs.append("%s（%s）" % (what, tag))
    chk("资产总计", ["流动资产合计", "非流动资产合计"], "资产总计 ≠ 流动资产 + 非流动资产")
    chk("负债合计", ["流动负债合计", "非流动负债合计"], "负债合计 ≠ 流动负债 + 非流动负债")
    total_le = next((x for k, x in b.items() if re.match(r"^负债和(?:所有者|股东)权益总计$", k)), None)
    if total_le is not None and b.get("资产总计") is not None and abs(total_le - b["资产总计"]) > _tol(2, unit):
        probs.append("负债和所有者权益总计 ≠ 资产总计（%s）" % tag)
    te = next((x for k, x in b.items() if re.match(r"^(?:所有者|股东)权益合计$", k)), None)
    ep = next((x for k, x in b.items() if re.match(r"^归属于母公司(?:所有者|股东)权益合计$", k)), None)
    if None not in (te, ep, b.get("少数股东权益")) and abs(te - ep - b["少数股东权益"]) > _tol(3, unit):
        probs.append("所有者权益合计 ≠ 归属于母公司 + 少数股东权益（%s）" % tag)
    return probs


def _line_by(st, pattern, col):
    for l in st["lines"]:
        if re.search(pattern, l["label"].lower()):
            return parse_value(l["cur"] if col == 0 else l["prior"])
    return None


def ifrs_balance_checks(st, col, tag, v, scale):
    probs = []
    unit = _step(st) * scale
    nca = _line_by(st, r"^net current assets|^net current liabilities|^流动资产净值", col)
    if nca is not None and None not in (v.get("total_current_assets"), v.get("total_current_liabilities")):
        if abs(nca * scale - (v["total_current_assets"] - v["total_current_liabilities"])) > _tol(3, unit):
            probs.append("流动资产净值 ≠ 流动资产 − 流动负债（%s）" % tag)
    talcl = _line_by(st, r"^total assets less current liabilities", col)
    if talcl is not None and None not in (v.get("total_assets"), v.get("total_current_liabilities")):
        if abs(talcl * scale - (v["total_assets"] - v["total_current_liabilities"])) > _tol(4, unit):
            probs.append("总资产减流动负债 ≠ 资产 − 流动负债（%s）" % tag)
    na = _line_by(st, r"^net assets|^net liabilities", col)
    if na is not None and v.get("total_equity") is not None and abs(na * scale - v["total_equity"]) > _tol(2, unit):
        probs.append("资产净值 ≠ 权益合计（%s）" % tag)
    tle = _line_by(st, r"^total (?:equity and liabilities|liabilities and equity)", col)
    if tle is not None and v.get("total_assets") is not None and abs(tle * scale - v["total_assets"]) > _tol(3, unit):
        probs.append("权益和负债合计 ≠ 资产合计（%s）" % tag)
    return probs


def check_cashflow(st, m, vals, scale, style):
    probs = []
    for name in ("cur", "prior"):
        v = vals[name]
        col = 0 if name == "cur" else 1
        tag = "本期" if name == "cur" else "上期"
        if v.get("cfo") is None:
            probs.append("现金流量表缺少经营活动现金流量净额（%s）" % tag)
        for f in ("capex", "dividends_paid", "buybacks"):
            if v.get(f) is not None and v[f] < -_step(st) * scale:
                probs.append("%s 为负（%s，可能是正负号或对照错了）" % (f, tag))
        if style == "cas":
            c = _vals_by_core(st, col)
            unit = _step(st)
            for act in ("经营", "投资", "筹资"):
                a, b, net = c.get(act + "活动现金流入小计"), c.get(act + "活动现金流出小计"), c.get(act + "活动产生的现金流量净额")
                if None not in (a, b, net) and abs(a - b - net) > _tol(3, unit):
                    probs.append("%s活动现金流入 − 流出 ≠ 净额（%s）" % (act, tag))
            parts = [c.get(k) for k in ("经营活动产生的现金流量净额", "投资活动产生的现金流量净额", "筹资活动产生的现金流量净额")]
            fx = c.get("汇率变动对现金及现金等价物的影响") or 0
            inc = c.get("现金及现金等价物净增加额")
            if None not in parts and inc is not None and abs(sum(parts) + fx - inc) > _tol(5, unit):
                probs.append("现金及现金等价物净增加额 ≠ 三类活动净额 + 汇率影响（%s）" % tag)
        else:
            unit = _step(st)
            cfi = _line_by(st, r"^net cash.*investing activities", col)
            cff = _line_by(st, r"^net cash.*financing activities", col)
            cfo = None
            if m.get("cfo"):
                cfo = line_value(st["lines"][m["cfo"][0]], col)
            inc = _line_by(st, r"^net (?:\(?(?:increase|decrease)\)?(?:\s*/\s*\(?(?:increase|decrease)\)?)?|change) in cash", col)
            fx = _line_by(st, r"effects? of (?:foreign )?exchange rate", col) or 0
            if None not in (cfo, cfi, cff, inc):
                s = cfo + cfi + cff
                if abs(s - inc) > _tol(4, unit) and abs(s + fx - inc) > _tol(5, unit):
                    probs.append("现金及现金等价物净增加额 ≠ 三类活动净额（%s）" % tag)
    return probs


CHECKS = {"income": check_income, "balance": check_balance, "cashflow": check_cashflow}


# ---------------------------------------------------------------- 一份报告
# 报告里说明了重述的写法（只认明确的说法；“公司是否需追溯调整或重述以前年度会计数据 □是 否”这类固定问句不算，除非勾的是“是”）
RESTATE = re.compile(r"因会计政策变更[^。]{0,40}追溯调整|重要会计政策变更\s*√\s*适用|调整后\s*调整前|调整前\s*调整后|"
                     r"追溯调整或重述以前年度会计数据\s*(?:√\s*是|是\s*□\s*否)|同一控制下企业合并[^。]{0,20}(?:追溯调整|重述)|"
                     r"\(restated\)|\brestated\b|\bre-?presented\b|comparative (?:figures|amounts|information) (?:have|has) been "
                     r"(?:restated|reclassified|re-?presented)|certain comparative (?:figures|amounts)[^.]{0,80}reclassified", re.I)


def restatement_hint(pages):
    for p in pages:
        t = norm(p)
        m = RESTATE.search(t)
        if m:
            return re.sub(r"\s+", " ", t[max(0, m.start() - 60): m.end() + 60]).strip()
    return ""


def header_years(st, fy):
    """表头里本期年份是否在上期年份之前：True / False（上期在前，需要对调两列）/ None（看不出）。"""
    text = " ".join(st["header"])
    a, b = text.find(str(fy)), text.find(str(fy - 1))
    if a < 0 or b < 0:
        return None
    return a < b


def evaluate(doc, stmts, mapping, unit, style):
    """按对照算数并核对；返回 (values, problems, warnings)。"""
    probs, warns, values = [], [], {}
    scale = unit["scale"]
    for kind in KINDS:
        st = stmts.get(kind)
        if not st:
            continue
        m = mapping.get(kind) or {}
        missing = [f for f in REQUIRED[kind] if not m.get(f)]
        if missing:
            probs.append("%s没有对到 %s" % (KIND_ZH[kind], "、".join(missing)))
            continue
        order = header_years(st, doc["period_end_year"])
        if order is False:
            for l in st["lines"]:
                l["cur"], l["prior"] = l["prior"], l["cur"]
            warns.append("%s的上期列在前，已对调" % KIND_ZH[kind])
        mult = 1.0
        if kind == "income":
            mult = 0.01 if unit.get("eps_unit") == "cents" else eps_scale(st, m.get("eps_diluted"))
            if unit.get("eps_unit") == "cents" and eps_scale(st, m.get("eps_diluted")) != 0.01:
                probs.append("GPT 说每股收益以分计，但原文没有写 cents")
        vals, p = compute_values(kind, st, m, scale, style, mult)
        probs += ["%s：%s" % (KIND_ZH[kind], x) for x in p]
        probs += ["%s：%s" % (KIND_ZH[kind], x) for x in CHECKS[kind](st, m, vals, scale, style)]
        values[kind] = vals
        if st.get("bad"):
            warns.append("%s有 %d 行读不出两列数（例如“%s”）" % (KIND_ZH[kind], len(st["bad"]), st["bad"][0][:40]))
    return values, probs, warns


def mapping_labels(stmts, mapping):
    out = {}
    for kind, m in mapping.items():
        st = stmts.get(kind)
        if not st:
            continue
        out[kind] = {f: (ids if ids == "zero" else [st["lines"][i]["label"] for i in ids]) for f, ids in m.items()}
    return out


def doc_period_end(doc, fye):
    return span_end(doc["fiscal_year"], doc["months"], fye)


def process_doc(doc, company, ctx, http, records, style):
    """处理一份报告：下载 → 抽文字 → 找报表 → 拆行 → 对照 → 核对。返回 (record, 提醒)。
    record["status"]：ok / failed（数字核对不过）/ no_statements（找不到报表）。网络或 GPT 失败时抛异常，下次再试。"""
    fye = company.get("fiscal_year_end_month", 12)
    pages = pdf_pages(http, doc["url"])
    stmts = parse_document(pages, layout=lambda nums: pdf_layout(http, doc["url"], nums))
    pe = doc_period_end(doc, fye)
    rec = {"doc": doc["id"], "title": doc["title"], "url": doc["url"], "published": doc["published"], "kind": doc["kind"],
           "fiscal_year": doc["fiscal_year"], "months": doc["months"], "period_end": pe.isoformat(),
           "parse_version": PARSE_VERSION, "check_version": CHECK_VERSION, "processed": ctx.today.isoformat(),
           "style": style, "restatement_hint": restatement_hint(pages), "warnings": []}
    if not stmts.get("income") and not stmts.get("balance"):
        rec.update(status="no_statements", problems=["没有找到合并利润表和资产负债表（%d 页）" % len(pages)])
        return rec
    rec["statements"] = {k: {"pages": st["pages"], "header": st["header"], "unit_text": st["unit_text"], "eps_cents": st["eps_cents"],
                             "bad": st["bad"], "lines": [[l["label"], l["section"], l["cur"], l["prior"]] for l in st["lines"]]}
                         for k, st in stmts.items()}
    return finish_record(rec, company, ctx, records)


def load_statements(rec):
    out = {}
    for k, s in (rec.get("statements") or {}).items():
        out[k] = {"pages": s["pages"], "header": s["header"], "unit_text": s.get("unit_text", ""), "eps_cents": s.get("eps_cents", False),
                  "bad": s.get("bad", []), "kind": k, "lines": [{"label": a, "section": b, "cur": c, "prior": d} for a, b, c, d in s["lines"]]}
    return out


def _previous_mapping(records, rec):
    prev = [r for r in records.values() if r.get("status") == "ok" and r.get("mapping_labels") and r["doc"] != rec["doc"]]
    if not prev:
        return None
    prev.sort(key=lambda r: (r["kind"] == rec["kind"], r["months"] == rec["months"], r["published"]))
    return prev[-1]["mapping_labels"]


def _reuse(records, stmts, rec):
    """版式（每行的行名和小节）和以前某份报告完全一样时，沿用那份的对照（不用再问 GPT）。"""
    sigs = {k: signature(st) for k, st in stmts.items()}
    out = {}
    for k, sig in sigs.items():
        for r in sorted(records.values(), key=lambda r: r["published"], reverse=True):
            if r.get("status") == "ok" and r["doc"] != rec["doc"] and (r.get("signatures") or {}).get(k) == sig and k in (r.get("mapping") or {}):
                out[k] = (r["mapping"][k], r["doc"], r.get("unit"))
                break
    return sigs, out


def finish_record(rec, company, ctx, records):
    """对照 + 核对（报表行已在 rec 里）。对照的来源依次是：A 股统一格式对照表 → 版式和以前某份报告相同就沿用 → GPT
    （只把前两步没对上的表交给 GPT；回答不过核对时把问题告诉它再问一次）。需要 GPT 而 GPT 不可用时抛 NeedGPT。"""
    stmts = load_statements(rec)
    style = rec["style"]
    meta = {"period_end_year": int(rec["period_end"][:4])}
    scale, cur = None, None
    for st in stmts.values():
        scale, cur = detect_unit(st)
        if scale:
            break
    mapping, unit, how = {}, {"text": "", "scale": scale, "currency": cur, "eps_unit": None}, []
    need = list(stmts)
    if style == "cas":
        cm = cas_mapping(stmts)
        done = [k for k in stmts if all(cm.get(k, {}).get(f) for f in REQUIRED[k])]
        mapping.update({k: cm[k] for k in done})
        need = [k for k in need if k not in done]
        if done:
            how.append("A 股统一格式对照表")
    sigs, reused = _reuse(records, stmts, rec)
    rec["signatures"] = sigs
    used = set()
    for k in list(need):
        if k in reused:
            mapping[k] = reused[k][0]
            used.add(reused[k][1])
            need.remove(k)
            prev_unit = reused[k][2] or {}
            unit["text"] = unit["text"] or prev_unit.get("text", "")
            unit["eps_unit"] = unit["eps_unit"] or prev_unit.get("eps_unit")
            if not unit["scale"]:
                unit["scale"], unit["currency"] = prev_unit.get("scale"), prev_unit.get("currency")
    if used:
        how.append("沿用 %s 的对照（版式相同）" % "、".join(sorted(used)))
    if need:
        gpt = ctx.gpt
        if gpt is None or not ctx.gpt_ok("mapping"):
            raise NeedGPT("需要 GPT 对照报表行，但 GPT 不可用")
        sub = {k: stmts[k] for k in need}
        sub_header = "\n".join(st.get("unit_text") or "\n".join(st["header"]) for st in sub.values())
        prev, feedback, probs, got, u2 = _previous_mapping(records, rec), None, [], {}, {}
        for attempt in range(2):
            ans = gpt_mapping(gpt, company, rec, sub, previous=prev, feedback=feedback)
            got, u2, probs = verify_mapping(sub, ans, sub_header)
            if not probs and scale and u2.get("scale") and abs(u2["scale"] - scale) > 1e-12:
                probs.append("GPT 给的单位“%s”与表头识别的不一致" % u2["text"])
            if not probs:
                trial = {k: load_statements(json.loads(json.dumps({"statements": {k: rec["statements"][k]}})))[k] for k in need}
                probs = evaluate(meta, trial, got, dict(u2, scale=scale or u2.get("scale")), style)[1]
            if not probs:
                break
            feedback = probs
        if probs:
            rec.update(status="failed", problems=["GPT 的对照没通过核对（问了 2 次）：" + "；".join(probs[:8])],
                       mapping_source="；".join(how + ["GPT 对照"]), gpt_answer=mapping_labels(sub, got) if got else {})
            return rec
        mapping.update(got)
        unit.update(text=u2.get("text") or unit["text"], eps_unit=u2.get("eps_unit"))
        if not unit["scale"]:
            unit["scale"], unit["currency"] = u2.get("scale"), u2.get("currency")
        unit["currency"] = unit["currency"] or u2.get("currency")
        how.append("GPT 对照%s" % ("（第 2 次才通过核对）" if attempt else ""))
    rec.pop("gpt_answer", None)
    if not unit["scale"]:
        rec.update(status="failed", problems=["认不出金额单位"], mapping_source="；".join(how))
        return rec
    values, probs, warns = evaluate(meta, stmts, mapping, unit, style)
    currency = unit.get("currency") or ("CNY" if style == "cas" else company.get("currency"))
    if unit.get("currency") is None and style != "cas":
        warns.append("表头没写币种，按公司清单记为 %s" % currency)
    elif company.get("currency") and currency != company["currency"]:
        warns.append("报表币种 %s 与公司清单（%s）不同，按报表记" % (currency, company["currency"]))
    rec.update(mapping={k: v for k, v in mapping.items() if k in stmts}, mapping_labels=mapping_labels(stmts, mapping),
               mapping_source="；".join(how), unit={"text": unit.get("text") or "", "scale": unit["scale"], "currency": currency,
                                                   "eps_unit": unit.get("eps_unit")},
               step={k: _step(st) * unit["scale"] for k, st in stmts.items()}, values=values,
               warnings=warns, problems=probs, status="failed" if probs else "ok", check_version=CHECK_VERSION)
    return rec


class NeedGPT(RuntimeError):
    pass


# ---------------------------------------------------------------- 期间与统一格式行
def _obs(records, fye):
    """所有报告给出的观测值：{(kind, 期间键): [(record, 'cur'/'prior')]}。流量表的期间键是 (财年, 月数)，资产负债表是期末日期。"""
    out = {}
    for r in sorted(records.values(), key=lambda r: (r["published"], r["doc"])):
        if r.get("status") != "ok":
            continue
        fy, months = r["fiscal_year"], r["months"]
        for kind, vals in (r.get("values") or {}).items():
            for col in ("cur", "prior"):
                if kind == "balance":
                    key = r["period_end"] if col == "cur" else month_end(fy - 1, fye).isoformat()
                else:
                    key = (fy if col == "cur" else fy - 1, months)
                out.setdefault((kind, key), []).append((r, col))
    return out


def _primary(obs_list):
    cur = [x for x in obs_list if x[1] == "cur"]
    return (cur or obs_list)[0]


def consistency(records, fye, fresh=()):
    """同一期间在不同报告里的数要一致（上年同期、业绩公告与年报/中报）。返回 (problems, notes, warnings)：
    核心科目对不上、而后一份报告里没有重述说明 → problems（需要处理）；有重述说明的、或非核心科目（对照有判断成分）
    对不上 → notes（每次报告里列出）；其中涉及本次新处理的报告 → 另记一条提醒。"""
    probs, notes, warns = [], [], []
    for (kind, key), lst in _obs(records, fye).items():
        if len(lst) < 2:
            continue
        base, bcol = _primary(lst)
        bv = base["values"][kind][bcol]
        for r, col in lst:
            if r is base and col == bcol:
                continue
            v = r["values"][kind][col]
            tol = 0.5 * ((base.get("step") or {}).get(kind, 0) + (r.get("step") or {}).get(kind, 0)) + 1e-9
            for f in FIELDS[kind]:
                a, b = bv.get(f), v.get(f)
                if a is None or b is None:
                    continue
                t = 0.011 if f == "eps_diluted" else tol * (len(r.get("mapping", {}).get(kind, {}).get(f) or [1]) + 1)
                if abs(a - b) <= t:
                    continue
                where = "%s %s %s" % (KIND_ZH[kind], key if isinstance(key, str) else "%d 年 %d 个月" % key, f)
                msg = "%s：%s 写的是 %s，%s 写的是 %s" % (where, base["title"], _fmt(a), r["title"], _fmt(b))
                if f in CORE[kind] and not r.get("restatement_hint"):
                    probs.append(msg + "（报告里没有重述说明）")
                    continue
                notes.append(msg + ("（报告提到：…%s…）" % r["restatement_hint"][:80] if r.get("restatement_hint") else "（非核心科目）"))
                if r["doc"] in fresh:
                    warns.append(msg)
    return probs, notes, warns


def _fmt(x):
    return "%.2f" % x


def build_rows(records, company, market):
    """所有核对通过的报告 → 统一格式的行（income / balance / cashflow；收入构成本阶段不做）。"""
    fye = company.get("fiscal_year_end_month", 12)
    obs = _obs(records, fye)
    flows = {"income": {}, "cashflow": {}}
    bal = {}
    for (kind, key), lst in obs.items():
        if kind == "balance":
            bal[key] = _primary(lst)          # 资产负债表：用最早发布的那份（本期列优先）
    for kind in ("income", "cashflow"):
        years = sorted({k[0] for (kd, k) in obs if kd == kind})
        for fy in years:
            orig = {k[1]: next((x for x in lst if x[1] == "cur"), None) for (kd, k), lst in obs.items() if kd == kind and k[0] == fy}
            comp = {k[1]: next((x for x in lst if x[1] == "prior"), None) for (kd, k), lst in obs.items() if kd == kind and k[0] == fy}
            orig = {m: v for m, v in orig.items() if v}
            comp = {m: v for m, v in comp.items() if v}
            # 利润表、现金流量表：一个财年的几个期间要用同一批报告的数（推算单季才一致）。次年报告里的上年同期数（可能重述过）
            # 覆盖了这一年所有期间时用它们（与次年同比口径一致，和美股适配器取“最新申报”一样），否则用当年报告的数。
            first, other = (comp, orig) if comp and set(comp) >= set(orig) else (orig, comp)
            for m, v in other.items():
                flows[kind][(fy, m)] = v        # 只在另一批报告里有的期间也补上
            for m, v in first.items():
                flows[kind][(fy, m)] = v
    out = {"income": [], "balance": [], "cashflow": [], "segments": []}
    for kind in ("income", "cashflow"):
        by_year = {}
        for (fy, months), v in flows[kind].items():
            by_year.setdefault(fy, {})[months] = v
        for fy, spans in sorted(by_year.items()):
            def row(fp, months, start, end, vals, basis, source):
                r = {"fiscal_year": str(fy), "fiscal_period": fp, "period_start": start.isoformat(), "period_end": end.isoformat(),
                     "months": months, "currency": cur_of(spans), "unit": "millions", "basis": basis, "source": source}
                r.update(_unified(kind, vals))
                out[kind].append(r)

            def get(months):
                if months not in spans:
                    return None
                rec, col = spans[months]
                return rec["values"][kind][col], rec

            def cur_of(sp):
                rec = next(iter(sp.values()))[0]
                return (rec.get("unit") or {}).get("currency") or company.get("currency")
            s = fy_start(fy, fye)
            names = {3: "Q1", 6: "H1", 12: "FY"}
            for months, fp in names.items():
                g = get(months)
                if g:
                    row(fp, months, s, span_end(fy, months, fye), g[0], "reported", _src(g[1], months, fy))
            for a, b, fp in ((6, 3, "Q2"), (9, 6, "Q3"), (12, 9, "Q4"), (12, 6, "H2")):
                ga, gb = get(a), get(b)
                if ga and gb:
                    vals = {f: (None if f in NOT_ADDITIVE or ga[0].get(f) is None or gb[0].get(f) is None else ga[0][f] - gb[0][f])
                            for f in FIELDS[kind]}
                    row(fp, a - b, span_end(fy, b, fye) + dt.timedelta(days=1), span_end(fy, a, fye), vals, "derived",
                        "%s − %s" % (_src(ga[1], a, fy), _src(gb[1], b, fy)))
    for pe, (r, col) in sorted(bal.items()):
        d = dt.date.fromisoformat(pe)
        fy = d.year if d.month <= fye or fye == 12 else d.year + 1
        months = ((d.month - fye - 1) % 12) + 1
        fp = {3: "Q1", 6: "Q2", 9: "Q3", 12: "Q4"}.get(months, "FY") if market == "A" else {6: "H1", 12: "FY"}.get(months, "Q%d" % (months // 3))
        v = r["values"]["balance"][col]
        row = {"fiscal_year": str(fy), "fiscal_period": fp, "period_end": pe, "currency": (r.get("unit") or {}).get("currency") or company.get("currency"),
               "unit": "millions", "source": r["url"] if col == "cur" else "%s（上年末比较数）" % r["url"]}
        row.update(_unified("balance", v))
        out["balance"].append(row)
    return out


def _src(rec, months, fy):
    return rec["url"] if rec["fiscal_year"] == fy else "%s（上年同期比较数）" % rec["url"]


def _r(x):
    return None if x is None else round(x, 6)


def _unified(kind, v):
    if kind == "income":
        out = {f: _r(v.get(f)) for f in INCOME_FIELDS}
        out.update(acquired_iprd=None, shares_diluted=None)
        if out.get("gross_profit") is None and None not in (out.get("revenue"), out.get("cost_of_sales")):
            out["gross_profit"] = _r(out["revenue"] - out["cost_of_sales"])
        return out
    if kind == "balance":
        out = {f: _r(v.get(f)) for f in BALANCE_FIELDS}
        out["cash_and_sti"] = _r(out["cash"] + (out.get("short_term_investments") or 0)) if out.get("cash") is not None else None
        return out
    out = {f: _r(v.get(f)) for f in CASHFLOW_FIELDS}
    out["fcf"] = _r(out["cfo"] - out["capex"]) if None not in (out.get("cfo"), out.get("capex")) else None
    return out


# ---------------------------------------------------------------- 适配器入口（hkex.py / cninfo.py 调用）
def reports_dir(ctx, cid):
    return os.path.join(ctx.state_dir(cid), REPORTS)


def load_records(ctx, cid):
    d = reports_dir(ctx, cid)
    out = {}
    if os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            if name.endswith(".json"):
                r = _read_json(os.path.join(d, name))
                if r and r.get("doc"):
                    out[r["doc"]] = r
    return out


def save_record(ctx, cid, rec):
    _write_json(os.path.join(reports_dir(ctx, cid), rec["doc"] + ".json"), rec)


def run_update(company, ctx, source):
    """source：hkex 或 cninfo 模块（提供 SOURCE_ZH、STYLE、MARKET、make_http(ctx)、discover(company, ctx, http, state, res)）。"""
    res = Result(company["id"])
    cid = company["id"]
    state = ctx.load_state(cid)
    records = load_records(ctx, cid)
    http = source.make_http(ctx)
    try:
        docs = source.discover(company, ctx, http, state, res)
    except FetchError as e:
        res.warnings.append("无法访问%s：%s（本次不更新）" % (source.SOURCE_ZH, e))
        docs = None
    done, pending, t0 = [], [], time.time()
    if _RUN["start"] is None:
        _RUN["start"] = t0
    # 规则改了（CHECK_VERSION）：用存下的报表行重新核对，不用下载
    for rec in list(records.values()):
        if rec.get("statements") and rec.get("check_version") != CHECK_VERSION and rec.get("parse_version") == PARSE_VERSION:
            try:
                records[rec["doc"]] = finish_record(rec, company, ctx, {k: v for k, v in records.items() if k != rec["doc"]})
                save_record(ctx, cid, records[rec["doc"]])
            except NeedGPT as e:
                pending.append("%s：%s" % (rec["title"], e))
    if docs is not None:
        todo = []
        for d in docs:
            old = records.get(d["id"])
            if old and old.get("url") == d["url"] and old.get("parse_version") == PARSE_VERSION:
                if old.get("status") != "failed" or old.get("processed", "") >= (ctx.today - dt.timedelta(days=30)).isoformat():
                    continue
            todo.append(d)
        todo.sort(key=lambda d: (d["published"], d["id"]))
        for d in todo:
            if len(done) >= MAX_DOCS_PER_RUN or time.time() - t0 > TIME_BUDGET or time.time() - _RUN["start"] > RUN_BUDGET:
                pending.append("还有 %d 份报告下次处理" % (len(todo) - len(done)))
                break
            try:
                rec = process_doc(d, company, ctx, http, records, source.STYLE)
            except NeedGPT as e:
                pending.append("%s：%s" % (d["title"], e))
                continue
            except FetchError as e:
                res.warnings.append("下载或读取 %s 失败：%s（下次再试）" % (d["title"], e))
                continue
            records[d["id"]] = rec
            save_record(ctx, cid, rec)
            done.append(rec)
        state.update(source=source.SOURCE_ZH, last_check=ctx.today.isoformat(), documents=len(docs))
    ctx.save_state(cid, state)
    if pending:
        res.warnings.append("暂缓：" + "；".join(pending[:6]))
    return finish(company, ctx, records, res, done, source)


def finish(company, ctx, records, res, done, source):
    """核对每份报告的结果和跨报告一致性，生成统一格式的行。"""
    fye = company.get("fiscal_year_end_month", 12)
    for r in sorted(records.values(), key=lambda r: r["published"]):
        if r.get("status") == "failed":
            res.check("%s 核对" % r["title"], False, "；".join(r.get("problems", [])[:6]) + "（%s）" % r["url"])
        elif r.get("status") == "no_statements":
            res.warnings.append("%s：%s（%s）" % (r["title"], "；".join(r.get("problems", [])), r["url"]))
    for r in done:
        if r.get("status") == "ok":
            res.check("%s 核对" % r["title"], True, r.get("mapping_source", ""))
        for w in r.get("warnings", []):
            res.warnings.append("%s：%s" % (r["title"], w))
    probs, notes, warns = consistency(records, fye, {r["doc"] for r in done})
    res.check("同一期间在不同报告里的数一致", not probs, "；".join(probs[:6]))
    res.warnings += ["与以前的报告不同：" + w for w in warns[:6]]
    if notes:
        res.notes.append(("不同报告之间的差异（重述或对照不同；一个财年的利润表、现金流量表用同一批报告的数，见 cn_pdf.build_rows）", notes[:20]))
    ok = [r for r in records.values() if r.get("status") == "ok"]
    if not ok:
        if not res.skipped and not done and not records:
            res.warnings.append("还没有处理过任何报告")
        return res
    rows = build_rows(records, company, source.MARKET)
    if res.ok:
        res.rows, res.replace = rows, True
    inc = sorted(rows["income"], key=lambda r: (r["period_end"], r["months"]))
    first = next((r for r in inc if r["basis"] == "reported"), None)
    last = inc[-1] if inc else None
    lines = ["%s：已核对 %d 份报告%s" % (source.SOURCE_ZH, len(ok), "，本次新处理 %d 份" % len(done) if done else "")]
    if first and last:
        lines.append("利润表覆盖 %s %s ~ %s %s（%d 行，其中推算 %d 行）" % (first["fiscal_year"], first["fiscal_period"], last["fiscal_year"],
                                                                 last["fiscal_period"], len(inc), len([r for r in inc if r["basis"] == "derived"])))
    for r in done:
        lines.append("%s（%s）：%s，%s" % (r["title"], r["published"], {"ok": "通过", "failed": "没通过", "no_statements": "找不到报表"}[r["status"]],
                                          r.get("mapping_source", "")))
    res.notes.append(("定期报告", lines))
    return res


def recheck(company, ctx, source):
    """回测：用 state 里存的报表行按现在的规则重新对照、核对（不下载、不调用 GPT），并与 data/ 里的数比较。"""
    res = Result(company["id"])
    records = load_records(ctx, company["id"])
    if not records:
        res.skipped = "还没有处理过任何报告"
        return res
    diffs = []
    for doc, rec in sorted(records.items()):
        if rec.get("status") != "ok":
            continue
        tmp = json.loads(json.dumps(rec))
        tmp["check_version"] = None
        try:
            new = finish_record(tmp, company, _NoGPT(ctx), {k: v for k, v in records.items() if k != doc})
        except NeedGPT:
            continue
        if new.get("status") != "ok":
            res.check("%s 重新核对" % rec["title"], False, "；".join(new.get("problems", [])[:5]))
            continue
        for kind, vals in (rec.get("values") or {}).items():
            for col in ("cur", "prior"):
                for f, a in vals[col].items():
                    b = new["values"].get(kind, {}).get(col, {}).get(f)
                    if (a is None) != (b is None) or (a is not None and abs(a - b) > 1e-6):
                        diffs.append("%s %s %s %s：%s → %s" % (rec["title"], kind, col, f, a, b))
    res.check("按现在的规则重新核对，结果与存下的一致", not diffs, "；".join(diffs[:8]))
    rows = build_rows(records, company, source.MARKET)
    from ..core.store import UnifiedStore
    st = UnifiedStore(ctx.data_dir(company["id"]))
    mism = compare_rows(rows, st.rows)
    res.check("data/ 里的数与报告一致", not mism, "；".join(mism[:8]))
    return res


class _NoGPT:
    """回测时用的上下文：不给 GPT（版式没变的报告沿用存下的对照）。"""

    def __init__(self, ctx):
        self._ctx = ctx
        self.gpt = None

    def gpt_ok(self, kind="events"):
        return False

    def __getattr__(self, k):
        return getattr(self._ctx, k)


def compare_rows(rows, existing):
    from ..pipeline.datasets import fmt
    from ..core import schema as S
    out = []
    for name in ("income", "balance", "cashflow"):
        keys = S.KEYS[name]
        have = {tuple(str(r.get(k)) for k in keys): r for r in existing.get(name, [])}
        for r in rows.get(name, []):
            k = tuple(str(r.get(x)) for x in keys)
            old = have.get(k)
            if not old:
                out.append("%s 缺少 %s" % (name, k))
                continue
            for f in S.FILES[name]:
                if f in S.NUMERIC and fmt(r.get(f)) != (old.get(f) or ""):
                    out.append("%s %s %s：data 里是 %s，报告是 %s" % (name, k, f, old.get(f), fmt(r.get(f))))
    return out


def check_company(company, ctx, source):
    """核对：data/ 里的数能由存下的报告重新得出。"""
    records = load_records(ctx, company["id"])
    if not records:
        return []
    from ..core.store import UnifiedStore
    rows = build_rows(records, company, source.MARKET)
    return compare_rows(rows, UnifiedStore(ctx.data_dir(company["id"])).rows)[:10]
