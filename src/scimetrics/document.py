"""保留 Markdown 原文和行号，解析图片，生成多模态消息。"""

import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from typing import TypedDict

from markdown_it import MarkdownIt


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class HTMLImages(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "img":
            data = dict(attrs)
            if data.get("src"):
                self.sources.append((data["src"], data.get("alt", "")))


def validate_image_url(src: str) -> str:
    parsed = urlsplit(src)
    if (parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or len(src) > 8192):
        raise ValueError("图片必须使用有效 HTTP(S) URL；不支持本地路径或 Base64，请先托管图片并替换 Markdown 链接")
    return src


class ImageOccurrence(TypedDict):
    start: int  # Python 字符偏移，含起点、不含终点，基于 raw_content
    end: int


class ImageRecord(TypedDict):
    figure_id: str
    url: str
    alt: str
    occurrences: list[ImageOccurrence]
    classification: dict | None


def find_images(raw_content: str) -> list[ImageRecord]:
    """解析实际图片语法并记录精确偏移；同 URL 分类一次，保留每次出现位置。"""
    from markdown_it.rules_inline import image, html_inline

    parser = MarkdownIt("commonmark", {"html": True}).enable("table")
    env = {}
    tokens = parser.parse(raw_content, env)
    # 遮蔽 fenced/indented code，保持字符位置；行内代码由 inline parser 跳过。
    offsets = [0]
    for line in raw_content.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    masked = list(raw_content)
    for token in tokens:
        if token.type in {"fence", "code_block"} and token.map:
            start, end = (offsets[i] for i in token.map)
            masked[start:end] = ["\n" if c == "\n" else " " for c in raw_content[start:end]]
    found = []
    source = "".join(masked)

    def track_image(state, silent):
        start = state.pos
        matched = image(state, silent)
        if matched and not silent and state.src == source:
            token = state.tokens[-1]
            found.append((start, state.pos, token.attrGet("src"), token.content))
        return matched

    def track_html(state, silent):
        start = state.pos
        matched = html_inline(state, silent)
        if matched and not silent and state.src == source:
            value = state.src[start:state.pos]
            html = HTMLImages()
            html.feed(value)
            for src, alt in html.sources:
                found.append((start, state.pos, src, alt))
        return matched

    parser.inline.ruler.at("image", track_image)
    parser.inline.ruler.at("html_inline", track_html)
    # 直接解析原始全文而非规范化后的段落，偏移始终对应 raw_content。
    parser.inline.parse(source, parser, env, [])
    images = []
    by_url = {}
    for start, end, src, alt in sorted(found):
        url = validate_image_url(src)
        if url not in by_url:
            record = {"figure_id": f"F{len(images)+1:04d}", "url": url, "alt": alt,
                      "occurrences": [], "classification": None}
            images.append(record)
            by_url[url] = record
        by_url[url]["occurrences"].append({"start": start, "end": end})
    return images


def image_part(figure: ImageRecord) -> dict:
    return {"type": "image_url", "image_url": {"url": validate_image_url(figure["url"])}}


def figure_context(raw_content: str, figure: ImageRecord, radius: int = 500) -> str:
    return "\n…\n".join(
        raw_content[max(0, item["start"]-radius):item["end"]+radius]
        for item in figure["occurrences"]
    )


def build_content(raw_content: str, images: list[ImageRecord], max_chars=250_000,
                  max_images=40, max_body_bytes=40_000_000) -> list:
    """按精确原文位置替换图片；不修改 raw_content，不在 state 保存正文副本。"""
    if len(raw_content) > max_chars:
        raise ValueError("全文超过 max_chars；不会截断")
    if any(f["classification"] is None for f in images):
        raise ValueError("必须先完成图片分类")
    occurrences = sorted((o["start"], o["end"], f) for f in images for o in f["occurrences"])
    count = sum(f["classification"]["label"] == "scientific" for _, _, f in occurrences)
    if count > max_images:
        raise ValueError("图片数量超过 max_images；不会静默丢弃")
    result = []
    cursor = 0
    text_index = 0

    def append_text(text):
        nonlocal text_index
        if text:
            text_index += 1
            result.append({"type": "text", "text": f"[B{text_index:06d}]\n{text}"})

    for start, end, figure in occurrences:
        if not cursor <= start < end <= len(raw_content):
            raise ValueError("图片位置重叠或越界")
        append_text(raw_content[cursor:start])
        if figure["classification"]["label"] == "scientific":
            result.append({"type": "text", "text": f"[{figure['figure_id']}]"})
            result.append(image_part(figure))
        # decorative：只跳过这一处图片语法，不删除邻近正文/图注。
        cursor = end
    append_text(raw_content[cursor:])
    if not result:
        result = [{"type": "text", "text": "本文过滤后没有可抽取内容。"}]
    if len(json.dumps(result).encode()) > max_body_bytes:
        raise ValueError("多模态输入超过请求体预算；不会截断")
    return result
