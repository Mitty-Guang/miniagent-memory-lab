"""配置加载：从 .env 读取 API 配置（不依赖第三方库）。"""
import asyncio
import contextlib
import os
import time

from mini_agent.llm import LLMResponse, SimpleLLM


def load_env_file(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def llm_kwargs() -> dict:
    load_env_file()
    return {
        "api_key": os.getenv("OPENAI_API_KEY", "").strip(),
        "model": os.getenv("MODEL_NAME", "gpt-4o-mini").strip(),
        "base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip(),
    }


async def warmup_async(attempts: int = 8, delay: float = 3.0, trace=None) -> bool:
    """冷启动预热：新进程的首次调用常因连接问题失败，先消耗掉。

    返回是否预热成功；失败也继续跑（后续任务自带重试）。
    """
    llm = CountingLLM(**llm_kwargs(), max_retries=1, trace=trace)
    try:
        for index in range(attempts):
            response = await llm.chat([{"role": "user", "content": "ping"}])
            content = response.content or ""
            if "LLM调用失败" not in content:
                print(f"[warmup] ok（第 {index + 1} 次尝试）", flush=True)
                return True
            await asyncio.sleep(delay)
        print(f"[warmup] failed（{attempts} 次尝试均失败，继续运行）", flush=True)
        return False
    finally:
        with contextlib.suppress(Exception):
            await llm.client.close()


def warmup(attempts: int = 8, delay: float = 3.0, trace=None) -> bool:
    """同步入口：在脚本 main 里调用一次。"""
    return asyncio.run(warmup_async(attempts=attempts, delay=delay, trace=trace))


class CountingLLM(SimpleLLM):
    """统计调用次数/字符数/token/延迟，对失败调用做指数退避重试，可选 trace。

    为了拿到 token usage，这里直接调用 OpenAI 兼容客户端（逻辑与 SimpleLLM 一致），
    而不是包一层 super().chat()。
    """

    def __init__(
        self,
        *args,
        max_retries: int = 5,
        base_delay: float = 2.0,
        trace=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.calls = 0
        self.prompt_chars = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.latencies = []
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.trace = trace

    async def chat(self, messages, system_prompt=None, tools=None):
        chat_messages = []
        if system_prompt:
            chat_messages.append({"role": "system", "content": system_prompt})
        chat_messages.extend(messages)

        request_params = {
            "model": self.model,
            "messages": chat_messages,
            "temperature": 0.7,
        }
        if tools:
            request_params["tools"] = tools
            request_params["tool_choice"] = "auto"

        response = None
        last_error = ""
        for attempt in range(self.max_retries):
            self.calls += 1
            for msg in messages:
                self.prompt_chars += len(str(msg.get("content") or ""))
                self.prompt_chars += len(str(msg.get("tool_calls") or ""))
            if system_prompt:
                self.prompt_chars += len(system_prompt)

            started = time.time()
            try:
                response = await self.client.chat.completions.create(**request_params)
            except Exception as exc:
                last_error = str(exc)
                print(f"[llm-retry] 第 {attempt + 1} 次异常: {exc}", flush=True)
                if self.trace is not None:
                    self.trace.log(
                        "llm_error", {"attempt": attempt + 1, "error": last_error[:200]}
                    )
                await asyncio.sleep(min(self.base_delay * (2**attempt), 16.0))
                continue

            latency = time.time() - started
            self.latencies.append(latency)

            usage = getattr(response, "usage", None)
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            self.prompt_tokens += prompt_tokens
            self.completion_tokens += completion_tokens

            message = response.choices[0].message
            result = LLMResponse(content=message.content)
            if message.tool_calls:
                result.tool_calls = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in message.tool_calls
                ]

            if self.trace is not None:
                self.trace.log(
                    "llm",
                    {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "latency": round(latency, 3),
                        "tool_calls": [
                            tc["function"]["name"] for tc in (result.tool_calls or [])
                        ],
                    },
                )
            return result

        return LLMResponse(
            content=f"LLM调用失败: 重试 {self.max_retries} 次后仍失败（最后错误: {last_error[:150]}）"
        )
