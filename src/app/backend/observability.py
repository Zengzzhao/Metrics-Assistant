"""问答 LangSmith 追踪；沿用 IE 的环境变量开关和项目配置。"""
import os

from langchain_core.tracers.langchain import wait_for_all_tracers
from langsmith.wrappers import wrap_openai


def tracing_enabled() -> bool:
    return os.getenv("LANGSMITH_TRACING", "false").lower() == "true"


def validate_tracing():
    if tracing_enabled() and not os.getenv("LANGSMITH_API_KEY"):
        raise ValueError("LANGSMITH_TRACING=true 时必须配置 LANGSMITH_API_KEY")
    os.environ.setdefault("LANGSMITH_PROJECT", "qa")


def instrument_client(client):
    if tracing_enabled():
        return wrap_openai(client, chat_name="DeepSeek QA", tracing_extra={
            "metadata": {"ls_provider": "deepseek"}, "tags": ["qa", "deepseek"]})
    return client


def flush_traces():
    if tracing_enabled():
        wait_for_all_tracers()
