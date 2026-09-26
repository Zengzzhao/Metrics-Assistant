import type { StreamEvent } from './types'

async function checked(response: Response): Promise<Response> {
  if (!response.ok) {
    const data: { detail?: unknown } = await response.json().catch(() => ({}))
    throw new Error(typeof data.detail === 'string' ? data.detail : `请求失败（${response.status}）`)
  }
  return response
}
export async function api<T>(path: string): Promise<T> {
  return (await checked(await fetch(path))).json() as Promise<T>
}

/** 增量解码 UTF-8 与 SSE 分帧，不能将一次网络分块当作一条事件。 */
export async function streamChat(body: { question: string; paper_id: string }, signal: AbortSignal,
  receive: (event: StreamEvent) => void): Promise<void> {
  const response = await checked(await fetch('/api/chat', {
    method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(body), signal,
  }))
  if (!response.body || !response.headers.get('content-type')?.includes('text/event-stream')) {
    throw new Error('服务未返回事件流，请确认后端已更新并重启')
  }
  const reader = response.body.getReader(), decoder = new TextDecoder()
  let buffer = '', completed = false
  function dispatch(frame: string): void {
    const lines = frame.split(/\r?\n/)
    const name = lines.find(line => line.startsWith('event:'))?.slice(6).trim()
    const data = lines.filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n')
    if (!name || !data) return // 忽略心跳注释
    const event = { event: name, data: JSON.parse(data) } as StreamEvent
    receive(event)
    if (event.event === 'error') throw new Error(event.data.message)
    if (event.event === 'done') completed = true
  }
  try {
    while (!completed) {
      const { value, done } = await reader.read()
      buffer += decoder.decode(value, { stream: !done })
      let boundary: RegExpExecArray | null
      while ((boundary = /\r?\n\r?\n/.exec(buffer)) !== null) {
        const frame = buffer.slice(0, boundary.index)
        buffer = buffer.slice(boundary.index + boundary[0].length)
        dispatch(frame)
      }
      if (done) break
    }
    if (!completed) throw new Error('连接提前结束，回答尚未完成，请重试')
  } finally {
    await reader.cancel().catch(() => undefined)
    reader.releaseLock()
  }
}
