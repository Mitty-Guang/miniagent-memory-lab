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
    """提取相关性 token：中文 2-gram + 英文词（>=2 字符，纯数字不计）。"""
    tokens: List[str] = []
    for run in re.findall(r"[\u4e00-\u9fff]+", query):
        if len(run) == 1:
            tokens.append(run)
        for i in range(len(run) - 1):
            tokens.append(run[i : i + 2])
    tokens += [
        word.lower() for word in re.findall(r"[A-Za-z][A-Za-z0-9]{1,}", query)
    ]
    return list(dict.fromkeys(tokens))


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
            candidates = [_strip_query_suffix(p) for p in cjk]
            return max(candidates, key=lambda p: len(re.findall(r"[\u4e00-\u9fff]", p)))
        return " ".join(parts[:2])
    runs = re.findall(r"[\u4e00-\u9fff]+", query)
    if runs and len(runs[0]) >= 6:
        core = _strip_query_suffix(runs[0])
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

    async def _search_once(self, query: str) -> List[Dict[str, str]]:
        """执行一次搜索：优先 RSS（结构稳定），失败或为空则回退 HTML 解析。"""
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

    async def execute(self, query: str, max_results: int = 0, **kwargs) -> ToolResult:
        limit = max_results or self.max_results
        query = " ".join((query or "").split())
        if not query:
            return ToolResult(success=False, error="搜索关键词不能为空")

        tokens = _query_tokens(query)
        note = ""
        try:
            results = _sort_by_relevance(await self._search_once(query), tokens)
        except Exception as exc:
            return ToolResult(success=False, error=f"搜索请求失败: {exc}")

        # 结果整体不相关时（长短语常被引擎错误分词），自动缩短关键词重搜一次
        if results and _relevance_score(results[0], tokens) == 0:
            fallback = _shorten_query(query)
            if fallback and fallback != query:
                try:
                    alt = await self._search_once(fallback)
                except Exception:
                    alt = []
                alt_tokens = _query_tokens(fallback)
                alt = _sort_by_relevance(alt, alt_tokens)
                if alt and _relevance_score(alt[0], alt_tokens) > 0:
                    results = alt
                    note = f"（原查询结果不相关，已自动改用「{fallback}」重搜）"

        results = results[:limit]
        if not results:
            return ToolResult(success=False, error="搜索没有返回可解析的结果（页面结构可能变化）")
        lines = []
        for index, item in enumerate(results, 1):
            lines.append(f"{index}. {item['title']}\n   链接: {item['url']}\n   摘要: {item['snippet']}")
        lines.append("（以上摘要通常已足够作答；仅在需要正文时用 http_get 打开具体链接）")
        if note:
            lines.insert(0, note)
        return ToolResult(success=True, output="\n".join(lines))
