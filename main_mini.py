"""
MiniAgent 主运行文件
"""
import asyncio
import os
from mini_agent import MiniAgent
from mini_agent.llm import SimpleLLM


def load_env_file(path: str = ".env") -> None:
    """把 .env 加载进环境变量（轻量实现，不依赖第三方库）。"""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


async def main():
    """主函数"""
    # 从环境变量（或 .env）获取配置
    load_env_file()
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()
    model = os.getenv("MODEL_NAME", "gpt-4o-mini").strip()

    if not api_key:
        print("❌ 请设置环境变量 OPENAI_API_KEY（或在项目根目录创建 .env 文件）")
        print("   PowerShell: $env:OPENAI_API_KEY='你的API密钥'")
        return

    print(f"🔌 使用模型: {model} @ {base_url}")

    # 创建LLM实例
    llm = SimpleLLM(
        api_key=api_key,
        model=model,
        base_url=base_url,
    )
    
    # 创建代理
    agent = MiniAgent(
        llm=llm,
        name="MiniAgent",
        max_steps=10
    )
    
    print("🤖 MiniAgent 已启动!")
    print("💡 该Agent支持以下功能:")
    print("   - Python代码执行")
    print("   - 文件读写操作")
    print("   - 命令行执行")
    print("\n例如，你可以输入:")
    print("   '在当前目录创建一个hello.txt文件，内容是Hello World'")
    print("   '列出当前目录的所有文件'")
    print("   '写一个Python程序计算1到100的和'")
    
    # 交互循环
    while True:
        try:
            user_input = input("\n请输入你的任务 (输入 'quit' 退出): ")
            
            if user_input.lower() in ['quit', 'exit', 'q']:
                print("👋 再见!")
                break
            
            if not user_input.strip():
                continue
            
            # 执行任务
            result = await agent.run(user_input)
            print(f"\n📋 执行结果:\n{result}")
            
        except KeyboardInterrupt:
            print("\n👋 程序被中断，再见!")
            break
        except Exception as e:
            print(f"❌ 发生错误: {e}")


if __name__ == "__main__":
    asyncio.run(main())