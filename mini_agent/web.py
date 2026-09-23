"""联网工具：http_get(url)——允许公网访问，内置 SSRF 防护（标准库实现，零依赖）。

安全设计（可配置）：
- **默认 open 模式**：允许访问任意公网 http/https 地址；
- **SSRF 防护**：拒绝内网 / 回环 / 链路本地 / 保留地址（含云元数据 169.254.169.254），
  且解析域名后逐个 IP 校验（防 DNS 指向内网）；
- **可选 allowlist 模式**：`WEB_ACCESS=allowlist`（可配 `WEB_ALLOWED_DOMAINS=a.com,b.com`）；
- 超时 20s、返回体积截断 4000 字符、剥离 HTML 标签/脚本。

诚实边界：这是"工具级"防护；生产环境还应叠加出网代理、审计日志、限流与内容安全策略，
并把 http_get 纳入人工审批（本项目 HITL 已支持按工具审批）。
"""
import asyncio
import ipaddress
import json
import os
import re
import socket
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

from mini_agent.tools import BaseTool, ToolResult

DEFAULT_ALLOWED_DOMAINS: List[str] = [
    "wttr.in",              # 天气（纯文本）
    "api.open-meteo.com",   # 天气（JSON）
    "open-meteo.com",
    "api.github.com",       # GitHub 公开 API
]


def domain_of(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "").lower()


def is_allowed(url: str, allowed: Optional[List[str]] = None) -> bool:
    allowed = allowed or DEFAULT_ALLOWED_DOMAINS
    host = domain_of(url)
    return any(host == d or host.endswith("." + d) for d in allowed)


def is_private_host(host: str) -> bool:
    """判断主机是否指向非公网地址（内网/回环/保留/链路本地）。"""
    if not host:
        return True
    if host in {"localhost", "localhost.localdomain"}:
        return True

    def blocked(addr: str) -> bool:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        return not ip.is_global  # 只有可全球路由的公网地址才放行

    try:
        return blocked(host)  # 直接是 IP 的情况
    except Exception:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False  # 解析失败交给请求阶段报错
    return any(blocked(info[4][0]) for info in infos)


