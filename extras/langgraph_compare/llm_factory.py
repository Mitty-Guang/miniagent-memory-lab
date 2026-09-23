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
    return ChatOpenAI(
        model=os.getenv("MODEL_NAME", "gpt-4o-mini"),
        base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        api_key=os.getenv("OPENAI_API_KEY", ""),
        temperature=0.7,
    )
