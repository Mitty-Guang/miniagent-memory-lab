"""
MiniAgent 使用示例
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
import os
from mini_agent import MiniAgent
from mini_agent.llm import SimpleLLM


async def example_file_operations():
    """文件操作示例"""
    print("=== 文件操作示例 ===")
    
    # 设置你的API密钥
    api_key = os.getenv("OPENAI_API_KEY", "your-api-key-here")
    
    llm = SimpleLLM(api_key=api_key, model="gpt-4o-mini")
    agent = MiniAgent(llm=llm, name="FileAgent")
    
    # 任务: 创建文件并写入内容
    task = "在当前目录创建一个名为demo.txt的文件，内容是'这是一个测试文件，由MiniAgent创建'"
    result = await agent.run(task)
    print(f"结果: {result}")


async def example_python_execution():
    """Python代码执行示例"""
    print("\n=== Python代码执行示例 ===")
    
    api_key = os.getenv("OPENAI_API_KEY", "your-api-key-here")
    
    llm = SimpleLLM(api_key=api_key, model="gpt-4o-mini")
    agent = MiniAgent(llm=llm, name="PythonAgent")
    
    # 任务: 数学计算
    task = "用Python计算斐波那契数列的前10项，并将结果保存到fib.txt文件中"
    result = await agent.run(task)
    print(f"结果: {result}")


async def example_data_processing():
    """数据处理示例"""
    print("\n=== 数据处理示例 ===")
    
    api_key = os.getenv("OPENAI_API_KEY", "your-api-key-here")
    
    llm = SimpleLLM(api_key=api_key, model="gpt-4o-mini") 
    agent = MiniAgent(llm=llm, name="DataAgent")
    
    # 任务: 清洗文本文件
    task = """
    请执行以下任务:
    1. 创建一个包含一些杂乱文本的文件data.txt
    2. 写一个Python程序清洗这个文件（去除空行，去除首尾空格）
    3. 将清洗后的结果保存到cleaned_data.txt
    """
    result = await agent.run(task)
    print(f"结果: {result}")


async def run_all_examples():
    """运行所有示例"""
    print("🚀 MiniAgent 示例程序")
    print("确保已设置环境变量 OPENAI_API_KEY")
    
    try:
        await example_file_operations()
        await example_python_execution()
        await example_data_processing()
        print("\n✅ 所有示例执行完成!")
    except Exception as e:
        print(f"❌ 示例执行出错: {e}")


if __name__ == "__main__":
    asyncio.run(run_all_examples())