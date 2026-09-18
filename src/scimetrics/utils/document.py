"""保留 Markdown 原文和行号，解析图片，生成多模态消息。"""

import hashlib
import json
import re
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


# 解析markdown中实际图片语法并记录精确偏移
def find_images(raw_content: str) -> list[ImageRecord]:

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

# 将markdown按照h1/h2划分章节
def section_ranges(raw: str) -> list[dict]:
    offsets = [0]
    for line in raw.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    tokens = MarkdownIt("commonmark").parse(raw)
    headings = []
    for index, token in enumerate(tokens):
        if token.type == "heading_open" and token.tag in {"h1", "h2"} and token.map:
            headings.append({"title": tokens[index + 1].content,
                             "level": int(token.tag[1]),
                             "start": offsets[token.map[0]], "body_start": offsets[token.map[1]]})
    # 如果全文没有标题，或者第一个标题不是从开头开始，则插入一个虚拟前言章节。
    if not headings or headings[0]["start"] > 0:
        headings.insert(0, {"title": "Preamble", "level": 0, "start": 0, "body_start": 0})
    return [{**h, "end": headings[i+1]["start"] if i+1 < len(headings) else len(raw)}
            for i, h in enumerate(headings)]

# 排除不相关章节标题
def excluded_title(title: str) -> bool:
    text = re.sub(r"[*_`]+", "", title).casefold().strip()

    # 去掉常见章节编号：
    # 1 References
    # 1. References
    # 1.2 References
    # IV References
    # A References
    text = re.sub(
        r"^(?:(?:\d+(?:\.\d+)*|[ivxlcdm]+|[a-z])[.)]?\s+)",
        "",
        text,
    )

    # 将连字符、冒号等标点统一为空格
    # Conflict-of-Interest -> conflict of interest
    text = re.sub(r"[^\w]+", " ", text).strip()

    return bool(re.fullmatch(
        r"(?:"
        # References
        r"references?|"
        r"bibliography|"

        # Authors / affiliations
        r"authors?|"
        r"authors? and affiliations?|"
        r"author information|"
        r"author affiliations?|"
        r"affiliations?|"

        # Funding
        r"funding(?: information| statement| sources?)?|"
        r"financial support|"

        # Acknowledgements
        r"acknowledg(?:e)?ments?|"

        # Author contributions / CRediT
        r"(?:credit )?authors? contributions?(?: statement)?|"
        r"author contribution statement|"

        # Conflict / competing interests
        r"conflicts? of interests?(?: statement)?|"
        r"competing interests?(?: statement)?|"
        r"declarations? of (?:competing interests?|interests?)(?: statement)?|"
        r"declaration of interests?(?: statement)?|"

        # Disclosure
        r"disclosures?|"
        r"disclosure statement|"
        r"financial disclosures?|"

        # Ethics / consent
        r"ethics?(?: statement| approval)?|"
        r"ethical approval|"
        r"informed consent(?: statement)?|"
        r"consent for publication|"

        # Data / code availability
        r"data availability(?: statement)?|"
        r"availability of data(?: and materials)?|"
        r"code availability(?: statement)?|"
        r"data and code availability|"

        # Supplementary content
        r"supplementary materials?|"
        r"supplemental materials?|"
        r"supporting information|"

        # Misc declarations
        r"declarations?|"
        r"publisher s note"
        r")",
        text,
    ))

# 划分章节，排除空正文、不相关章节
def plan_sections(raw: str, images: list[ImageRecord]) -> tuple[list[dict], list[dict]]:
    chunks, skipped = [], []
    for index, section in enumerate(section_ranges(raw), 1):
        section = {**section, "chunk_id": f"C{index:04d}"}
        # 排除不相关章节chunk
        if excluded_title(section["title"]):
            skipped.append({**section, "reason": "excluded_section"})
            continue
        # 判断章节chunk里面是否有科学图片
        body = raw[section["body_start"]:section["end"]]
        scientific = False
        for figure in images:
            for occurrence in sorted(figure["occurrences"], key=lambda o: o["start"], reverse=True):
                if section["body_start"] <= occurrence["start"] < section["end"]:
                    scientific |= figure["classification"]["label"] == "scientific"
        # 临时移除当前章节chunk的图片，然后判断这个章节是否真正的文本内容；如果没有文本、也没有科学图片，就跳过这个章节。
        occurrences = sorted((o for f in images for o in f["occurrences"]
                              if section["body_start"] <= o["start"] < section["end"]),
                             key=lambda o: o["start"], reverse=True)
        for occurrence in occurrences:
            a, b = occurrence["start"]-section["body_start"], occurrence["end"]-section["body_start"]
            body = body[:a] + body[b:]
        body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
        body = re.sub(r"(?m)^\s*\[[^\]]+\]:[^\n]*$", "", body)
        if not body.strip() and not scientific:
            skipped.append({**section, "reason": "empty_body"})
        else:
            chunks.append(section)
    return chunks, skipped

# 把内容中的科学图片的图片语法，替换成“模型可接收的多模态图片输入”，返回一个多模态内容列表
def build_content(raw_content: str, images: list[ImageRecord], max_chars=250_000,
                  max_images=40, max_body_bytes=40_000_000, *, start=0, end=None) -> list:
    end = len(raw_content) if end is None else end
    if not 0 <= start <= end <= len(raw_content):
        raise ValueError("正文范围越界")
    if end - start > max_chars:
        raise ValueError("全文超过 max_chars；不会截断")
    if any(f["classification"] is None for f in images):
        raise ValueError("必须先完成图片分类")
    # 找出当前范围内所有图片
    occurrences = sorted((o["start"], o["end"], f) for f in images for o in f["occurrences"]
                         if start <= o["start"] < end)
    # 统计科学图片数量，超过 max_images 报错；decorative 图片不计入数量。
    count = sum(f["classification"]["label"] == "scientific" for _, _, f in occurrences)
    if count > max_images:
        raise ValueError("图片数量超过 max_images；不会静默丢弃")

    result = []
    cursor = start
    def append_text(a, b):
        if a < b:
            result.append({"type": "text", "text": raw_content[a:b]})

    for image_start, image_end, figure in occurrences:
        if not cursor <= image_start < image_end <= end:
            raise ValueError("图片位置重叠或越界")
        append_text(cursor, image_start)
        if figure["classification"]["label"] == "scientific":
            result.append(image_part(figure))
        # decorative：只跳过这一处图片语法，不删除邻近正文/图注。
        cursor = image_end
    append_text(cursor, end)

    if not result:
        result = [{"type": "text", "text": "本文过滤后没有可抽取内容。"}]
    if len(json.dumps(result).encode()) > max_body_bytes:
        raise ValueError("多模态输入超过请求体预算；不会截断")

    return result
