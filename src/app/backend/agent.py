"""有界检索 Agent：规划 → 图查询 → 可选补查 → 基于证据生成。"""
import json
from typing import TypedDict
from time import monotonic
from threading import Event
from pydantic_core import from_json
from langgraph.config import get_stream_writer, get_config

from langgraph.graph import StateGraph, START, END
from openai import OpenAI

from . import prompts
from .schemas import Answer, Plan, ToolCall

CONTEXT_CHARS = 65_000


def emit(event, **data):
    cancel = get_config().get('configurable', {}).get('_cancel')
    if cancel is not None and cancel.is_set():
        raise InterruptedError('请求已取消')
    get_stream_writer()({'event': event, 'data': data})


class State(TypedDict, total=False):
    question: str
    paper: dict
    catalog: list
    plan: dict
    facts: list
    sources: list
    trace: list
    warnings: list
    seen_calls: list
    rounds: int
    answer: dict


class KnowledgeAgent:
    def __init__(self, store, *, api_key, base_url, model, timeout, max_tokens=12000, thinking="disabled"):
        self.store = store
        self.model = model
        self.max_tokens = max_tokens
        self.thinking = thinking
        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)
        graph = StateGraph(State)
        graph.add_node('plan', self.plan)
        graph.add_node('retrieve', self.retrieve)
        graph.add_node('review', self.review)
        graph.add_node('answer', self.answer)
        graph.add_edge(START, 'plan')
        graph.add_conditional_edges('plan', self.after_plan, ['retrieve', 'answer'])
        graph.add_conditional_edges('retrieve', lambda s: 'review' if s['rounds'] < 2 else 'answer', ['review', 'answer'])
        graph.add_conditional_edges('review', self.after_plan, ['retrieve', 'answer'])
        graph.add_edge('answer', END)
        self.graph = graph.compile()

    def close(self):
        self.client.close()

    def call(self, prompt, data, schema):
        content = ''
        reason = 'no_choices'
        last_preview, last_time = None, 0.0
        # 读取供应商真实 token 流；只投递回答字段，绝不投递 reasoning_content。
        with self.client.chat.completions.create(
            model=self.model,
            messages=[{'role':'system', 'content':prompt + '\n输出 JSON Schema：\n' + json.dumps(schema.model_json_schema(), ensure_ascii=False)},
                      {'role':'user', 'content':json.dumps(data, ensure_ascii=False)}],
            response_format={'type':'json_object'}, max_tokens=self.max_tokens, stream=True,
            extra_body={'thinking': {'type': self.thinking}}) as stream:
            for chunk in stream:
                cancel = get_config().get('configurable', {}).get('_cancel')
                if cancel is not None and cancel.is_set():
                    raise InterruptedError('请求已取消')
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                content += choice.delta.content or ''
                if choice.finish_reason:
                    reason = choice.finish_reason
                if schema is Answer and (monotonic() - last_time >= 0.06 or choice.finish_reason):
                    try:
                        partial = from_json(content, allow_partial='trailing-strings')
                    except ValueError:
                        continue
                    if not isinstance(partial, dict):
                        continue
                    claims = partial.get('claims')
                    if not isinstance(claims, list):
                        claims = []
                    texts = [c['text'] for c in claims
                             if isinstance(c, dict) and isinstance(c.get('text'), str)]
                    preview = dict(texts=texts,
                                   limitation=partial.get('limitation') if isinstance(partial.get('limitation'), str) else '',
                                   follow_up=partial.get('follow_up') if isinstance(partial.get('follow_up'), str) else None)
                    if preview != last_preview:
                        emit('draft', **preview)
                        last_preview, last_time = preview, monotonic()
        if reason != 'stop':
            raise ValueError(f'{schema.__name__} 模型输出未完整结束：finish_reason={reason}，'
                             f'max_tokens={self.max_tokens}。可缩小问题范围或调整 QA_MAX_OUTPUT_TOKENS。')
        return schema.model_validate_json(content)

    def _plan(self, state, review=False):
        emit('status', stage='review' if review else 'thinking',
             label='检查证据与规划补查中' if review else '思考中',
             detail='判断是否需要补充检索' if review else '识别指标与选择检索工具')
        data = {k: state[k] for k in ('question', 'paper', 'catalog')}
        if review:
            # 复核依据已取回的真实内容，决定是否仍需调用工具。
            data.update({k:state[k] for k in ('facts','sources','trace','warnings')})
        result = self.call(prompts.PLAN, data, Plan)
        known = {i['id'] for i in state['catalog']}
        for call in result.calls:
            if not set(call.indicator_ids) <= known:
                raise ValueError('检索计划引用了目录外指标，请明确指标名称后重试')
            if call.tool != 'relations' and call.predicates:
                raise ValueError('模型生成的检索工具参数不合法，请重试')
        if result.clarification:
            result.calls = []
        result.calls = [c for c in result.calls if c.model_dump_json() not in state['seen_calls']]
        emit('plan', **result.model_dump())
        return {'plan':result.model_dump()}

    def plan(self, state):
        return self._plan(state)

    def review(self, state):
        return self._plan(state, review=True)

    @staticmethod
    def after_plan(state):
        return 'retrieve' if state['plan']['calls'] and not state['plan']['clarification'] else 'answer'

    def retrieve(self, state):
        facts, sources = list(state['facts']), list(state['sources'])
        trace, warnings, seen_calls = list(state['trace']), list(state['warnings']), list(state['seen_calls'])
        known = {f['id'] for f in facts}
        used_chars = len(json.dumps([facts, sources], ensure_ascii=False))
        for item in state['plan']['calls']:
            call = ToolCall.model_validate(item)
            signature = call.model_dump_json()
            if signature in seen_calls:
                continue
            emit('status', stage='retrieving', label='查询知识图谱中',
                 detail='补充检索' if state['rounds'] else '首轮检索')
            emit('tool_start', round=state['rounds']+1, **call.model_dump())
            rows, truncated = self.store.retrieve(call, state['paper']['id'])
            seen_calls.append(signature)
            added = 0
            for row in rows:
                if not row.get('id'):
                    raise ValueError('图谱缺少稳定 ID，请使用最新导入器重新导入')
                if row['id'] in known:
                    continue
                evidence = row.pop('evidence')
                row['citation_ids'] = []
                new_sources = []
                # 无文字证据时只允许描述图谱记录，不能把它当作论文原文。
                if not evidence:
                    evidence = [{'kind':'graph_record', 'quote': '',
                                 'observation':'该条目来自图谱属性，未附原文证据。'}]
                for e in evidence:
                    cid = f"E{len(sources) + len(new_sources) + 1}"
                    row['citation_ids'].append(cid)
                    new_sources.append({'id':cid, 'fact_id':row['id'], 'paper_id':state['paper']['id'],
                                        'paper_title':state['paper']['title'], 'evidence':e})
                cost = len(json.dumps([row, new_sources], ensure_ascii=False))
                if used_chars + cost > CONTEXT_CHARS:
                    warnings.append('检索上下文达到长度上限，部分记录未提供，回答不代表完整枚举。')
                    continue
                used_chars += cost
                facts.append(row)
                sources.extend(new_sources)
                known.add(row['id'])
                added += 1
            if truncated:
                warnings.append(f'{call.tool} 返回超过 40 条，本轮只取前 40 条，请按具体指标缩小范围。')
            trace.append({'round':state['rounds']+1, 'tool':call.tool,
                          'indicator_ids':call.indicator_ids, 'predicates':call.predicates,
                          'returned':len(rows), 'added':added, 'truncated':truncated,
                          'purpose':state['plan']['explanation']})
            emit('tool_end', **trace[-1])
            emit('sources', sources=sources, warnings=list(dict.fromkeys(warnings)), paper=state['paper'])
        return dict(facts=facts, sources=sources, trace=trace, warnings=list(dict.fromkeys(warnings)),
                    seen_calls=seen_calls, rounds=state['rounds']+1)

    def answer(self, state):
        emit('status', stage='answering', label='回答中', detail='正在根据图谱证据生成回答')
        if state['plan']['clarification']:
            result = Answer(status='clarification', follow_up=state['plan']['clarification'])
        elif not state['facts']:
            result = Answer(status='insufficient', limitation='当前论文图谱未检索到支持该问题的资料；这不代表相关指标或关系在现实中不存在。',
                            follow_up='可以指定指标的原文名称，或换一个关系类型继续查询。')
        else:
            result = self.call(prompts.GENERATE, {k:state[k] for k in ('question','paper','facts','sources','warnings')}, Answer)
            emit('status', stage='validating', label='核对引用中', detail='检查事实与证据归属')
            facts = {f['id']:f for f in state['facts']}
            sources = {s['id']:s for s in state['sources']}
            for claim in result.claims:
                if not set(claim.fact_ids) <= facts.keys():
                    raise ValueError('回答引用了未检索到的事实，已阻止返回，请重试')
                used = [facts[fid] for fid in claim.fact_ids]
                allowed = {cid for fact in used for cid in fact['citation_ids']}
                if not set(claim.citation_ids) <= allowed:
                    raise ValueError('回答引用了不属于对应事实的证据，已阻止返回，请重试')
                if any(not set(f['citation_ids']).intersection(claim.citation_ids) for f in used):
                    raise ValueError('回答包含未提供相应引用的事实，请重试')
                if any(f.get('assertion_mode') == 'inferred' for f in used):
                    claim.assertion_mode = 'inferred'
                elif any(sources[c]['evidence']['kind'] == 'graph_record' for c in claim.citation_ids):
                    claim.assertion_mode = 'graph_record'
                elif any(f['kind'] != 'relation' for f in used) and claim.assertion_mode == 'explicit':
                    claim.assertion_mode = 'graph_record'
            if result.status == 'answered' and not result.claims:
                result.status = 'insufficient'
                result.limitation = result.limitation or '现有证据不足以形成可引用的回答。'
        return {'answer':result.model_dump()}

    def stream(self, question, paper_id, cancelled: Event):
        yield {'event':'status', 'data':{'stage':'catalog','label':'读取论文中','detail':'加载指标目录'}}
        paper, catalog, truncated = self.store.catalog(paper_id)
        if cancelled.is_set():
            return
        if not catalog:
            yield {'event':'done', 'data':dict(answer=Answer(status='insufficient', limitation='该论文尚无已导入的指标。').model_dump(),
                        sources=[], facts=[], trace=[], warnings=[], paper=paper)}
            return
        catalog = [{**i, 'aliases':(i['aliases'] or [])[:10]} for i in catalog]
        warnings = ['指标目录超过 500 条，仅提供前 500 条，实体匹配范围受限。'] if truncated else []
        state = dict(question=question, paper=paper, catalog=catalog, facts=[], sources=[],
                     trace=[], warnings=warnings, seen_calls=[], rounds=0)
        for mode, update in self.graph.stream(state,
                {'recursion_limit':12, 'configurable':{'_cancel':cancelled}}, stream_mode=['custom','updates']):
            if cancelled.is_set():
                return
            if mode == 'custom':
                yield update
            else:
                for values in update.values():
                    state.update(values)
        yield {'event':'done', 'data':{k:state[k] for k in ('answer','sources','facts','trace','warnings','paper')}}
