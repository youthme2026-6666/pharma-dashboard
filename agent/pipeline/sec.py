"""SEC EDGAR 访问：声明身份（SEC 要求 User-Agent 带联系方式）、限速、重试、本地缓存。

只用标准库。SEC 的公平访问规则：每秒不超过 10 次请求，并在 User-Agent 里写明联系人。
这里默认每 0.25 秒最多一次。身份从环境变量 SEC_USER_AGENT 读取，例如 "Lilly Dashboard ops@example.com"。
"""
import gzip
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
import zlib
from html.parser import HTMLParser


class SecError(RuntimeError):
    pass


class SEC:
    def __init__(self, user_agent=None, cache_dir=None, min_interval=0.25, retries=4, opener=None):
        ua = (user_agent if user_agent is not None else os.environ.get("SEC_USER_AGENT", "")).strip()
        if not re.search(r"\S+@\S+\.\S+", ua):
            raise SecError("SEC_USER_AGENT 未设置或没有联系邮箱。SEC 要求请求头写明身份，例如：'Lilly Dashboard you@example.com'。")
        self.ua = ua
        self.cache_dir = cache_dir
        self.min_interval = min_interval
        self.retries = retries
        self._last = 0.0
        self._open = opener or urllib.request.urlopen
        self.requests = 0
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

    # ---------- 底层 ----------
    def _cache_path(self, url):
        return os.path.join(self.cache_dir, hashlib.sha1(url.encode()).hexdigest()) if self.cache_dir else None

    def get(self, url, ttl=None):
        """返回字节。ttl=None 表示缓存永久有效（归档文件不会变），ttl=0 表示总是重新下载。"""
        cp = self._cache_path(url)
        if cp and os.path.exists(cp) and ttl != 0:
            if ttl is None or time.time() - os.path.getmtime(cp) < ttl:
                with open(cp, "rb") as f:
                    return f.read()
        delay = 2.0
        for attempt in range(self.retries + 1):
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            req = urllib.request.Request(url, headers={"User-Agent": self.ua, "Accept-Encoding": "gzip, deflate"})
            try:
                with self._open(req, timeout=60) as resp:
                    body = resp.read()
                    enc = (resp.headers.get("Content-Encoding") or "").lower()
                self.requests += 1
                if enc == "gzip":
                    body = gzip.decompress(body)
                elif enc == "deflate":
                    body = zlib.decompress(body)
                if cp:
                    with open(cp, "wb") as f:
                        f.write(body)
                return body
            except urllib.error.HTTPError as e:
                self.requests += 1
                if e.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                if e.code == 403:
                    raise SecError("SEC 拒绝访问（403）：%s。请确认 SEC_USER_AGENT 写明了真实联系人，且请求频率不高。" % url)
                raise SecError("SEC 返回 HTTP %s：%s" % (e.code, url))
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < self.retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise SecError("无法连接 SEC：%s（%s）" % (url, e))
        raise SecError("SEC 请求失败：%s" % url)

    def text(self, url, ttl=None):
        b = self.get(url, ttl)
        for enc in ("utf-8", "cp1252", "latin-1"):
            try:
                return b.decode(enc)
            except UnicodeDecodeError:
                continue
        return b.decode("utf-8", "replace")

    def json(self, url, ttl=None):
        return json.loads(self.get(url, ttl).decode("utf-8"))

    # ---------- EDGAR ----------
    @staticmethod
    def cik10(cik):
        return "%010d" % int(cik)

    def submissions(self, cik, include_older=False):
        """公司的申报清单（最近一页每次都重新下载）。include_older=True 时把更早的分页也读进来（缓存 7 天）。
        返回 [{form, accession, filing_date, report_date, items, primary}]，按申报日期从新到旧。"""
        d = self.json("https://data.sec.gov/submissions/CIK%s.json" % self.cik10(cik), ttl=0)
        pages = [d.get("filings", {}).get("recent", {})]
        if include_older:
            for f in d.get("filings", {}).get("files", []) or []:
                if f.get("name"):
                    pages.append(self.json("https://data.sec.gov/submissions/" + f["name"], ttl=7 * 86400))
        keys = ["form", "accessionNumber", "filingDate", "reportDate", "items", "primaryDocument"]
        out, seen = [], set()
        for r in pages:
            cols = [r.get(k) or [] for k in keys]
            for i in range(len(cols[0])):
                row = [c[i] if i < len(c) else "" for c in cols]
                if row[1] in seen:
                    continue
                seen.add(row[1])
                out.append({"form": row[0], "accession": row[1], "filing_date": row[2], "report_date": row[3],
                            "items": row[4] or "", "primary": row[5]})
        out.sort(key=lambda x: x["filing_date"], reverse=True)
        return out

    def folder(self, cik, accession):
        return "https://www.sec.gov/Archives/edgar/data/%d/%s/" % (int(cik), accession.replace("-", ""))

    def index_url(self, cik, accession):
        return self.folder(cik, accession) + accession + "-index.htm"

    def documents(self, cik, accession):
        """申报文件里的各个文件：[{name, type, description, url}]（来自 -index.htm 的文件表）。"""
        html = self.text(self.index_url(cik, accession))
        p = _IndexParser()
        p.feed(html)
        out = []
        for cells, href in p.rows:
            if not href or len(cells) < 4:
                continue
            name = href.split("/")[-1].split("?")[0]
            out.append({"name": name, "type": cells[3].strip(), "description": cells[1].strip(),
                        "url": self.folder(cik, accession) + name})
        return out

    def concept(self, cik, taxonomy, tag):
        """XBRL 单个科目的全部历史值（用于交叉核对总数）。"""
        return self.json("https://data.sec.gov/api/xbrl/companyconcept/CIK%s/%s/%s.json" % (self.cik10(cik), taxonomy, tag), ttl=0)


class _IndexParser(HTMLParser):
    """解析 EDGAR -index.htm 的文件表：每行 [序号, 说明, 文件名, 类型, 大小]，以及文件链接。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self._row, self._cell, self._href, self._in_table = [], None, None, None, 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table" and "tablefile" in (a.get("class") or "").lower():
            self._in_table += 1
        elif tag == "tr" and self._in_table:
            self._row, self._href = [], None
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "a" and self._row is not None and self._cell is not None:
            href = a.get("href") or ""
            if "/Archives/edgar/data/" in href and self._href is None:
                self._href = href.split("doc=")[-1]

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append((self._row, self._href))
            self._row = None
        elif tag == "table" and self._in_table:
            self._in_table -= 1

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)
