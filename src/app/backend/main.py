"""本地问答 API；密钥只保留在后端，不对外提供任意 Cypher 接口。"""
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from threading import BoundedSemaphore, Event, Thread
from queue import Queue, Empty, Full
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from neo4j.exceptions import Neo4jError, DriverError
from openai import APIError

from .agent import KnowledgeAgent
from .schemas import ChatRequest
from .store import GraphStore

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[3]


@asynccontextmanager
async def lifespan(app):
    load_dotenv(ROOT / '.env')
    app.state.store = None
    app.state.agent = None
    app.state.slots = BoundedSemaphore(2)
    required = ('NEO4J_URI','NEO4J_USER','NEO4J_PASSWORD')
    if all(os.getenv(k) for k in required):
        app.state.store = GraphStore(*(os.environ[k] for k in required), os.getenv('NEO4J_DATABASE','neo4j'))
        if os.getenv('DEEPSEEK_API_KEY'):
            app.state.agent = KnowledgeAgent(app.state.store, api_key=os.environ['DEEPSEEK_API_KEY'],
                base_url=os.getenv('DEEPSEEK_BASE_URL','https://api.deepseek.com'),
                model=os.getenv('QA_MODEL','deepseek-flash'), timeout=float(os.getenv('QA_MODEL_TIMEOUT','60')),
                max_tokens=int(os.getenv('QA_MAX_OUTPUT_TOKENS','12000')),
                thinking=os.getenv('QA_THINKING','disabled'))
    try:
        yield
    finally:
        if app.state.agent:
            app.state.agent.close()
        if app.state.store:
            app.state.store.close()


app = FastAPI(title='Scientometrics Knowledge QA', version='0.1.0', lifespan=lifespan)


def store_for(request):
    if request.app.state.store is None:
        raise HTTPException(503, '请在项目 .env 配置 NEO4J_URI、NEO4J_USER、NEO4J_PASSWORD，然后重启后端')
    return request.app.state.store


@app.get('/api/health')
def health(request: Request):
    store = store_for(request)
    try:
        store.read('RETURN 1 AS connected')
    except (Neo4jError, DriverError, OSError):
        raise HTTPException(503, '无法连接 Neo4j，请检查 Docker 容器、端口和账号配置') from None
    return {'database':'connected','model_configured':request.app.state.agent is not None}


@app.get('/api/papers')
def papers(request: Request):
    try:
        rows = store_for(request).papers()
        return {'papers':rows[:200], 'truncated':len(rows)>200}
    except (Neo4jError, DriverError, OSError):
        raise HTTPException(503, '读取论文失败，请检查 Neo4j 服务和连接配置') from None


@app.post('/api/chat', response_class=StreamingResponse,
          responses={200: {'content': {'text/event-stream': {}}}})
def chat(body: ChatRequest, request: Request):
    store_for(request)
    agent = request.app.state.agent
    if agent is None:
        raise HTTPException(503, '请配置 DEEPSEEK_API_KEY 并重启后端')
    if not body.question.strip() or not body.paper_id.strip():
        raise HTTPException(422, '问题和论文 ID 不能为空')
    if not request.app.state.slots.acquire(blocking=False):
        raise HTTPException(429, '当前已有两个问答请求在处理，请稍后重试')
    request_id = str(uuid4())
    cancelled = Event()
    events = Queue(maxsize=64)

    def send(event):
        while not cancelled.is_set():
            try:
                events.put(event, timeout=0.25)
                return
            except Full:
                continue

    def work():
        try:
            for event in agent.stream(body.question.strip(), body.paper_id, cancelled):
                send(event)
        except InterruptedError:
            pass
        except Exception as error:
            from pydantic import ValidationError
            if isinstance(error, LookupError):
                detail = str(error)
            elif isinstance(error, (Neo4jError, DriverError, OSError)):
                detail = '图谱检索失败，请检查 Neo4j 连接后重试'
            elif isinstance(error, APIError):
                detail = '模型服务请求失败或超时，请检查模型、API 配置及网络后重试'
            elif isinstance(error, ValidationError):
                detail = '模型输出结构不符合要求，请重试'
            elif isinstance(error, ValueError):
                detail = str(error)
            else:
                log.exception('QA request failed: %s', request_id)
                detail = f'问答处理失败，诊断 ID：{request_id}'
            send({'event':'error', 'data':{'message':detail}})
        finally:
            request.app.state.slots.release()
            send(None)

    async def response():
        worker = Thread(target=work, daemon=True, name=f'qa-{request_id}')
        worker.start()
        try:
            yield 'event: meta\ndata: ' + json.dumps({'request_id':request_id}) + '\n\n'
            while not cancelled.is_set():
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.to_thread(events.get, True, 1)
                except Empty:
                    yield ': keepalive\n\n'
                    continue
                if event is None:
                    break
                yield f"event: {event['event']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
        finally:
            cancelled.set()

    return StreamingResponse(response(), media_type='text/event-stream',
                             headers={'Cache-Control':'no-cache', 'X-Accel-Buffering':'no'})


def main():
    import uvicorn
    uvicorn.run('app.backend.main:app', host='127.0.0.1', port=8000)


if __name__ == '__main__':
    main()