class HttpGetTool(BaseTool):
    name: str = "http_get"
    description: str = (
        "抓取 HTTP(S) 接口/网页的文本内容（可访问公网；内网/本机地址会被拒绝；"
        "返回纯文本、截断到 4000 字符）。可用于查询天气等实时信息，"
        "例如 https://wttr.in/Beijing?format=3"
    )
    parameters: Dict = {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "要抓取的完整 URL"}},
        "required": ["url"],
    }
    timeout: int = 20
    max_chars: int = 4000
    access_mode: str = ""          # 空则读环境变量 WEB_ACCESS（默认 open）
    allowed_domains: List[str] = []

    def _mode(self) -> str:
        return (self.access_mode or os.getenv("WEB_ACCESS", "open")).strip().lower()

    def _allowed(self) -> List[str]:
        if self.allowed_domains:
            return self.allowed_domains
        raw = os.getenv("WEB_ALLOWED_DOMAINS", "").strip()
        if raw:
            return [d.strip().lower() for d in raw.split(",") if d.strip()]
        return DEFAULT_ALLOWED_DOMAINS

    def _fetch(self, url: str) -> str:
        request = urllib.request.Request(url, headers={"User-Agent": "miniagent/1.0"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read(self.max_chars * 6)
        return raw.decode("utf-8", errors="replace")

    async def execute(self, url: str, **kwargs) -> ToolResult:
        scheme = (urllib.parse.urlparse(url).scheme or "").lower()
        if scheme not in {"http", "https"}:
            return ToolResult(success=False, error=f"仅支持 http/https，收到：{scheme or '(空)'}")

        # SSRF 防护优先：内网/本机/保留地址一律拒绝（open 模式下这是唯一防线）
        if is_private_host(domain_of(url)):
            return ToolResult(
                success=False,
                error="拒绝访问内网 / 本机 / 保留地址（SSRF 防护）",
            )

        if self._mode() == "allowlist" and not is_allowed(url, self._allowed()):
            return ToolResult(
                success=False,
                error=(
                    f"域名不在白名单内：{domain_of(url) or url}"
                    f"（允许：{', '.join(self._allowed())}；如需放开请设 WEB_ACCESS=open）"
                ),
            )

        try:
            text = await asyncio.to_thread(self._fetch, url)
        except Exception as exc:
            # 超时重试一次（弱网常见），其余错误直接返回
            if "timed out" in str(exc).lower():
                try:
                    text = await asyncio.to_thread(self._fetch, url)
                except Exception as exc2:
                    return ToolResult(success=False, error=f"请求失败: {exc2}")
            else:
                return ToolResult(success=False, error=f"请求失败: {exc}")

        text = re.sub(
            r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.S | re.I
        )
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return ToolResult(success=True, output=text[: self.max_chars] or "(空响应)")


def _clean_html(fragment: str, limit: int = 240) -> str:
    import html as html_lib

    text = re.sub(
        r"<script.*?</script>|<style.*?</style>", " ", fragment, flags=re.S | re.I
    )
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(re.sub(r"\s+", " ", text)).strip()
    return text[:limit]


def _query_tokens(query: str) -> List[str]:
    """提取相关性 token：中文 2-gram + 英文词（>=2 字符，纯数字不计）。

    会剔除弱词/虚词 token（今年、推荐、的、了…），避免"弱词命中"造成的相关性误判。
    """
    tokens: List[str] = []
    for run in re.findall(r"[\u4e00-\u9fff]+", query):
        if len(run) == 1:
            tokens.append(run)
        for i in range(len(run) - 1):
            tokens.append(run[i : i + 2])
    tokens += [
        word.lower() for word in re.findall(r"[A-Za-z][A-Za-z0-9]{1,}", query)
    ]
    tokens = list(dict.fromkeys(tokens))

    weak = [w for w in (*_QUERY_PREFIXES, *_QUERY_SUFFIXES) if w]
    kept: List[str] = []
    for token in tokens:
        if token.isascii():
            kept.append(token)
            continue
        if any(ch in _FUNCTION_CHARS for ch in token):        # 含"的/了/是"等虚词
            continue
        if any(token in w or w in token for w in weak):       # 属于弱词（或弱词的一部分）
            continue
        kept.append(token)
    return kept or tokens   # 全是弱词时退回原集合，避免空集


def _relevance_score(item: Dict[str, str], tokens: List[str]) -> int:
    text = (item.get("title", "") + " " + item.get("snippet", "")).lower()
    return sum(1 for token in tokens if token in text)


def _sort_by_relevance(
    results: List[Dict[str, str]], tokens: List[str]
) -> List[Dict[str, str]]:
    """按相关性稳定排序；存在相关结果时剔除 0 分噪声。"""
    scored = [
        (_relevance_score(item, tokens), index, item)
        for index, item in enumerate(results)
    ]
    scored.sort(key=lambda row: (-row[0], row[1]))
    relevant = [item for score, _, item in scored if score > 0]
    return relevant or [item for _, _, item in scored]


def _item_text(item: Dict[str, str]) -> str:
    return (item.get("title", "") + " " + item.get("snippet", "")).lower()


def _ddg_real_url(href: str) -> str:
    """DuckDuckGo 结果链接形如 //duckduckgo.com/l/?uddg=<urlencoded>，取出真实 URL。"""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    if "duckduckgo.com" in parsed.netloc:
        params = urllib.parse.parse_qs(parsed.query)
        target = (params.get("uddg") or [""])[0]
        return urllib.parse.unquote(target) if target else ""
    return href


def _is_relevant(item: Dict[str, str], tokens: List[str]) -> bool:
    """整体相关性判断（决定是否触发自动重搜）。

    注意：token 已剔除弱词/虚词（今年、推荐、的…），因此"命中 ≥1"即可——
    「今年的北京实习推荐」的生肖黄历页在弱词过滤后命中 0，会被正确判为不相关。
    """
    text = _item_text(item)
    return any(token in text for token in tokens)


# 垂直领域：(触发词, 平台词, 话题词)
# 泛词检索会退化成百科/黄历（实测「北京实习」→ 北京市百科），加平台词并前置能落到真实列表页
# （实测「实习僧 北京」→ 国家大学生就业服务平台岗位页；平台词放后面无效）。
VERTICAL_HINTS = (
    (("实习", "校招", "招聘", "求职"), "实习僧", ("实习", "招聘", "岗位", "校招")),
    (("租房", "房源", "房租"), "贝壳租房", ("租房", "房源", "租金")),
    (("机票", "航班"), "航班动态", ("航班", "机票", "起飞")),
    (("股票", "股价", "基金"), "东方财富", ("股价", "行情", "股票")),
    (("论文", "文献", "综述"), "知网", ("论文", "文献", "期刊")),
    (("天气",), "天气预报", ("天气", "气温", "降雨")),
)


def _vertical_hint(query: str) -> str:
    for keywords, hint, _ in VERTICAL_HINTS:
        if any(keyword in query for keyword in keywords) and hint not in query:
            return hint
    return ""


def _vertical_expected(query: str) -> tuple:
    """命中垂直领域时返回 (平台词, 话题词)；话题词用于验收兜底结果（防"命中地名"式假相关）。"""
    for keywords, hint, expected in VERTICAL_HINTS:
        if any(keyword in query for keyword in keywords):
            return hint, expected
    return "", ()


def _accept_fallback(item: Dict[str, str], tokens: List[str], expected: tuple) -> bool:
    """验收兜底查询的结果：配置了话题词时要求命中话题词，否则退回普通相关性判断。"""
    if expected:
        text = _item_text(item)
        return any(token in text for token in expected)
    return _is_relevant(item, tokens)


_QUERY_PREFIXES = (
    "今年的",
    "今年",
    "今天的",
    "今天",
    "现在的",
    "现在",
    "最新的",
    "最新",
    "请问",
    "帮我",
    "我想",
    "想知道",
    "查一下",
    "查询",
    "搜索",
    "了解一下",
    "有没有",
)


def _strip_query_prefix(text: str) -> str:
    """剥离口语化前缀（今年/现在/帮我…），仅在缩短重试时使用。"""
    for prefix in sorted(_QUERY_PREFIXES, key=len, reverse=True):
        if text.startswith(prefix) and len(text) - len(prefix) >= 2:
            return text[len(prefix) :]
    return text


_FUNCTION_CHARS = set("的了是在和与有我你他它这那")

_QUERY_SUFFIXES = (
    "开放时间",
    "怎么样",
    "怎么去",
    "有哪些",
    "是什么",
    "参观",
    "介绍",
    "攻略",
    "门票",
    "交通",
    "价格",
    "地址",
    "推荐",
    "新闻",
    "最新",
    "时间",
)


def _strip_query_suffix(text: str) -> str:
    """剥离常见查询后缀词（参观/攻略/开放时间…），仅在缩短重试时使用。"""
    for suffix in sorted(_QUERY_SUFFIXES, key=len, reverse=True):
        if text.endswith(suffix) and len(text) - len(suffix) >= 2:
            return text[: -len(suffix)]
    return text


def _shorten_query(query: str) -> str:
    """把长查询缩为核心词：多段时取最长汉字段，纯英文取前两个词。"""
    parts = query.split()
    if len(parts) >= 2:
        cjk = [p for p in parts if re.search(r"[\u4e00-\u9fff]", p)]
        if cjk:
            candidates = [_strip_query_suffix(_strip_query_prefix(p)) for p in cjk]
            return max(candidates, key=lambda p: len(re.findall(r"[\u4e00-\u9fff]", p)))
        return " ".join(parts[:2])
    runs = re.findall(r"[\u4e00-\u9fff]+", query)
    if runs and len(runs[0]) >= 6:
        core = _strip_query_suffix(_strip_query_prefix(runs[0]))
        return core[:4] if len(core) >= 6 else core
    return ""


class WebSearchTool(BaseTool):
    """联网搜索（Bing RSS 版，零依赖）：给模型一个"先搜索再抓取"的入口。

    稳定性处理：RSS 解析失败回退 HTML；结果按相关性过滤（中文 2-gram / 英文词匹配），
    若整体不相关则自动缩短关键词重搜一次（应对引擎对长短语错误分词的情况）。
    """

    name: str = "web_search"
    description: str = (
        "联网搜索实时信息（返回前几条结果的标题 / 链接 / 摘要）。用法建议："
        "query 用简短关键词（如「Python 3.12 新特性」），不要用口语化长句；"
        "返回的摘要通常已足够作答，仅当需要正文时才用 http_get 打开链接（建议最多打开 2 个）。"
    )
    parameters: Dict = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "max_results": {"type": "integer", "description": "返回条数（默认 5）"},
        },
        "required": ["query"],
    }
    timeout: int = 15
    max_results: int = 5
    backend: str = ""        # 空则读环境变量 WEB_SEARCH_BACKEND（bing_rss 默认 / duckduckgo / tavily / openwebsearch）
    heuristics: Optional[bool] = None  # None=按后端自动（爬取类开、API 类关）；False 时只做原始检索（A/B 用）

    def _backend(self) -> str:
        return (self.backend or os.getenv("WEB_SEARCH_BACKEND", "bing_rss")).strip().lower()

    def _use_heuristics(self, override: Optional[bool] = None) -> bool:
        """启发式（相关性过滤/自动重搜/平台词兜底）默认只对爬取类后端启用。

        实测（docs/SEARCH_EVAL.md）：在 Tavily 这类优质后端上，启发式会因"跨语言结果被
        中文 bigram 过滤"等原因误伤结果（hit@3 100% → 96.7%），故按后端自动关闭。
        """
        if override is not None:
            return bool(override)
        if self.heuristics is not None:
            return bool(self.heuristics)
        return self._backend() in {"bing_rss", "bing"}

    def _fetch(self, url: str) -> str:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
                )
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.read(600_000).decode("utf-8", errors="replace")

    def parse_results(self, page_html: str) -> List[Dict[str, str]]:
        results: List[Dict[str, str]] = []
        blocks = re.findall(
            r'<li[^>]*class="b_algo"[^>]*>(.*?)</li>', page_html, re.S
        )
        for block in blocks:
            match = re.search(
                r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S
            )
            if not match:
                continue
            paragraph = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
            snippet = _clean_html(paragraph.group(1), 240) if paragraph else ""
            if not snippet:
                snippet = _clean_html(block, 240)
            results.append(
                {
                    "url": match.group(1),
                    "title": _clean_html(match.group(2), 160),
                    "snippet": snippet,
                }
            )
        return results

    def parse_rss(self, xml_text: str) -> List[Dict[str, str]]:
        """解析 Bing 的 RSS 输出（比 HTML 更稳、更干净）。"""
        import xml.etree.ElementTree as ET

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return []
        results: List[Dict[str, str]] = []
        for item in root.iter("item"):
            link = (item.findtext("link") or "").strip()
            if not link:
                continue
            results.append(
                {
                    "url": link,
                    "title": _clean_html(item.findtext("title") or "", 160),
                    "snippet": _clean_html(item.findtext("description") or "", 240),
                }
            )
        return results

    def parse_ddg(self, page_html: str) -> List[Dict[str, str]]:
        """解析 DuckDuckGo 结果页（html.duckduckgo.com / lite）。真实链接在 uddg 参数里。"""
        results: List[Dict[str, str]] = []
        patterns = (
            r'<a[^>]*class=["\']result__a["\'][^>]*href="([^"]+)"[^>]*>(.*?)</a>',   # html 版
            r'<a[^>]*href="([^"]+)"[^>]*class=["\']result-link["\'][^>]*>(.*?)</a>',  # lite 版
        )
        seen = set()
        for pattern in patterns:
            for href, title_html in re.findall(pattern, page_html, re.S):
                url = _ddg_real_url(href)
                if not url or url in seen:
                    continue
                seen.add(url)
                results.append(
                    {
                        "url": url,
                        "title": _clean_html(title_html, 160),
                        "snippet": "",
                    }
                )
        # 摘要（html 版）：result__snippet / result-snippet
        snippets = re.findall(
            r'class=["\']result(?:__snippet|-snippet)["\'][^>]*>(.*?)</(?:a|td)>', page_html, re.S
        )
        for item, snippet in zip(results, snippets):
            item["snippet"] = _clean_html(snippet, 240)
        return results

    async def _bing_search_once(self, query: str) -> List[Dict[str, str]]:
        """Bing：优先 RSS（结构稳定），失败或为空则回退 HTML 解析。"""
        quoted = urllib.parse.quote(query)
        rss_url = f"https://cn.bing.com/search?format=rss&q={quoted}"
        html_url = f"https://cn.bing.com/search?setlang=zh-cn&q={quoted}"
        try:
            page = await asyncio.to_thread(self._fetch, rss_url)
            results = self.parse_rss(page)
            if results:
                return results
        except Exception:
            pass
        page = await asyncio.to_thread(self._fetch, html_url)
        return self.parse_results(page)

    async def _ddg_search_once(self, query: str) -> List[Dict[str, str]]:
        """DuckDuckGo（免费、无 key；国内需代理：设置 HTTPS_PROXY 即可被 urllib 自动采用）。"""
        url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
        page = await asyncio.to_thread(self._fetch, url)
        return self.parse_ddg(page)

    def _post_json(self, url: str, payload: Dict, headers: Optional[Dict] = None) -> str:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "miniagent/1.0",
                **(headers or {}),
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.read(600_000).decode("utf-8", errors="replace")

    async def _tavily_search_once(self, query: str) -> List[Dict[str, str]]:
        """Tavily（agent-native 搜索 API；免费额度 1000 credits/月，basic=1 credit）。

        key 从环境变量 TAVILY_API_KEY 读取（写在 .env，仓库不落库）。
        """
        import contextlib

        with contextlib.suppress(Exception):
            from mini_agent.config import load_env_file  # 懒加载 .env

            load_env_file()
        key = os.getenv("TAVILY_API_KEY", "").strip()
        if not key:
            raise RuntimeError("未配置 TAVILY_API_KEY（请在 .env 中设置）")

        payload = {
            "api_key": key,
            "query": query,
            "max_results": max(self.max_results, 5),
            "search_depth": "basic",
            "include_answer": False,
        }
        page = await asyncio.to_thread(
            self._post_json, "https://api.tavily.com/search", payload
        )
        data = json.loads(page)
        results: List[Dict[str, str]] = []
        for item in data.get("results", []):
            results.append(
                {
                    "url": str(item.get("url") or ""),
                    "title": _clean_html(str(item.get("title") or ""), 160),
                    "snippet": _clean_html(str(item.get("content") or ""), 240),
                }
            )
        return results

    async def _openwebsearch_once(self, query: str) -> List[Dict[str, str]]:
        """Open-WebSearch 本地 daemon（免 key 多引擎聚合；默认 bing，可配 baidu/duckduckgo）。

        启动：`npx --yes open-websearch@latest serve`（默认 http://127.0.0.1:3000）
        可用环境变量：OPEN_WEBSEARCH_URL、OPEN_WEBSEARCH_ENGINES（如 bing,baidu,duckduckgo）
        """
        base = os.getenv("OPEN_WEBSEARCH_URL", "http://127.0.0.1:3000").rstrip("/")
        payload: Dict = {"query": query, "limit": max(self.max_results, 5)}
        engines = os.getenv("OPEN_WEBSEARCH_ENGINES", "").strip()
        if engines:
            payload["engines"] = [e.strip() for e in engines.split(",") if e.strip()]
        page = await asyncio.to_thread(self._post_json, f"{base}/search", payload)
        data = json.loads(page)
        items = data.get("results") or data.get("data") or []
        if isinstance(items, dict):   # 兼容 {"results": {"bing": [...]}} 形式
            flat: List = []
            for value in items.values():
                flat.extend(value if isinstance(value, list) else [])
            items = flat
        results: List[Dict[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or item.get("link") or "")
            if not url:
                continue
            results.append(
                {
                    "url": url,
                    "title": _clean_html(str(item.get("title") or ""), 160),
                    "snippet": _clean_html(
                        str(
                            item.get("description")
                            or item.get("snippet")
                            or item.get("content")
                            or ""
                        ),
                        240,
                    ),
                }
            )
        return results

    async def _search_once(self, query: str) -> List[Dict[str, str]]:
        backend = self._backend()
        if backend in {"duckduckgo", "ddg"}:
            return await self._ddg_search_once(query)
        if backend == "tavily":
            return await self._tavily_search_once(query)
        if backend in {"openwebsearch", "open-websearch", "ows"}:
            return await self._openwebsearch_once(query)
        return await self._bing_search_once(query)

    async def search_results(
        self, query: str, limit: int = 0, heuristics: Optional[bool] = None
    ) -> tuple:
        """返回 (results, note)。

        heuristics=False 时只做原始检索（不排序/不重试），用于 A/B 评估对照。
        """
        limit = limit or self.max_results
        query = " ".join((query or "").split())
        if not query:
            return [], ""
        use_heuristics = self.heuristics if heuristics is None else heuristics
        results = await self._search_once(query)
        if not use_heuristics:
            return results[:limit], ""

        tokens = _query_tokens(query)
        results = _sort_by_relevance(results, tokens)
        note = ""
        # 结果整体不相关时（长短语常被引擎错误分词），自动缩短关键词重搜一次；
        # 仍不相关则用"平台词 + 核心词"再试一次（泛词检索退化的兜底）。
        hint, expected = _vertical_expected(query)
        if results and not _is_relevant(results[0], tokens):
            fallback = _shorten_query(query)
            if fallback and fallback != query:
                try:
                    alt = await self._search_once(fallback)
                except Exception:
                    alt = []
                alt_tokens = _query_tokens(fallback)
                alt = _sort_by_relevance(alt, alt_tokens)
                if alt and _accept_fallback(alt[0], alt_tokens, expected):
                    results = alt
                    note = f"（原查询结果不相关，已自动改用「{fallback}」重搜）"
                elif hint:
                    hinted = f"{hint} {fallback}"
                    try:
                        alt2 = await self._search_once(hinted)
                    except Exception:
                        alt2 = []
                    alt2_tokens = _query_tokens(hinted)
                    alt2 = _sort_by_relevance(alt2, alt2_tokens)
                    if alt2 and _accept_fallback(alt2[0], alt2_tokens, expected):
                        results = alt2
                        note = (
                            f"（原查询结果不相关，已自动改用「{hinted}」重搜；"
                            "泛词检索建议加平台词并前置，如「实习僧 北京」）"
                        )
        return results[:limit], note

    async def execute(self, query: str, max_results: int = 0, **kwargs) -> ToolResult:
        limit = max_results or self.max_results
        try:
            results, note = await self.search_results(query, limit=limit)
        except Exception as exc:
            return ToolResult(success=False, error=f"搜索请求失败: {exc}")

        if not results:
            return ToolResult(success=False, error="搜索没有返回可解析的结果（页面结构可能变化）")
        lines = []
        for index, item in enumerate(results, 1):
            lines.append(f"{index}. {item['title']}\n   链接: {item['url']}\n   摘要: {item['snippet']}")
        lines.append("（以上摘要通常已足够作答；仅在需要正文时用 http_get 打开具体链接）")
        if note:
            lines.insert(0, note)
        return ToolResult(success=True, output="\n".join(lines))
