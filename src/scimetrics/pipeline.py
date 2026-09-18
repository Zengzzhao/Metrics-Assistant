"""按章节发现指标、文内归并、分别抽取两类关系。"""

import json
from dataclasses import dataclass
from copy import deepcopy
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from . import prompts
from .utils.evidence import EvidenceValidationError, check_evidence
from .utils.document import (
    build_content,
    image_part,
    find_images,
    ImageRecord,
    plan_sections,
    write_json,
)
from .models import (
    Discovery,
    ImageDecision,
    MergePlan,
    PaperRelations,
    IndicatorRelations,
)


@dataclass
class Settings:
    input_path: Path
    output_dir: Path
    max_chars: int = 250_000
    max_images: int = 40
    max_body_bytes: int = 40_000_000


class State(TypedDict, total=False):
    raw_content: str
    images: list[ImageRecord]
    chunks: list[dict]
    skipped_sections: list[dict]
    chunk_discoveries: list[dict]
    discovery: dict
    merge_map: list[dict]
    paper_relations: list[dict]
    indicator_relations: list[dict]
    extraction: dict


def unique(values):
    result, seen = [], set()
    for value in values:
        key = json.dumps(value, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def build_graph(settings: Settings, client, *, checkpointer=None, interrupt_after=None):
    def prepare(state):
        source = settings.input_path.resolve()
        if source.suffix.lower() != ".md" or not source.is_file():
            raise ValueError("输入必须是已解析的 Markdown (.md) 文件")
        raw = source.read_text(encoding="utf-8")
        if not raw.strip():
            raise ValueError("Markdown 为空")
        return {"raw_content": raw, "images": find_images(raw)}

    def classify(state):
        images = deepcopy(state["images"])
        for figure in images:
            figure["classification"] = client.call(
                "classify_" + figure["figure_id"],
                prompts.CLASSIFY,
                [image_part(figure)],
                ImageDecision,
            ).model_dump()
        return {"images": images}

    def plan(state):
        chunks, skipped = plan_sections(state["raw_content"], state["images"])
        return {"chunks": chunks, "skipped_sections": skipped}

    def content(state, chunk=None):
        bounds = {"start": chunk["start"], "end": chunk["end"]} if chunk else {}
        return build_content(
            state["raw_content"],
            state["images"],
            settings.max_chars,
            settings.max_images,
            settings.max_body_bytes,
            **bounds,
        )

    def validate(data, state, parts, stage, chunk=None):
        raw = state["raw_content"]
        scope = raw[chunk["start"]:chunk["end"]] if chunk else raw
        try:
            check_evidence(data, scope, parts, stage=stage,
                           source_path=str(settings.input_path.resolve()),
                           chunk=chunk, full_raw=raw)
        except EvidenceValidationError as error:
            # 仅失败时保存完整响应和诊断；无请求正文、Base64 或密钥。
            path = settings.output_dir / "evidence_errors" / f"{stage}.json"
            write_json(path, {"report": error.report, "extraction": data})
            raise ValueError(f"{error}\n诊断报告与抽取结果：{path.resolve()}") from error

    def discover(state):
        records = []
        # 每个章节chunk单独发现指标
        for chunk in state["chunks"]:
            parts = content(state, chunk)
            result = client.call(
                "discover_" + chunk["chunk_id"], prompts.DISCOVER, parts, Discovery
            )
            data = result.model_dump()
            validate(data, state, parts, "discover_" + chunk["chunk_id"], chunk)
            # 指标的编号由程序统一分配为Cxxx_Ixxx，避免不同章节都返回 I001 时冲突。
            for index, indicator in enumerate(data["indicators"], 1):
                indicator["indicator_id"] = f"{chunk['chunk_id']}_I{index:03d}"
            records.append(
                {"chunk_id": chunk["chunk_id"], "title": chunk["title"], **data}
            )
        return {"chunk_discoveries": records}

    def merge(state):
        candidates = {
            i["indicator_id"]: {**i, "chunk_id": d["chunk_id"]}
            for d in state["chunk_discoveries"]
            for i in d["indicators"]
        }
        if not candidates:
            return {"discovery": {"indicators": []}, "merge_map": []}
        if len(candidates) == 1:
            candidate = next(iter(candidates.values()))
            groups = [
                {
                    "candidate_ids": [candidate["indicator_id"]],
                    "canonical_name": candidate["name"],
                    "aliases": candidate["aliases"],
                    "reason": "唯一候选，无需归并判断",
                }
            ]
        else:
            parts = [
                {
                    "type": "text",
                    "text": json.dumps(list(candidates.values()), ensure_ascii=False),
                }
            ]
            groups = client.call(
                "merge_indicators", prompts.MERGE, parts, MergePlan
            ).model_dump()["groups"]
        assigned = [cid for group in groups for cid in group["candidate_ids"]]
        if len(assigned) != len(set(assigned)) or set(assigned) != set(candidates):
            raise ValueError(
                "归并结果必须恰好覆盖每个候选一次，不得遗漏、重复或新增候选"
            )
        groups.sort(key=lambda g: min(g["candidate_ids"]))
        indicators, mapping = [], []
        for index, group in enumerate(groups, 1):
            members = [candidates[cid] for cid in group["candidate_ids"]]
            iid = f"I{index:03d}"
            name = group["canonical_name"].strip()
            if not name:
                raise ValueError("规范指标名称不能为空")
            # 证据由程序从原候选合并，归并模型无权改写原句。
            indicators.append(
                {
                    "indicator_id": iid,
                    "name": name,
                    "aliases": unique(
                        [
                            a
                            for a in group["aliases"]
                            + [m["name"] for m in members]
                            + [a for m in members for a in m["aliases"]]
                            if a != name
                        ]
                    ),
                    "definitions": unique(
                        [m["definition"] for m in members if m["definition"]]
                    ),
                    "evaluation_objects": unique(
                        [
                            m["evaluation_object"]
                            for m in members
                            if m["evaluation_object"]
                        ]
                    ),
                    "evidence": unique([e for m in members for e in m["evidence"]]),
                    "source_chunk_ids": unique([m["chunk_id"] for m in members]),
                    "candidate_ids": group["candidate_ids"],
                }
            )
            mapping.append({"indicator_id": iid, **group})
        return {"discovery": {"indicators": indicators}, "merge_map": mapping}

    def relation_parts(state):
        parts = content(state)
        parts.append(
            {
                "type": "text",
                "text": "固定指标清单（仅这些 ID 可作为关系端点）：\n"
                + json.dumps(state["discovery"], ensure_ascii=False),
            }
        )
        return parts

    def pi_relation(state):
        if not state["discovery"]["indicators"]:
            return {"paper_relations": []}
        parts = relation_parts(state)
        relations = client.call(
            "extract_pi_relation", prompts.EXTRACT_PI, parts, PaperRelations
        ).model_dump()["paper_relations"]
        known = {i["indicator_id"] for i in state["discovery"]["indicators"]}
        if any(r["indicator_id"] not in known for r in relations):
            raise ValueError("论文关系引用未知指标 ID")
        validate(relations, state, parts, "extract_pi_relation")
        return {"paper_relations": unique(relations)}

    def ii_relation(state):
        relations = []
        known = {i["indicator_id"] for i in state["discovery"]["indicators"]}
        if len(known) >= 2:
            parts = relation_parts(state)
            relations = client.call(
                "extract_ii_relation", prompts.EXTRACT_II, parts, IndicatorRelations
            ).model_dump()["indicator_relations"]
            if any(
                r["subject_id"] not in known
                or r["object_id"] not in known
                or r["subject_id"] == r["object_id"]
                for r in relations
            ):
                raise ValueError("指标关系端点未知或存在自关系")
            validate(relations, state, parts, "extract_ii_relation")
        relations = unique(relations)
        data = {
            "status": "extracted_unverified",
            "indicators": state["discovery"]["indicators"],
            "merge_map": state["merge_map"],
            "paper_relations": state["paper_relations"],
            "indicator_relations": relations,
            "chunks": state["chunks"],
            "skipped_sections": state["skipped_sections"],
        }
        write_json(settings.output_dir / "result.json", data)
        return {"indicator_relations": relations, "extraction": data}

    graph = StateGraph(State)
    nodes = {
        "prepare_document": prepare,
        "classify_images": classify,
        "plan_chunks": plan,
        "discover_chunks": discover,
        "merge_indicators": merge,
        "extract_pi_relation": pi_relation,
        "extract_ii_relation": ii_relation,
    }
    previous = START
    for name, node in nodes.items():
        graph.add_node(name, node)
        graph.add_edge(previous, name)
        previous = name
    graph.add_edge(previous, END)
    return graph.compile(checkpointer=checkpointer, interrupt_after=interrupt_after)
