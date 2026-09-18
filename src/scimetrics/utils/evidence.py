"""证据来源校验与失败诊断；近似匹配只用于定位，不作为通过依据。"""

import difflib
import json
import re

_MATH = re.compile(r"\$\$.*?\$\$|(?<!\\)\$(?!\$)(?:\\.|[^$])*?(?<!\\)\$|\\\(.*?\\\)|\\\[.*?\\\]", re.S)


def normalize(value: str) -> str:
    # 只在有数学定界符的片段中忽略 LaTeX 命令与左花括号之间的空白。
    # 不删普通词间空格，不做公式等价变换，不改符号、数值、下标或标点。
    value = _MATH.sub(lambda match: re.sub(r"(\\[A-Za-z]+)\s+(?=\{)", r"\1", match.group()), value)
    return re.sub(r"\s+", " ", value).strip()


def whitespace_map(raw: str):
    """空白规范化文本到原始字符偏移的映射，仅用于诊断原文位置。"""
    chars, offsets = [], []
    for match in re.finditer(r"\S+", raw):
        if chars:
            chars.append(" ")
            offsets.append(match.start() - 1)
        chars.extend(match.group())
        offsets.extend(range(match.start(), match.end()))
    return "".join(chars), offsets


def nearest_excerpt(quote: str, raw: str, source_start: int, full_raw: str | None):
    query = re.sub(r"\s+", " ", quote).strip()
    source, offsets = whitespace_map(raw)
    if not query or not source:
        return None
    anchor = difflib.SequenceMatcher(None, query, source, autojunk=False).find_longest_match()
    if anchor.size < 8:
        return None
    left = max(0, anchor.b - anchor.a)
    right = min(len(source), left + len(query) + 80)
    candidate = source[left:right]
    matcher = difflib.SequenceMatcher(None, query, candidate, autojunk=False)
    blocks = [b for b in matcher.get_matching_blocks() if b.size]
    if blocks:
        # 去掉仅为搜索余量附带的后文，保留引句末尾可能替换的字符。
        last = blocks[-1]
        right = min(right, left + last.b + last.size + len(query) - last.a - last.size)
        candidate = source[left:right]
    a, b = offsets[left], offsets[max(left, right - 1)] + 1
    differences = []
    for tag, i, j, k, l in difflib.SequenceMatcher(None, query, candidate, autojunk=False).get_opcodes():
        if tag != 'equal':
            differences.append({'operation_quote_to_source': tag,
                                'quote_fragment': query[i:j][:200],
                                'source_fragment': candidate[k:l][:200]})
    line_base = full_raw[:source_start].count('\n') if full_raw is not None else 0
    return {'diagnostic_only': True,
            'source_start': source_start + a, 'source_end': source_start + b,
            'line_scope': 'markdown' if full_raw is not None else 'provided_source',
            'line_start': line_base + raw[:a].count('\n') + 1,
            'line_end': line_base + raw[:b].count('\n') + 1,
            'source_excerpt': raw[a:min(b, a + 1200)],
            'differences': differences[:8]}


class EvidenceValidationError(ValueError):
    def __init__(self, report):
        self.report = report
        super().__init__('证据来源校验失败（近似位置仅供诊断，不作为有效证据）：\n' +
                         json.dumps(report, ensure_ascii=False, indent=2))


def check_evidence(data, raw_content: str, parts: list, *, stage='unknown', source_path=None,
                   chunk=None, full_raw=None):
    original = normalize(raw_content)
    visual_urls = {p['image_url']['url'] for p in parts if p['type'] == 'image_url'}
    errors = []
    source_start = chunk['start'] if chunk else 0

    def visit(value, path='$', owner=None):
        if isinstance(value, dict):
            identity = {k: value[k] for k in ('indicator_id', 'name', 'subject_id', 'predicate', 'object_id') if k in value}
            owner = identity or owner
            if value.get('kind') in {'text', 'visual'} and 'quote' in value:
                quote = value['quote']
                reason = None
                if value['kind'] == 'text':
                    if not isinstance(quote, str) or not normalize(quote) or normalize(quote) not in original:
                        reason = 'text_quote_not_found'
                elif quote not in visual_urls:
                    reason = 'visual_url_not_supplied'
                elif not (value.get('observation') or '').strip():
                    reason = 'visual_observation_empty'
                if reason:
                    error = {'path': path, 'owner': owner, 'reason': reason,
                             'kind': value['kind'], 'quote': quote}
                    if value['kind'] == 'text' and isinstance(quote, str):
                        error['nearest_source'] = nearest_excerpt(quote, raw_content, source_start, full_raw)
                        if full_raw is not None and normalize(quote) and normalize(quote) in normalize(full_raw):
                            error['found_elsewhere_in_full_document'] = True
                    else:
                        error['supplied_image_urls'] = sorted(visual_urls)
                        error['observation'] = value.get('observation')
                    errors.append(error)
            for key, child in value.items():
                visit(child, f'{path}.{key}', owner)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f'{path}[{index}]', owner)

    visit(data)
    if errors:
        raise EvidenceValidationError({'stage': stage, 'source_file': source_path,
            'chunk': chunk, 'source_range': {'start': source_start, 'end': source_start + len(raw_content)},
            'error_count': len(errors), 'errors': errors})
