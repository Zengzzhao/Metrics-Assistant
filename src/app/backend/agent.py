"""有界检索 Agent：规划 → 单轮图查询 → 基于证据生成。"""
import json
from typing import TypedDict
from time import monotonic
from threading import Event
from uuid import UUID, uuid4
from pydantic_core import from_json
from langgraph.config import get_stream_writer, get_config

from langgraph.graph import StateGraph, START, END
from openai import OpenAI

from . import prompts
from .schemas import Answer, Plan, ToolCall
from .observability import instrument_client, tracing_enabled, validate_tracing


def emit(event, **data):
    cancel = get_config().get('configurable', {}).get('_cancel')
    if cancel is not None and cancel.is_set():
        raise InterruptedError('请求已取消')
    get_stream_writer()({'event': event, 'data': data})


class State(TypedDict, total=False):
    question: str
    history: list
    paper: dict
    catalog: list
    plan: dict
    facts: list
    sources: list
    trace: list
    warnings: list
    answer: dict


class KnowledgeAgent:
    def __init__(self, store, *, api_key, base_url, model, timeout, max_tokens=12000, thinking="disabled"):
        validate_tracing()
        self.store = store
        self.model = model
        self.max_tokens = max_tokens
        self.thinking = thinking
        self.client = instrument_client(OpenAI(
            api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0))
        graph = StateGraph(State)
        graph.add_node('plan', self.plan)
        graph.add_node('retrieve', self.retrieve)
        graph.add_node('answer', self.answer)
        graph.add_edge(START, 'plan')
        graph.add_conditional_edges('plan', self.after_plan, ['retrieve', 'answer'])
        graph.add_edge('retrieve', 'answer')
        graph.add_edge('answer', END)
        self.graph = graph.compile()

    def close(self):
        self.client.close()

    def call(self, prompt, data, schema, *, stage):
        content = ''
        reason = 'no_choices'
        last_preview, last_time = None, 0.0
        extra = {"langsmith_extra": {
            "name": f"deepseek:qa:{stage}", "run_id": str(uuid4()),
            "metadata": {"stage": stage},
        }} if tracing_enabled() else {}
        # 读取供应商真实 token 流；只投递回答字段，绝不投递 reasoning_content。
        with self.client.chat.completions.create(
            model=self.model,
            messages=[{'role':'system', 'content':prompt + '\n输出 JSON Schema：\n' + json.dumps(schema.model_json_schema(), ensure_ascii=False)},
                      {'role':'user', 'content':json.dumps(data, ensure_ascii=False)}],
            response_format={'type':'json_object'}, max_tokens=self.max_tokens, stream=True,
            stream_options={'include_usage': True},
            extra_body={'thinking': {'type': self.thinking}}, **extra) as stream:
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
                                   message=partial.get('message') if isinstance(partial.get('message'), str) else '')
                    if preview != last_preview:
                        emit('draft', **preview)
                        last_preview, last_time = preview, monotonic()
        if reason != 'stop':
            raise ValueError(f'{schema.__name__} 模型输出未完整结束：finish_reason={reason}，'
                             f'max_tokens={self.max_tokens}。可缩小问题范围或调整 QA_MAX_OUTPUT_TOKENS。')
        return schema.model_validate_json(content)

    def plan(self, state):
        """单轮规划：根据问题、历史问答和指标目录选择检索工具。"""
        emit('status', stage='thinking', label='规划中', detail='根据问题、历史问答和指标目录判断问题是否清晰，若清晰则选择检索工具进行检索')
        data = {k: state[k] for k in ('question', 'history', 'paper', 'catalog')}
        result = self.call(prompts.PLAN, data, Plan, stage='plan')
        self._validate_plan(result, state)
        plan = result.model_dump()
        emit('plan', **plan)
        return {'plan': plan}

    @staticmethod
    def _validate_plan(result: Plan, state: State):
        """校验工具参数，并清除澄清时的调用；允许重复查询。"""
        known = {i['id'] for i in state['catalog']}
        for call in result.calls:
            if not set(call.indicator_ids) <= known:
                raise ValueError('检索计划引用了目录外指标，请明确指标名称后重试')
            if call.tool != 'relations' and call.predicates:
                raise ValueError('模型生成的检索工具参数不合法，请重试')
        if result.clarification:
            result.calls = []

    @staticmethod
    def after_plan(state):
        # 如果有工具且问题清楚不需要澄清，则进入检索阶段；否则直接进入回答阶段。
        return 'retrieve' if state['plan']['calls'] and not state['plan']['clarification'] else 'answer'

    def retrieve(self, state):
        facts, sources = list(state['facts']), list(state['sources'])
        # 工具执行记录用于前端展示检索过程。
        trace, warnings = list(state['trace']), list(state['warnings'])
        known = {f['id'] for f in facts}
        emit('status', stage='retrieving', label='查询知识图谱中', detail='执行规划中的检索工具')
        # 一次执行计划内的全部调用，不根据中间结果追加查询。
        for item in state['plan']['calls']:
            call = ToolCall.model_validate(item)
            emit('tool_start', **call.model_dump())
            rows = self.store.retrieve(call, state['paper']['id'])
            added = 0
            for row in rows:
                if not row.get('id'):
                    raise ValueError('图谱缺少稳定 ID，请使用最新导入器重新导入')
                if row['id'] in known:
                    continue
                evidence = row.pop('evidence')
                row['citation_ids'] = []
                new_sources = []
                for e in evidence:
                    cid = f"E{len(sources) + len(new_sources) + 1}"
                    row['citation_ids'].append(cid)
                    new_sources.append({'id':cid, 'fact_id':row['id'], 'paper_id':state['paper']['id'],
                                        'paper_title':state['paper']['title'], 'evidence':e})
                facts.append(row)
                sources.extend(new_sources)
                known.add(row['id'])
                added += 1
            trace.append({'tool':call.tool,
                          'indicator_ids':call.indicator_ids, 'predicates':call.predicates,
                          'returned':len(rows), 'added':added, 'truncated':False,
                          'purpose':state['plan']['explanation']})
            emit('tool_end', **trace[-1])
            emit('sources', sources=sources, warnings=list(dict.fromkeys(warnings)), paper=state['paper'])
        return dict(facts=facts, sources=sources, trace=trace, warnings=list(dict.fromkeys(warnings)))

    def answer(self, state):
        emit('status', stage='answering', label='回答中', detail='正在根据图谱证据生成回答')
        # 需要用户澄清
        if state['plan']['clarification']:
            result = Answer(message=state['plan']['clarification'])
        # 未执行查询时，模型判定当前问题不需要检索，直接回答
        elif not state['facts'] and not state['trace']:
            result = Answer(message=state['plan']['explanation'])
        # 查询完成后统一生成回答；空结果无需核对引用。
        else:
            result = self.call(prompts.GENERATE, {k:state[k] for k in ('question','history','paper','facts','sources','trace','warnings')}, Answer, stage='answer')
            if not state['facts']:
                if result.claims or not result.message.strip():
                    raise ValueError('空查询回答应提供直接结论，不能生成事实引用，请重试')
                return {'answer': result.model_dump()}
            emit('status', stage='validating', label='核对证据引用中', detail='检查事实与证据归属')
            facts = {f['id']:f for f in state['facts']}
            for claim in result.claims:
                if not set(claim.fact_ids) <= facts.keys():
                    raise ValueError('回答引用了未检索到的事实，已阻止返回，请重试')
                used = [facts[fid] for fid in claim.fact_ids]
                allowed = {cid for fact in used for cid in fact['citation_ids']}
                if not set(claim.citation_ids) <= allowed:
                    raise ValueError('回答引用了不属于对应事实的证据，已阻止返回，请重试')
                if any(not set(f['citation_ids']).intersection(claim.citation_ids) for f in used):
                    raise ValueError('回答包含未提供相应证据的事实，请重试')
                # 引用了推断事实 → 标记为 inferred
                if any(f.get('assertion_mode') == 'inferred' for f in used):
                    claim.assertion_mode = 'inferred'
                # 定义和无关系判定属于图谱记录；有证据不等于作者明示该判定。
                elif any(f['kind'] != 'relation' for f in used) and claim.assertion_mode == 'explicit':
                    claim.assertion_mode = 'graph_record'
            if not result.claims:
                result.message = result.message
        return {'answer':result.model_dump()}

    # 流式问答入口
    def stream(self, question, paper_id, cancelled: Event, *, history=(), request_id=None):
        # 加载论文和指标目录
        yield {'event':'status', 'data':{'stage':'catalog','label':'读取论文中','detail':'读取论文并加载指标目录'}}
        paper, catalog = self.store.catalog(paper_id)
        if cancelled.is_set():
            return
        if not catalog:
            yield {'event':'done', 'data':dict(answer=Answer(message='该论文尚无已导入的指标。').model_dump(),
                        sources=[], facts=[], trace=[], warnings=[], paper=paper)}
            return
        catalog = [{**i, 'aliases': i['aliases'] or []} for i in catalog]
        # 完整保留历史文本，仅移除旧请求局部的事实与引用编号。
        conversation = [
            dict(question=turn.question, answer=turn.answer.model_dump(
                exclude={'claims': {'__all__': {'fact_ids', 'citation_ids'}}}))
            for turn in history
        ]
        # 初始化 LangGraph 状态
        state = dict(question=question, history=conversation, paper=paper, catalog=catalog, facts=[], sources=[],
                     trace=[], warnings=[])
        run_id = UUID(request_id) if request_id else uuid4()
        config = {
            'run_id': run_id, 'run_name': 'qa:chat', 'tags': ['qa'],
            'metadata': {'request_id': str(run_id), 'paper_id': paper_id,
                         'model': self.model, 'history_turns': len(conversation)},
            'recursion_limit': 12, 'configurable': {'_cancel': cancelled},
        }
        for mode, update in self.graph.stream(state, config, stream_mode=['custom','updates']):
            # LangGraph 同时给两种输出。stream_mode=['custom', 'updates']
            if cancelled.is_set():
                return
            # custom：自己主动emit的事件，给UI/用户看的实时事件。
            if mode == 'custom':
                yield update
            # updates：LangGraph 节点执行结束以后，节点返回了哪些 state 更新，将更新应用到局部state中，供最终回答使用
            else:
                for values in update.values():
                    state.update(values)
        yield {'event':'done', 'data':{k:state[k] for k in ('answer','sources','facts','trace','warnings','paper')}}
