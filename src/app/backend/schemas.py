from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


Predicate = Literal["PROPOSES", "MODIFIES", "APPLIES", "VARIANT_OF", "DERIVED_FROM",
                    "IMPROVES_ON", "ALTERNATIVE_TO", "COMPONENT_OF"]


class ToolCall(Strict):
    # 执行那种查询
    tool: Literal["indicator_details", "relations", "no_relation"]
    # 查询那些指标
    indicator_ids: list[str] = Field(default_factory=list, max_length=8)
    # 查询那些关系类型
    predicates: list[Predicate] = Field(default_factory=list, max_length=8)


class Plan(Strict):
    explanation: str = Field(max_length=500)
    clarification: str | None = Field(default=None, max_length=500)
    calls: list[ToolCall] = Field(default_factory=list, max_length=3)


class Claim(Strict):
    text: str = Field(min_length=1, max_length=1800)
    assertion_mode: Literal["explicit", "inferred", "graph_record"]
    fact_ids: list[str] = Field(min_length=1, max_length=20)
    citation_ids: list[str] = Field(min_length=1, max_length=30)


class Answer(Strict):
    # 澄清问题、空查询结论或回答范围说明，不伪造事实与证据引用。
    message: str = Field(default="", max_length=1500)
    claims: list[Claim] = Field(default_factory=list, max_length=30)


class HistoryTurn(Strict):
    question: str = Field(min_length=1, max_length=2000)
    answer: Answer


class ChatRequest(Strict):
    question: str = Field(min_length=1, max_length=2000)
    paper_id: str = Field(min_length=1, max_length=300)
    history: list[HistoryTurn] = Field(default_factory=list)
