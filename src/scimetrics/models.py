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
    quote: str = Field(
        min_length=1, description="text: 原文连续引句；visual: 对应输入图片的完整 URL"
    )
    # 视觉证据描述原图区域及所见
    observation: str | None = Field(
        default=None, description="visual 描述原图区域及所见，不冒充原文"
    )

    # Pydantic把整个Evidence对象解析后，再做一次跨字段检查
    @model_validator(mode="after")
    def require_content(self):
        if not self.quote.strip():
            raise ValueError("quote 必须是非空原文引句或图片 URL")
        if self.kind == "visual" and not (
            self.observation and self.observation.strip()
        ):
            raise ValueError("视觉证据必须有 observation")
        return self


# 一个指标的schema
class Indicator(StrictModel):
    indicator_id: str
    name: str
    # 别名
    aliases: list[str] = Field(default_factory=list)
    # 定义
    definition: str | None = None
    # 每个指标至少必须有1个证据
    evidence: list[Evidence] = Field(min_length=1)


# ========指标发现节点输出========
class Discovery(StrictModel):
    indicators: list[Indicator]


class MergeEvidenceRef(StrictModel):
    candidate_id: str
    evidence_index: int = Field(ge=0, description="候选 evidence 数组下标，从 0 开始")


class MergeContextEvidence(StrictModel):
    chunk_id: str
    evidence: Evidence


# ======指标合并组========
class MergeGroup(StrictModel):
    # 每组合并得到的指标的候选来源指标
    candidate_ids: list[str] = Field(min_length=1)
    # 标准名
    canonical_name: str = Field(min_length=1)
    # 别名
    aliases: list[str] = Field(default_factory=list)
    # 组状态
    status: Literal["confirmed", "needs_context"]
    # 合并理由
    reason: str = Field(
        min_length=1, description="解释同一性、差异或尚缺的信息；不是思维过程"
    )
    # 当前组每个候选指标合并是依靠的该指标的那个证据（evidence_index为候选指标的证据数组下标）
    evidence_refs: list[MergeEvidenceRef] = Field(min_length=1)
    # 记录本次补充原文中用于判断的证据
    context_evidence: list[MergeContextEvidence] = Field(default_factory=list)


# =======指标合并节点输出========
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


class PaperRelation(Grounded):
    indicator_id: str
    predicate: Literal["PROPOSES", "MODIFIES", "APPLIES"]
    origin_status: (
        Literal["author_claimed", "citation_supported", "unresolved"] | None
    ) = None


class IndicatorRelation(Grounded):
    subject_id: str
    predicate: Literal[
        "VARIANT_OF", "DERIVED_FROM", "IMPROVES_ON", "ALTERNATIVE_TO", "COMPONENT_OF"
    ]
    object_id: str


class PaperRelations(StrictModel):
    paper_relations: list[PaperRelation]


class IndicatorRelations(StrictModel):
    indicator_relations: list[IndicatorRelation]
