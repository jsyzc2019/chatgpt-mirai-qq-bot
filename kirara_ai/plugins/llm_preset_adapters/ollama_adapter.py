import asyncio

import aiohttp
import requests
from pydantic import BaseModel, ConfigDict
from typing import Optional

from kirara_ai.llm.adapter import AutoDetectModelsProtocol, LLMBackendAdapter
from kirara_ai.llm.format.message import LLMChatImageContent, LLMChatTextContent, LLMToolResultContent, LLMToolCallContent
from kirara_ai.llm.format.request import LLMChatRequest
from kirara_ai.llm.format.response import LLMChatResponse, Message, Usage, ToolCall, Function
from kirara_ai.logger import get_logger
from kirara_ai.media.manager import MediaManager
from kirara_ai.tracing import trace_llm_chat


def convert_llm_response(response_data: dict[str, dict]):
    # 无法得知tool_call时content是否为空，因此两个都记录下来
    content = [LLMChatTextContent(text=response_data["message"].get("content", ""))]
    calls = []
    if response_data["message"].get("tool_calls", None):
        for tool_call in response_data["message"]["tool_calls"]:
            tool_call: dict[str, dict] # 类型标注，运行时将被忽略
            calls.append(
                LLMToolCallContent(
                    name=tool_call["function"]["name"],
                    parameters=tool_call["function"].get("arguments", None)
                )
            )
        content.extend(calls)
    return content

def resolve_tool_calls_form_content(response_data: dict[str, dict]) -> Optional[list[ToolCall]]:
    if tool_calls := response_data["message"].get("tool_calls", None):
        calls =[]
        for call in tool_calls:
            call: dict[str, dict] # 类型标注，运行时将被忽略
            calls.append(ToolCall(
                function=Function(
                    name = call["function"]["name"], 
                    arguments = call["function"].get("arguments", None),
                )
            ))
    else:
        return None

class OllamaConfig(BaseModel):
    api_base: str = "http://localhost:11434"
    model_config = ConfigDict(frozen=True)

async def resolve_media_ids(media_ids: list[str], media_manager: MediaManager) -> list[str]:
    return [await media_manager.get_media(media_id).get_base64() for media_id in media_ids]

class OllamaAdapter(LLMBackendAdapter, AutoDetectModelsProtocol):
    def __init__(self, config: OllamaConfig):
        self.config = config
        self.logger = get_logger("OllamaAdapter")
        
    @trace_llm_chat
    def chat(self, req: LLMChatRequest) -> LLMChatResponse:
        api_url = f"{self.config.api_base}/api/chat"
        headers = {"Content-Type": "application/json"}

        # 将消息转换为 Ollama 格式
        messages = []
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        for msg in req.messages:
            # 收集每条消息中的文本内容和图像
            if msg.role == "tool":
                messages.extend([{"role": "tool", "content": part.content, "name": part.name} for part in msg.content])
            else:
                text_content = ""
                images = []
                for part in msg.content:
                    if isinstance(part, LLMChatTextContent):
                        text_content += part.text
                    elif isinstance(part, LLMChatImageContent):
                        images.append(part.media_id)
                    elif isinstance(part, LLMToolCallContent):
                        continue
                    #TODO: 创建 Ollama 格式的消息
                    message = {"role": msg.role, "content": text_content}
                    if images:
                        messages["images"] = loop.run_until_complete(resolve_media_ids(images, self.media_manager))
                    messages.append(message)

        data = {
            "model": req.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": req.temperature,
                "top_p": req.top_p,
                "num_predict": req.max_tokens,
                "stop": req.stop,
            "tools": [tool.model_dump() for tool in req.tools] if req.tools else None,
            },
        }

        # Remove None fields
        data = {k: v for k, v in data.items() if v is not None}
        if "options" in data:
            data["options"] = {
                k: v for k, v in data["options"].items() if v is not None
            }

        response = requests.post(api_url, json=data, headers=headers)
        try:
            response.raise_for_status()
            response_data = response.json()
        except Exception as e:
            print(f"API Response: {response.text}")
            raise e
        # https://github.com/ollama/ollama/blob/main/docs/api.md#generate-a-chat-completions
        return LLMChatResponse(
            model=req.model,
            message=Message(
                content= convert_llm_response(response_data),
                role="assistant",
                finish_reason="stop",
                tool_calls= resolve_tool_calls_form_content(response_data),
            ),
            usage=Usage(
                prompt_tokens=response_data['prompt_eval_count'],
                completion_tokens=response_data['eval_count'],
                total_tokens=response_data['prompt_eval_count'] + response_data['eval_count'],
            )
        )

    async def auto_detect_models(self) -> list[str]:
        api_url = f"{self.config.api_base}/api/tags"
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.get(api_url) as response:
                response.raise_for_status()
                response_data = await response.json()
                return [tag["name"] for tag in response_data["models"]]