"""DeepSeek JSON 调用，仅 LangSmith 追踪，不写请求、响应或缓存文件。"""
import json
import os
from uuid import uuid4

from openai import OpenAI
from pydantic import BaseModel

from .observability import instrument_client, tracing_enabled


class DeepSeekClient:
    def __init__(self, model="deepseek-flash", max_tokens=65536, timeout=600):
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise ValueError("请设置 DEEPSEEK_API_KEY")
        self.model = model
        self.max_tokens = max_tokens
        self.api = instrument_client(OpenAI(
            api_key=api_key, base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            timeout=timeout, max_retries=2))

    def call(self, stage: str, prompt: str, content: list, schema: type[BaseModel]):
        """
        stage: 用于追踪的阶段名称
        prompt: 系统提示词
        content: 用户问题内容
        schema: 用于验证 JSON 的 Pydantic 模型类
        """
        system = prompt + "\n输出 JSON Schema：\n" + json.dumps(schema.model_json_schema(), ensure_ascii=False)
        extra = {"langsmith_extra": {"name": f"deepseek:{stage}", "run_id": str(uuid4()),
                 "metadata": {"stage": stage}}} if tracing_enabled() else {}
        response = self.api.chat.completions.create(
            model=self.model, messages=[{"role": "system", "content": system},
                                       {"role": "user", "content": content}],
            response_format={"type": "json_object"}, max_tokens=self.max_tokens, **extra)
        reason = response.choices[0].finish_reason if response.choices else "no_choices"
        if reason != "stop":
            usage = response.usage.model_dump() if response.usage else None
            advice = ("输出达到 token 或上下文上限；可提高 max_output_tokens，或缩小输入/输出规模。"
                      if reason == "length" else "请根据 finish_reason 排查服务响应，增大 token 上限未必有效。")
            raise ValueError(
                f"{stage} 输出未完整结束：finish_reason={reason}, "
                f"max_output_tokens={self.max_tokens}, response_id={response.id}, "
                f"usage={json.dumps(usage, ensure_ascii=False)}。{advice}"
            )
        return schema.model_validate_json(response.choices[0].message.content or "")
