"""证据来源校验与失败诊断；近似匹配只用于定位，不作为通过依据。"""

import difflib
import json
import re

_TYPOGRAPHY = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "‐": "-", "‑": "-",
                            "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl"})
_LAYOUT_BOUNDARIES = set("/-$\\{}_ ^()[]".replace(" ", ""))


def normalized_map(raw: str):
    """有限排版规范化，并保留每个规范字符对应的原始起止位置。"""
    chars, spans = [], []
    for index, char in enumerate(raw):
        for replacement in char.translate(_TYPOGRAPHY):
            if replacement.isspace():
                if chars and chars[-1] == " ":
                    spans[-1] = (spans[-1][0], index + 1)
                else:
                    chars.append(" ")
                    spans.append((index, index + 1))
            else:
                chars.append(replacement)
                spans.append((index, index + 1))
    kept = []
    for i, char in enumerate(chars):
        if char == " " and (i == 0 or i == len(chars) - 1
                or chars[i - 1] in _LAYOUT_BOUNDARIES or chars[i + 1] in _LAYOUT_BOUNDARIES):
            continue
        kept.append(i)
    return "".join(chars[i] for i in kept), [spans[i] for i in kept]


def locate_quote(quote: str, raw: str):
    """返回匹配方式和所有原文位置；不以相似度决定通过。"""
    if not isinstance(quote, str) or not quote.strip():
        return None
    exact = [(m.start(), m.end()) for m in re.finditer(re.escape(quote), raw)]
    if exact:
        return "exact", exact
    query, _ = normalized_map(quote)
    source, offsets = normalized_map(raw)
    if not query:
        return None
    spans = [(offsets[m.start()][0], offsets[m.end() - 1][1])
             for m in re.finditer(re.escape(query), source)]
    # 禁止在展开的 Unicode 字形中间截断匹配。
    spans = [span for span in spans if normalized_map(raw[span[0]:span[1]])[0] == query]
    return ("normalized", spans) if spans else None


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
                   chunk=None, full_raw=None, source_ranges=None):
    visual_urls = {p['image_url']['url'] for p in parts if p['type'] == 'image_url'}
    errors = []
    source_start = chunk['start'] if chunk else 0

    def visit(value, path='$', owner=None):
        if isinstance(value, dict):
            identity = {k: value[k] for k in ('indicator_id', 'name', 'subject_id', 'predicate', 'object_id') if k in value}
            owner = {**identity, **{k: value[k] for k in ('definition', 'assertion_mode', 'rationale_summary', 'scope') if k in value}} if identity else owner
            if value.get('kind') in {'text', 'visual'} and 'quote' in value:
                quote = value['quote']
                reason = None
                if value['kind'] == 'text':
                    if source_ranges is None:
                        match = locate_quote(quote, raw_content)
                    else:
                        # 各章节独立匹配，禁止引用已过滤章节或拼接跨章节引句。
                        matches = []
                        for left, right in source_ranges:
                            located = locate_quote(quote, raw_content[left:right])
                            if located:
                                method, spans = located
                                matches.extend((method, left + a, left + b) for a, b in spans)
                        method = "exact" if any(m[0] == "exact" for m in matches) else "normalized"
                        match = (method, [(a, b) for kind, a, b in matches if kind == method]) if matches else None
                    if match is None:
                        reason = 'text_quote_not_found'
                    else:
                        method, spans = match
                        # 同一引句重复出现时保留所有位置，不臆断唯一来源。
                        start, end = spans[0]
                        value.update(quote=raw_content[start:end], extracted_quote=quote,
                                     match_method=method, source_file=source_path,
                                     source_spans=[{'start': source_start + a, 'end': source_start + b,
                                                    'quote': raw_content[a:b]}
                                                   for a, b in spans])
                        if len(spans) == 1:
                            value.update(source_start=source_start + start, source_end=source_start + end)
                elif quote not in visual_urls:
                    reason = 'visual_url_not_supplied'
                elif not (value.get('observation') or '').strip():
                    reason = 'visual_observation_empty'
                if reason:
                    error = {'path': path, 'owner': owner, 'reason': reason,
                             'kind': value['kind'], 'quote': quote}
                    if value['kind'] == 'text' and isinstance(quote, str):
                        error['nearest_source'] = nearest_excerpt(quote, raw_content, source_start, full_raw)
                        if full_raw is not None and locate_quote(quote, full_raw):
                            error['found_elsewhere_in_full_document'] = True
                    else:
                        error['supplied_image_urls'] = sorted(visual_urls)
                        error['observation'] = value.get('observation')
                    errors.append(error)
            for key, child in list(value.items()):
                visit(child, f'{path}.{key}', owner)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f'{path}[{index}]', owner)

    visit(data)
    if errors:
        raise EvidenceValidationError({'stage': stage, 'source_file': source_path,
            'chunk': chunk, 'allowed_source_ranges': source_ranges, 'source_range': {'start': source_start, 'end': source_start + len(raw_content)},
            'error_count': len(errors), 'errors': errors})
