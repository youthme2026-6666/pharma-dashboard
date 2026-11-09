"""调用 OpenAI（GPT）的 Responses API。只用标准库。

GPT 只做需要判断的事：找重大事件、读新闻稿写一句中文说明、给新产品归类、读出第四季度的价格/销量拆分。
所有输出都要经过 verify.py 的确定性核对；核对不过就丢弃，不会写进数据。
密钥从环境变量 OPENAI_API_KEY 读取；模型用 OPENAI_MODEL，没设置时从可用模型里按 config.json 的偏好挑一个。
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

API = "https://api.openai.com/v1"


class GPTError(RuntimeError):
    pass


class GPT:
    def __init__(self, cfg=None, api_key=None, model=None, opener=None):
        self.cfg = (cfg or {}).get("gpt", {})
        self.key = (api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")).strip()
        self.model = (model if model is not None else os.environ.get("OPENAI_MODEL", "")).strip() or None
        self._open = opener or urllib.request.urlopen
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "web_searches": 0}
        self.log = []

    @property
    def available(self):
        return bool(self.key)

    # ---------- HTTP ----------
    def _request(self, method, path, payload=None, timeout=None):
        if not self.key:
            raise GPTError("没有设置 OPENAI_API_KEY")
        data = json.dumps(payload).encode() if payload is not None else None
        delay = 3.0
        for attempt in range(4):
            req = urllib.request.Request(API + path, data=data, method=method, headers={
                "Authorization": "Bearer " + self.key, "Content-Type": "application/json"})
            try:
                with self._open(req, timeout=timeout or self.cfg.get("timeout_seconds", 240)) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:800]
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise GPTError("OpenAI HTTP %s：%s" % (e.code, body))
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < 3:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise GPTError("无法连接 OpenAI：%s" % e)
        raise GPTError("OpenAI 请求失败")

    def resolve_model(self):
        """按 config.json 的偏好挑模型；偏好的都下线了，就用账号里版本号最新的通用模型（gpt-数字，不带 mini/pro 等后缀）。"""
        if self.model:
            return self.model
        prefs = self.cfg.get("model_preference") or ["gpt-5"]
        try:
            ids = {m["id"] for m in self._request("GET", "/models", timeout=30).get("data", [])}
        except GPTError as e:
            self.log.append("读取模型列表失败：%s" % e)
            ids = set()
        for p in prefs:
            if p in ids:
                self.model = p
                return p
        plain = [m for m in ids if re.fullmatch(r"gpt-\d+(?:\.\d+)?", m)]
        if plain:
            self.model = max(plain, key=lambda m: tuple(int(x) for x in re.findall(r"\d+", m)))
            self.log.append("偏好的模型都不可用，改用 %s" % self.model)
            return self.model
        self.model = prefs[0]
        return self.model

    # ---------- 调用 ----------
    def respond(self, instructions, user_input, web_search=False, schema=None, allowed_domains=None, max_tokens=None):
        """返回 {text, data, citations, sources, model}。web_search=True 时允许 GPT 联网搜索；
        schema 给出 JSON Schema 时要求结构化输出。遇到模型不支持的参数会逐项退回（例如域名过滤、结构化输出）。"""
        model = self.resolve_model()
        payload = {"model": model, "instructions": instructions, "input": user_input,
                   "max_output_tokens": max_tokens or self.cfg.get("max_output_tokens", 6000)}
        if web_search:
            tool = {"type": "web_search"}
            if allowed_domains:
                tool["filters"] = {"allowed_domains": allowed_domains}
            payload["tools"] = [tool]
            payload["include"] = ["web_search_call.action.sources"]
        if schema:
            payload["text"] = {"format": {"type": "json_schema", "name": "result", "schema": schema, "strict": True}}
        # 模型不接受某个参数时（HTTP 400 且报错里提到它），去掉该参数再试；其余错误直接抛出。
        fallbacks = [
            (("filters", "allowed_domains"), lambda p: p.get("tools") and p["tools"][0].pop("filters", None) is not None),
            (("include", "action.sources"), lambda p: p.pop("include", None) is not None),
            (("json_schema", "text.format", "json mode", "structured", "response_format"), lambda p: p.pop("text", None) is not None),
            (("web_search",), lambda p: bool(p.get("tools")) and p["tools"][0].get("type") == "web_search" and not p["tools"].__setitem__(0, {"type": "web_search_preview"})),
        ]
        while True:
            try:
                t0 = time.time()
                r = self._request("POST", "/responses", payload)
                self.log.append("responses %s %.1fs" % (model, time.time() - t0))
                return self._parse(r, model)
            except GPTError as e:
                msg = str(e)
                low = msg.lower()
                fixed = False
                if "HTTP 400" in msg:
                    for keys, fix in fallbacks:
                        if any(k in low for k in keys) and fix(payload):
                            self.log.append("参数不被接受（%s），调整后重试" % keys[0])
                            fixed = True
                            break
                if not fixed:
                    raise

    def _parse(self, r, model):
        self.usage["calls"] += 1
        u = r.get("usage") or {}
        self.usage["input_tokens"] += u.get("input_tokens", 0) or 0
        self.usage["output_tokens"] += u.get("output_tokens", 0) or 0
        texts, cites, sources = [], [], []
        for item in r.get("output") or []:
            if item.get("type") == "web_search_call":
                self.usage["web_searches"] += 1
                action = item.get("action") or {}
                for s in action.get("sources") or []:
                    if s.get("url"):
                        sources.append(s["url"])
                if action.get("type") in ("open_page", "find_in_page") and action.get("url"):
                    sources.append(action["url"])   # 打开过的网页也算看过的来源
            if item.get("type") == "message":
                for c in item.get("content") or []:
                    if c.get("type") == "output_text":
                        texts.append(c.get("text") or "")
                        for a in c.get("annotations") or []:
                            if a.get("type") == "url_citation" and a.get("url"):
                                cites.append(a["url"])
        text = "".join(texts)
        if r.get("status") == "incomplete":
            self.log.append("回答不完整：%s" % (r.get("incomplete_details") or {}))
        return {"text": text, "data": parse_json(text), "citations": cites, "sources": sources, "model": model, "status": r.get("status")}


def parse_json(text):
    """从回答里取出第一个 JSON 对象（兼容 ```json 代码块）。"""
    if not text:
        return None
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, re.S)
    if m:
        t = m.group(1)
    try:
        return json.loads(t)
    except ValueError:
        pass
    start = t.find("{")
    while start >= 0:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(t)):
            ch = t[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[start:i + 1])
                    except ValueError:
                        break
        start = t.find("{", start + 1)
    return None
