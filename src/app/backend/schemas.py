from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatRequest(Strict):
    question: str = Field(min_length=1, max_length=2000)
    paper_id: str = Field(min_length=1, max_length=300)


Predicate = Literal["PROPOSES", "MODIFIES", "APPLIES", "VARIANT_OF", "DERIVED_FROM",
                    "IMPROVES_ON", "ALTERNATIVE_TO", "COMPONENT_OF"]


class ToolCall(Strict):
    tool: Literal["indicator_details", "relations", "no_relation"]
    indicator_ids: list[str] = Field(default_factory=list, max_length=8)
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
    status: Literal["answered", "insufficient", "clarification"]
    claims: list[Claim] = Field(default_factory=list, max_length=30)
    limitation: str = Field(default="", max_length=1500)
    follow_up: str | None = Field(default=None, max_length=500)
