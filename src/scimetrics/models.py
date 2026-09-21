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
    # 断言模式：explicit 表示原文明确表达该关系；inferred 表示结合多条事实推断，并非作者明确陈述。
    assertion_mode: Literal["explicit", "inferred"]
    # 证据
    evidence: list[Evidence] = Field(min_length=1)
    # 推断的摘要。inferred必须有简短依据摘要，explicit时可为null
    rationale_summary: str | None = None
    @model_validator(mode="after")
    def require_rationale(self):
        if self.assertion_mode == "inferred" and not (
            self.rationale_summary and self.rationale_summary.strip()
        ):
            raise ValueError("推断必须有简短依据摘要")
        return self


class PaperRelation(StrictModel):
    indicator_id: str = Field(min_length=1, description="merge 后固定指标清单中的统一 ID")
    predicate: Literal["PROPOSES", "MODIFIES", "APPLIES"] | None
    assertion_mode: Literal["explicit", "inferred"] | None
    evidence: list[Evidence]
    rationale_summary: str | None = None
    no_relation_reason: Literal["仅背景提及", "未实际应用", "证据不足"] | None
    no_relation_detail: str | None

    @model_validator(mode="after")
    def validate_decision(self):
        if self.predicate is None:
            if self.assertion_mode is not None:
                raise ValueError("无关系判定的 assertion_mode 必须为 null")
            if self.no_relation_reason is None or not (self.no_relation_detail and self.no_relation_detail.strip()):
                raise ValueError("无关系判定必须填写原因分类和具体说明")
            if self.no_relation_reason != "证据不足" and not self.evidence:
                raise ValueError("仅背景提及/未实际应用必须提供相关原文证据")
        else:
            if self.no_relation_reason is not None or self.no_relation_detail is not None:
                raise ValueError("有关系时无关系原因字段必须为 null")
            if self.assertion_mode is None or not self.evidence:
                raise ValueError("有关系必须提供 assertion_mode 和证据")
            if self.assertion_mode == "inferred" and not (self.rationale_summary and self.rationale_summary.strip()):
                raise ValueError("推断必须有简短依据摘要")
        return self


class IndicatorRelation(Grounded):
    # 列出推断依赖但原文未直接陈述的假设，使用中文；没有额外假设返回 []。假设不能替代缺失证据，不得用“假定本文首次提出”来建立 PROPOSES。
    assumptions: list[str] = Field(default_factory=list)
    # 关系适用的对象、变体、实验或评价场景；无需要限定的条件时返回 null，不凭空补充。
    scope: str | None = None
    subject_id: str
    predicate: Literal[
        "VARIANT_OF", "DERIVED_FROM", "IMPROVES_ON", "ALTERNATIVE_TO", "COMPONENT_OF"
    ]
    object_id: str


class PaperRelations(StrictModel):
    paper_relations: list[PaperRelation]


class IndicatorRelations(StrictModel):
    indicator_relations: list[IndicatorRelation]
