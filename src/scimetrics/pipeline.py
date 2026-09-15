"""仅在抽取成功时保存最终 JSON，其余数据在 State 中流转。"""

import json
from dataclasses import dataclass
from copy import deepcopy
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from . import prompts
from .document import (
    build_content,
    figure_context,
    image_part,
    find_images,
    ImageRecord,
    write_json,
)
from .models import Discovery, Extraction, ImageDecision


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
    discovery: dict
    extraction: dict


def build_graph(settings: Settings, client, *, checkpointer=None, interrupt_after=None):
    def prepare(state):
        source = settings.input_path.resolve()
        if source.suffix.lower() != ".md" or not source.is_file():
            raise ValueError("输入必须是已解析的 Markdown (.md) 文件")
        raw_content = source.read_text(encoding="utf-8")
        if not raw_content.strip():
            raise ValueError("Markdown 为空")
        if len(raw_content) > settings.max_chars:
            raise ValueError("全文超过 max_chars；不会截断")
        return {"raw_content": raw_content, "images": find_images(raw_content)}

    def classify(state):
        images = deepcopy(state["images"])
        for figure in images:
            context = figure_context(state["raw_content"], figure)
            parts = [
                {
                    "type": "text",
                    "text": (
                        f"图片 {figure['figure_id']}，alt={figure['alt']}\n{context}"
                    ),
                },
                image_part(figure),
            ]
            if len(json.dumps(parts).encode()) > settings.max_body_bytes:
                raise ValueError("图片分类输入超过请求体预算")
            figure["classification"] = client.call(
                "classify_" + figure["figure_id"],
                prompts.CLASSIFY,
                parts,
                ImageDecision,
            ).model_dump()
        return {"images": images}

    def content(state):
        return build_content(
            state["raw_content"], state["images"],
            settings.max_chars, settings.max_images, settings.max_body_bytes
        )

    def discover(state):
        result = client.call(
            "discover", prompts.DISCOVER, content(state), Discovery
        )
        ids = [i.indicator_id for i in result.indicators]
        if len(ids) != len(set(ids)):
            raise ValueError("发现阶段返回重复指标 ID")
        return {"discovery": result.model_dump()}

    def extract(state):
        parts = content(state)
        parts.append(
            {
                "type": "text",
                "text": "候选指标清单：\n"
                + json.dumps(state["discovery"], ensure_ascii=False),
            }
        )
        if len(json.dumps(parts).encode()) > settings.max_body_bytes:
            raise ValueError("全文及候选指标超过请求体预算")
        result = client.call("extract", prompts.EXTRACT, parts, Extraction)
        data = result.model_dump()
        write_json(settings.output_dir / "result.json", data)
        return {"extraction": data}

    graph = StateGraph(State)
    nodes = {
        "prepare_document": prepare,
        "classify_images": classify,
        "discover_indicators": discover,
        "extract_information": extract,
    }
    previous = START
    for name, node in nodes.items():
        graph.add_node(name, node)
        graph.add_edge(previous, name)
        previous = name
    graph.add_edge(previous, END)
    return graph.compile(checkpointer=checkpointer, interrupt_after=interrupt_after)
