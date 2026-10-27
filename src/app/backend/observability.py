"""问答 LangSmith 追踪；共用开关和凭据，使用独立的问答项目。"""
import os

from langchain_core.tracers.langchain import wait_for_all_tracers
from langsmith.wrappers import wrap_openai


def tracing_enabled() -> bool:
    return os.getenv("LANGSMITH_TRACING", "false").lower() == "true"


def validate_tracing():
    if tracing_enabled() and not os.getenv("LANGSMITH_API_KEY"):
        raise ValueError("LANGSMITH_TRACING=true 时必须配置 LANGSMITH_API_KEY")
    # LangGraph 与 wrap_openai 均读取标准变量；仅在问答后端进程内设置。
    os.environ["LANGSMITH_PROJECT"] = os.getenv("QA_LANGSMITH_PROJECT", "").strip() or "qa"


def instrument_client(client):
    if tracing_enabled():
        return wrap_openai(client, chat_name="DeepSeek QA", tracing_extra={
            "metadata": {"ls_provider": "deepseek"}, "tags": ["qa", "deepseek"]})
    return client


def flush_traces():
    if tracing_enabled():
        wait_for_all_tracers()
