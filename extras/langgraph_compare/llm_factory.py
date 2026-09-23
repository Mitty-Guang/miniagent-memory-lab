"""LangChain 版 LLM 工厂：读取仓库根目录的 .env（与主项目同一份配置）。

不依赖 python-dotenv：用与主项目一致的轻量 .env 解析。
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_env_file(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def get_llm():
    from langchain_openai import ChatOpenAI

    load_env_file()
    llm = ChatOpenAI(
        model=os.getenv("MODEL_NAME", "gpt-4o-mini"),
        base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        api_key=os.getenv("OPENAI_API_KEY", ""),
        temperature=0.7,
    )
    CREATED_LLMS.append(llm)   # 登记：调用方可在一轮结束后统一关闭（避免跨事件循环泄漏）
    return llm


CREATED_LLMS: list = []        # 本进程创建过的 ChatOpenAI（用于逐轮关闭）


async def close_created_llms() -> None:
    """关闭并清空登记的 LLM 客户端（httpx 连接绑定事件循环，跨轮复用会报 Event loop is closed）。"""
    import asyncio

    while CREATED_LLMS:
        llm = CREATED_LLMS.pop()
        for attr in ("async_client", "root_async_client", "client"):
            client = getattr(llm, attr, None)
            close = getattr(client, "aclose", None) or getattr(client, "close", None)
            if close is None:
                continue
            try:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass
            break
