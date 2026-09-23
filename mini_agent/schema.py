"""
核心数据结构定义
"""
from enum import Enum
from typing import List, Optional, Dict, Any
from pydantic import BaseModel


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class AgentState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    FINISHED = "finished"


class Message(BaseModel):
    role: Role
    content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    reasoning_content: Optional[str] = None   # 思考模式模型的思维链，需随消息回传
    
    @classmethod
    def user_message(cls, content: str) -> "Message":
        return cls(role=Role.USER, content=content)
    
    @classmethod
    def assistant_message(
        cls,
        content: str,
        tool_calls: Optional[List[Dict]] = None,
        reasoning_content: Optional[str] = None,
    ) -> "Message":
        return cls(
            role=Role.ASSISTANT,
            content=content,
            tool_calls=tool_calls,
            reasoning_content=reasoning_content,
        )
    
    @classmethod
    def tool_message(cls, content: str, tool_call_id: str) -> "Message":
        return cls(role=Role.TOOL, content=content, tool_call_id=tool_call_id)


class Memory(BaseModel):
    messages: List[Message] = []
    
    def add_message(self, message: Message):
        self.messages.append(message)
    
    def get_messages(self) -> List[Dict[str, Any]]:
        """转换为OpenAI API格式（`content` 始终存在，空内容用空字符串）"""
        result = []
        for msg in self.messages:
            message_dict = {"role": msg.role.value, "content": msg.content or ""}
            if msg.tool_calls:
                message_dict["tool_calls"] = msg.tool_calls
            if msg.tool_call_id:
                message_dict["tool_call_id"] = msg.tool_call_id
            if msg.reasoning_content:
                message_dict["reasoning_content"] = msg.reasoning_content
            result.append(message_dict)
        return result