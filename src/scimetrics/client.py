"""DeepSeek JSON 调用，仅 LangSmith 追踪，不写请求、响应或缓存文件。"""
import json
import os
from uuid import uuid4

from openai import OpenAI
from pydantic import BaseModel

from .observability import instrument_client, tracing_enabled


class DeepSeekClient:
    def __init__(self, model="deepseek-flash", max_tokens=16000, timeout=180):
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise ValueError("请设置 DEEPSEEK_API_KEY")
        self.model = model
        self.max_tokens = max_tokens
        self.api = instrument_client(OpenAI(
            api_key=api_key, base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            timeout=timeout, max_retries=2))

    def call(self, stage: str, prompt: str, content: list, schema: type[BaseModel]):
        system = prompt + "\n输出 JSON Schema：\n" + json.dumps(schema.model_json_schema(), ensure_ascii=False)
        extra = {"langsmith_extra": {"name": f"deepseek:{stage}", "run_id": str(uuid4()),
                 "metadata": {"stage": stage}}} if tracing_enabled() else {}
        response = self.api.chat.completions.create(
            model=self.model, messages=[{"role": "system", "content": system},
                                       {"role": "user", "content": content}],
            response_format={"type": "json_object"}, max_tokens=self.max_tokens, **extra)
        if not response.choices or response.choices[0].finish_reason != "stop":
            raise ValueError(f"{stage} 输出未完整结束，请调整 run.toml 中的 max_output_tokens 后重试")
        return schema.model_validate_json(response.choices[0].message.content or "")
