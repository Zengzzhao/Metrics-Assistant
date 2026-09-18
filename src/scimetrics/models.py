"""固定领域 schema；语义验证留给后续 EV 模块。"""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

# ========图片二分类节点输出========
class ImageDecision(StrictModel):
    label: Literal["scientific", "decorative"]
    reason: str

# 一条证据的schema
class Evidence(StrictModel):
    kind: Literal["text", "visual"]
    # 文字证据保存原句；视觉证据保存实际输入图片的 URL。
    quote: str = Field(min_length=1, description="text: 原文连续引句；visual: 对应输入图片的完整 URL")
    # 视觉证据描述原图区域及所见
    observation: str | None = Field(default=None, description="visual 描述原图区域及所见，不冒充原文")

    # Pydantic把整个Evidence对象解析后，再做一次跨字段检查
    @model_validator(mode="after")
    def require_content(self):
        if not self.quote.strip():
            raise ValueError("quote 必须是非空原文引句或图片 URL")
        if self.kind == "visual" and not (self.observation and self.observation.strip()):
            raise ValueError("视觉证据必须有 observation")
        return self

# 一个指标的schema
class Indicator(StrictModel):
    indicator_id: str
    name: str
    # 别名
    aliases: list[str] = Field(default_factory=list)
    definition: str | None = None
    # 每个指标至少必须有1个证据
    evidence: list[Evidence] = Field(min_length=1)

# ========指标发现节点输出========
class Discovery(StrictModel):
    indicators: list[Indicator]


class MergeGroup(StrictModel):
    candidate_ids: list[str] = Field(min_length=1)
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    reason: str = Field(description="合并依据；不确定是否相同则各自单独分组")


class MergePlan(StrictModel):
    groups: list[MergeGroup]


class Grounded(StrictModel):
    assertion_mode: Literal["explicit", "inferred"]
    evidence: list[Evidence] = Field(min_length=1)
    rationale_summary: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    scope: str | None = None

    @model_validator(mode="after")
    def require_rationale(self):
        if self.assertion_mode == "inferred" and not (
            self.rationale_summary and self.rationale_summary.strip()
        ):
            raise ValueError("推断必须有简短依据摘要")
        return self


class Formula(StrictModel):
    indicator_id: str
    latex_raw: str
    equation_label: str | None = None
    variables: dict[str, str] = Field(default_factory=dict)
    conditions: str | None = None
    evidence: list[Evidence] = Field(min_length=1)


class Limitation(Grounded):
    indicator_id: str
    summary: str
    attribution: Literal["current_authors", "cited_authors", "model_inference"]


class PaperRelation(Grounded):
    indicator_id: str
    predicate: Literal["PROPOSES", "MODIFIES", "APPLIES"]
    origin_status: Literal["author_claimed", "citation_supported", "unresolved"] | None = None


class IndicatorRelation(Grounded):
    subject_id: str
    predicate: Literal["VARIANT_OF", "DERIVED_FROM", "IMPROVES_ON", "ALTERNATIVE_TO", "COMPONENT_OF"]
    object_id: str


class PaperRelations(StrictModel):
    paper_relations: list[PaperRelation]


class IndicatorRelations(StrictModel):
    indicator_relations: list[IndicatorRelation]

# ========信息节点输出========
class Extraction(StrictModel):
    indicators: list[Indicator]
    formulas: list[Formula]
    reported_limitations: list[Limitation]
    inferred_limitations: list[Limitation]
    paper_relations: list[PaperRelation]
    indicator_relations: list[IndicatorRelation]

    @model_validator(mode="after")
    def validate_references(self):
        ids = [i.indicator_id for i in self.indicators]
        if len(ids) != len(set(ids)):
            raise ValueError("指标 ID 重复")
        known = set(ids)
        for group in (self.formulas, self.reported_limitations, self.inferred_limitations,
                      self.paper_relations):
            for item in group:
                if item.indicator_id not in known:
                    raise ValueError(f"未知指标 ID: {item.indicator_id}")
        for rel in self.indicator_relations:
            if rel.subject_id not in known or rel.object_id not in known:
                raise ValueError("指标关系引用未知实体")
            if rel.subject_id == rel.object_id:
                raise ValueError("指标不能与自己建立目标关系")
        for item in self.reported_limitations:
            if item.assertion_mode != "explicit" or item.attribution == "model_inference":
                raise ValueError("reported_limitations 只能是文献明示评价")
        for item in self.inferred_limitations:
            if item.assertion_mode != "inferred" or item.attribution != "model_inference":
                raise ValueError("inferred_limitations 必须标记模型推断")
        return self
