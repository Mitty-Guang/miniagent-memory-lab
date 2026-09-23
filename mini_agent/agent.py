"""
智能代理核心实现
"""
import json
from typing import Optional

from mini_agent.schema import Message, AgentState, Memory, Role
from mini_agent.llm import SimpleLLM
from mini_agent.tools import ToolCollection


class MiniAgent:
    """最小化智能代理实现"""
    
    def __init__(
        self, 
        llm: SimpleLLM,
        name: str = "MiniAgent",
        system_prompt: Optional[str] = None,
        max_steps: int = 10
    ):
        self.name = name
        self.llm = llm
        self.tools = ToolCollection()
        self.memory = Memory()
        self.state = AgentState.IDLE
        self.max_steps = max_steps
        self.current_step = 0
        
        # 默认系统提示词
        self.system_prompt = system_prompt or """
你是一个有用的AI助手，可以使用各种工具来帮助用户完成任务。

可用工具：
- python_execute: 执行Python代码
- file_editor: 读写文件和查看目录
- bash_execute: 执行命令行命令
- http_get: 抓取网页/接口的文本内容（可查实时信息）
- web_search: 联网搜索（结果的链接可用 http_get 打开正文）

请根据用户的需求，选择合适的工具来完成任务。每次只调用一个工具，然后根据结果决定下一步行动。
当已有信息足够回答用户时，请尽早给出最终答案，不要无休止地检索或尝试。

联网检索按以下流程：
① web_search 用简短关键词（2-6 个词，如「诺坎普球场 参观」而不是整句话）；
② 若结果明显不相关，换个关键词再搜一次；
③ 用 http_get 打开最相关的 1-2 个链接获取正文，不要逐个打开；
④ 信息足够后立即总结作答，并附上来源链接。
"""
    
    async def run(self, user_input: str) -> str:
        """执行用户请求"""
        print(f"\n🚀 {self.name} 开始执行任务: {user_input}")
        
        # 初始化
        self.state = AgentState.RUNNING
        self.current_step = 0
        
        # 添加用户消息到记忆
        self.memory.add_message(Message.user_message(user_input))
        
        # 执行循环
        while self.state == AgentState.RUNNING and self.current_step < self.max_steps:
            self.current_step += 1
            print(f"\n--- 第 {self.current_step} 步 ---")

            # 预算感知：步数将尽时督促收口，避免耗尽预算却没有最终答案
            # 说明：用 user 角色注入（部分接口要求 system 只能在开头），但加【系统提醒】标记，
            # 前端据此渲染为系统提示而非"用户提问"（用户反馈过这个显示问题）。
            remaining = self.max_steps - self.current_step
            if remaining == 2:
                self.memory.add_message(
                    Message.user_message(
                        "【系统提醒】只剩 2 步工具调用预算。若信息已大致够用，请立即总结作答，不要再打开新链接。"
                    )
                )
            elif remaining == 0:
                self.memory.add_message(
                    Message.user_message(
                        "【系统提醒】这是最后一步：请直接基于已有信息给出最终答案，不要再调用任何工具。"
                    )
                )
            
            # Think: 思考下一步行动
            should_continue = await self.think()
            if not should_continue:
                break
            
            # Act: 执行行动
            await self.act()
        
        self.state = AgentState.FINISHED
        result = self._generate_summary()
        print(f"\n✅ 任务完成! 总共执行了 {self.current_step} 步")
        return result
    
    async def think(self) -> bool:
        """思考阶段：分析当前状态，决定下一步行动"""
        print("🤔 正在思考...")
        
        try:
            # 获取LLM响应
            response = await self.llm.chat(
                messages=self.memory.get_messages(),
                system_prompt=self.system_prompt,
                tools=self.tools.get_tool_definitions()
            )
            
            print(f"💭 思考结果: {response.content}")
            
            # 保存助手消息（含思考模式的 reasoning_content，需回传给接口）
            self.memory.add_message(
                Message.assistant_message(
                    content=response.content,
                    tool_calls=response.tool_calls,
                    reasoning_content=getattr(response, "reasoning_content", None),
                )
            )
            
            # 检查是否需要调用工具
            if response.tool_calls:
                return True
            else:
                # 没有工具调用，任务可能已完成
                self.state = AgentState.FINISHED
                return False
                
        except Exception as e:
            print(f"❌ 思考过程出错: {e}")
            self.state = AgentState.FINISHED
            return False
    
    async def act(self) -> None:
        """行动阶段：执行工具调用"""
        print("⚡ 正在执行行动...")
        
        # 获取最后一条消息的工具调用
        last_message = self.memory.messages[-1]
        if not last_message.tool_calls:
            return
        
        # 执行所有工具调用
        for tool_call in last_message.tool_calls:
            tool_id = tool_call["id"]
            function_name = tool_call["function"]["name"]
            
            try:
                # 解析参数
                arguments = json.loads(tool_call["function"]["arguments"])
                print(f"🔧 执行工具: {function_name} with {arguments}")
                
                # 执行工具
                result = await self.tools.execute_tool(function_name, **arguments)
                
                # 准备结果消息
                if result.success:
                    result_content = result.output
                    print(f"✅ 工具执行成功: {result_content[:100]}...")
                else:
                    result_content = f"错误: {result.error}"
                    print(f"❌ 工具执行失败: {result.error}")
                
                # 保存工具结果
                self.memory.add_message(
                    Message.tool_message(
                        content=result_content,
                        tool_call_id=tool_id
                    )
                )
                
            except Exception as e:
                error_msg = f"工具执行异常: {str(e)}"
                print(f"❌ {error_msg}")
                self.memory.add_message(
                    Message.tool_message(
                        content=error_msg,
                        tool_call_id=tool_id
                    )
                )
    
    def _generate_summary(self) -> str:
        """生成任务执行摘要"""
        messages = self.memory.messages
        if not messages:
            return "没有执行任何操作"
        
        # 提取关键信息
        user_requests = [msg.content for msg in messages if msg.role == Role.USER]
        assistant_messages = [
            msg for msg in messages if msg.role == Role.ASSISTANT and msg.content
        ]
        last = assistant_messages[-1] if assistant_messages else None
        # 最后一条助手消息仍带工具调用 => 未给出最终作答（通常是步数耗尽）
        unfinished = bool(last and last.tool_calls)

        summary = f"""
任务执行摘要:
- 用户请求: {user_requests[0] if user_requests else '未知'}
- 执行步数: {self.current_step}
- 最终状态: {self.state.value}
- 主要响应: {last.content if last else '无响应'}
"""
        if unfinished:
            summary += (
                f"- 备注: 达到步数上限（{self.max_steps} 步）仍未给出最终作答，"
                "可调高步数或换个更具体的问法后重试\n"
            )
        return summary