"""按章节发现指标、文内归并、分别抽取两类关系。"""

import json
from dataclasses import dataclass
from copy import deepcopy
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from . import prompts
from .utils.merge import validate_merge_plan
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


def build_graph(
    settings: Settings,
    client,
    *,
    checkpointer=None,
    interrupt_after=None,
    interrupt_before=None,
):
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
        return {"chunks": chunks, "skipped_sections": skipped, "chunk_discoveries": []}

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

    def validate(data, state, parts, stage, chunk=None, source_ranges=None):
        raw = state["raw_content"]
        scope = raw[chunk["start"] : chunk["end"]] if chunk else raw
        try:
            check_evidence(
                data,
                scope,
                parts,
                stage=stage,
                source_path=str(settings.input_path.resolve()),
                chunk=chunk,
                full_raw=raw,
                source_ranges=source_ranges,
            )
        except EvidenceValidationError as error:
            # 仅失败时保存完整响应和诊断；无请求正文、Base64 或密钥。
            path = settings.output_dir / "evidence_errors" / f"{stage}.json"
            write_json(path, {"report": error.report, "extraction": data})
            raise ValueError(
                f"{error}\n诊断报告与抽取结果：{path.resolve()}"
            ) from error

    def discover(state):
        records = state["chunk_discoveries"]
        chunks = state["chunks"]
        if [r["chunk_id"] for r in records] != [
            c["chunk_id"] for c in chunks[: len(records)]
        ]:
            raise ValueError("已保存的章节结果与章节清单不一致，不能继续发现")
        if len(records) == len(chunks):
            return {"chunk_discoveries": records}
        chunk = chunks[len(records)]
        parts = content(state, chunk)
        stage = "discover_" + chunk["chunk_id"]
        result = client.call(stage, prompts.DISCOVER, parts, Discovery)
        data = result.model_dump()
        validate(data, state, parts, stage, chunk)
        for index, indicator in enumerate(data["indicators"], 1):
            indicator["indicator_id"] = f"{chunk['chunk_id']}_I{index:03d}"
        record = {"chunk_id": chunk["chunk_id"], "title": chunk["title"], **data}
        # 不修改传入状态。只有请求、结构校验、证据校验全部成功才提交本章。
        return {"chunk_discoveries": [*records, record]}

    def after_discover(state):
        return (
            "discover_chunks"
            if len(state["chunk_discoveries"]) < len(state["chunks"])
            else "merge_indicators"
        )

    def merge(state):
        candidates = {
            i["indicator_id"]: {**i, "chunk_id": d["chunk_id"]}
            for d in state["chunk_discoveries"]
            for i in d["indicators"]
        }
        # 如果没有候选指标，直接返回空发现和空归并映射
        if not candidates:
            return {"discovery": {"indicators": []}, "merge_map": []}
        # 如果只有一个候选指标，直接返回该指标作为唯一发现，并生成单组归并映射
        if len(candidates) == 1:
            candidate = next(iter(candidates.values()))
            groups = [
                {
                    "candidate_ids": [candidate["indicator_id"]],
                    "canonical_name": candidate["name"],
                    "aliases": candidate["aliases"],
                    "reason": "唯一候选，无需归并判断",
                    "status": "confirmed",
                    "evidence_refs": [
                        {"candidate_id": candidate["indicator_id"], "evidence_index": 0}
                    ],
                    "context_evidence": [],
                }
            ]
        # 模型做首轮归并，模型输出confirmed、needs_context
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
        # 验证首轮归并结果
        validate_merge_plan(groups, candidates)

        # 对 needs_context 的组进行二次复核
        final_groups = []
        chunks_by_id = {c["chunk_id"]: c for c in state["chunks"]}
        for group_index, group in enumerate(groups):
            if group["status"] != "needs_context":
                final_groups.append({**group, "review": None})
                continue
            # 待复核组复核
            subset = {cid: candidates[cid] for cid in group["candidate_ids"]}
            chunk_ids = unique([c["chunk_id"] for c in subset.values()])
            # 为每个待复核组生成输入正文，包含候选指标和初步归并决定
            parts = [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "candidates": list(subset.values()),
                            "initial_decision": group,
                        },
                        ensure_ascii=False,
                    ),
                }
            ]
            supplied = {}
            # 为每个待复核组补充来源章节的全文和科学图片
            for chunk_id in chunk_ids:
                chunk = chunks_by_id[chunk_id]
                chunk_parts = content(state, chunk)
                supplied[chunk_id] = chunk_parts
                parts.append(
                    {
                        "type": "text",
                        "text": f"以下是来源章节 {chunk_id}：{chunk['title']}",
                    }
                )
                parts.extend(chunk_parts)
            stage = f"merge_review_{group_index + 1:03d}"
            # 调用模型做复核
            reviewed = client.call(
                stage, prompts.MERGE_REVIEW, parts, MergePlan
            ).model_dump()["groups"]
            # 验证复核结果
            validate_merge_plan(reviewed, subset, review=True)
            # 验证复核引用的补充证据确实来自提供过的真实原文，验证通过后再把复核结果和复核记录一起存入最终归并结果。
            for resolved in reviewed:
                for item in resolved["context_evidence"]:
                    chunk_id = item["chunk_id"]
                    if chunk_id not in supplied:
                        raise ValueError(f"{stage} 引用了未提供的章节 {chunk_id}")
                    validate(
                        item, state, supplied[chunk_id], stage, chunks_by_id[chunk_id]
                    )
                final_groups.append(
                    {
                        **resolved,
                        "review": {
                            "initial_decision": group,
                            "supplied_chunk_ids": chunk_ids,
                        },
                    }
                )
        groups = final_groups


        # 按候选 ID 排序，保证归并结果在不同运行中顺序一致，便于对比。
        groups.sort(key=lambda g: min(g["candidate_ids"]))
        # 遍历每个合并后的指标，生成最终指标清单和归并映射
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
                    "evidence": unique([e for m in members for e in m["evidence"]]),
                    "source_chunk_ids": unique([m["chunk_id"] for m in members]),
                    "candidate_ids": group["candidate_ids"],
                }
            )
            mapping.append({"indicator_id": iid, **group})
        return {"discovery": {"indicators": indicators}, "merge_map": mapping}

    def relation_parts(state, *, retained_only=False):
        if retained_only:
            parts = []
            for chunk in state["chunks"]:
                parts.extend(content(state, chunk))
        else:
            parts = content(state)
        parts.append(
            {
                "type": "text",
                "text": "固定指标清单（仅这些 ID 可作为关系端点）：\n"
                + json.dumps(state["discovery"], ensure_ascii=False),
            }
        )
        if retained_only and (
            sum(len(p["text"]) for p in parts if p["type"] == "text") > settings.max_chars
            or sum(p["type"] == "image_url" for p in parts) > settings.max_images
            or len(json.dumps(parts).encode()) > settings.max_body_bytes
        ):
            raise ValueError("PI 保留章节与指标清单的总输入超过资源限额；不会截断")
        return parts

    def pi_relation(state):
        if not state["discovery"]["indicators"]:
            return {"paper_relations": []}
        parts = relation_parts(state, retained_only=True)
        relations = client.call(
            "extract_pi_relation", prompts.EXTRACT_PI, parts, PaperRelations
        ).model_dump()["paper_relations"]
        known = {i["indicator_id"] for i in state["discovery"]["indicators"]}
        for index, relation in enumerate(relations):
            if relation["indicator_id"] not in known:
                raise ValueError(
                    f"paper_relations[{index}].indicator_id={relation['indicator_id']!r} "
                    f"不属于 merge 后的统一指标清单：{sorted(known)}"
                )
        assigned = [r["indicator_id"] for r in relations]
        if len(assigned) != len(set(assigned)):
            raise ValueError("PI 每个指标只能返回一条判定，不允许多关系或重复的无关系记录")
        missing = known - set(assigned)
        if missing:
            raise ValueError(f"PI 未覆盖全部指标，缺少：{sorted(missing)}；无关系也必须记录原因")
        order = {i["indicator_id"]: n for n, i in enumerate(state["discovery"]["indicators"])}
        relations.sort(key=lambda r: order[r["indicator_id"]])
        validate(
            relations, state, parts, "extract_pi_relation",
            source_ranges=[(c["start"], c["end"]) for c in state["chunks"]],
        )
        return {"paper_relations": relations}

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
        if previous != "discover_chunks":
            graph.add_edge(previous, name)
        previous = name
    graph.add_conditional_edges(
        "discover_chunks", after_discover, ["discover_chunks", "merge_indicators"]
    )
    graph.add_edge(previous, END)
    return graph.compile(
        checkpointer=checkpointer,
        interrupt_after=interrupt_after,
        interrupt_before=interrupt_before,
    )
