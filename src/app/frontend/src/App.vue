<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { api, streamChat } from './api'
import type { Paper, Result, SourceBundle, Turn, ToolName, AssertionMode } from './types'

const papers = ref<Paper[]>([]), paperId = ref(''), question = ref(''), turns = ref<Turn[]>([])
const busy = ref(false), booting = ref(false), connection = ref('连接中'), error = ref(''), elapsed = ref(0)
const active = ref<SourceBundle | null>(null), activeCitation = ref(''), resultsEl = ref<HTMLElement | null>(null)
let timer: ReturnType<typeof setInterval> | undefined
let controller: AbortController | null = null
const examples = ['这篇论文提出了哪些指标？', '这些指标之间有哪些改进或变体关系？', '哪些指标仅被背景提及，为什么？']
const selectedPaper = computed(() => papers.value.find(p => p.id === paperId.value))
const sources = computed(() => active.value?.sources || [])
const labels: Record<AssertionMode, string> = { explicit: '原文明示', inferred: '根据证据推断', graph_record: '图谱记录' }
const toolLabels: Record<ToolName, string> = { indicator_details: '指标详情', relations: '关系检索', no_relation: '无关系判定' }

function message(error: unknown): string { return error instanceof Error ? error.message : '请求失败' }
async function load() {
  booting.value = true; error.value = ''
  try {
    const health = await api<{ model_configured: boolean }>('/api/health')
    connection.value = health.model_configured ? '图谱已连接' : '模型待配置'
    const data = await api<{ papers: Paper[]; truncated: boolean }>('/api/papers')
    papers.value = data.papers
    if (!papers.value.some(p => p.id === paperId.value)) paperId.value = papers.value[0]?.id || ''
    if (data.truncated) error.value = '论文列表超过 200 篇，当前仅展示前 200 篇。'
    if (!health.model_configured) error.value = '请配置后端 DEEPSEEK_API_KEY，再重启后端。'
  } catch (e) { connection.value = '连接未就绪'; error.value = message(e) }
  finally { booting.value = false }
}
async function ask() {
  if (busy.value || !question.value.trim() || !paperId.value) return
  const text = question.value.trim(), id = paperId.value
  const title = selectedPaper.value?.title || id
  busy.value = true; error.value = ''; elapsed.value = 0
  timer = setInterval(() => elapsed.value++, 1000)
  controller = new AbortController()
  const timeout = setTimeout(() => controller?.abort(), 360000)
  const turn: Turn = { question: text, title, data: null, error: '', status: '连接中',
    progress: [], draft: null, requestId: '', finished: false }
  turns.value.push(turn)
  const index = turns.value.length - 1
  question.value = ''
  await nextTick(); resultsEl.value?.lastElementChild?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  try {
    const current = turns.value[index]!
    await streamChat({ question: text, paper_id: id }, controller.signal, (event) => {
      switch (event.event) {
        case 'meta': current.requestId = event.data.request_id; break
        case 'status':
          current.status = event.data.label
          current.progress.push({ label: event.data.label, detail: event.data.detail, seconds: elapsed.value })
          break
        case 'plan':
          current.progress.push({ label: '检索计划', detail: event.data.explanation, seconds: elapsed.value })
          break
        case 'tool_start':
          current.progress.push({ label: `第 ${event.data.round} 轮 · ${toolLabels[event.data.tool]}`, detail: '正在执行只读查询', seconds: elapsed.value })
          break
        case 'tool_end':
          current.progress.push({ label: '查询完成', detail: `返回 ${event.data.returned} 条，新增 ${event.data.added} 条${event.data.truncated ? '（结果已截断）' : ''}`, seconds: elapsed.value })
          break
        case 'sources': active.value = event.data; activeCitation.value = ''; break
        case 'draft': current.draft = event.data; break
        case 'done':
          current.data = { ...event.data, request_id: current.requestId }
          current.draft = null; current.finished = true; current.status = '已完成'
          current.progress.push({ label: '已完成', detail: '回答及引用归属检查完成', seconds: elapsed.value })
          active.value = current.data; activeCitation.value = ''
          break
        case 'error': break // streamChat 统一抛出，清除未验证预览
      }
    })
  } catch (e) {
    const current = turns.value[index]!
    const stopped = e instanceof Error && e.name === 'AbortError'
    current.error = stopped ? '已停止等待，未完成的回答预览已清除。' : message(e)
    current.draft = null; current.finished = true; current.status = stopped ? '已停止' : '失败'
    current.progress.push({ label: current.status, detail: current.error, seconds: elapsed.value })
    question.value = text
  } finally { clearInterval(timer); clearTimeout(timeout); busy.value = false; controller = null }
}
function stop() { controller?.abort() }

async function showCitation(data: Result, id: string) {
  active.value = data; activeCitation.value = id
  await nextTick()
  document.getElementById(`citation-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
}
function safeUrl(value: string) {
  try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) ? url.href : null } catch { return null }
}
function reset() { turns.value = []; active.value = null; activeCitation.value = ''; error.value = '' }
onMounted(load)
onBeforeUnmount(() => { clearInterval(timer); controller?.abort() })
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <a class="brand" href="/" aria-label="指标知识库首页"><span class="brand-mark">∴</span><span>指标知识库<small>SCIENTOMETRICS · GRAPH RAG</small></span></a>
      <div class="connection"><span :class="['dot', { online: connection === '图谱已连接' }]" />{{ connection }}</div>
    </header>
    <main>
      <section class="intro"><span class="eyebrow">从关系出发，让证据回答</span><h1>论文里的指标，<br class="mobile-break">有据可问。</h1><p>探索科学计量指标之间的联系，追溯每个回答背后的原文。</p></section>
      <section class="workspace">
        <div class="chat-column">
          <div class="scope-card">
            <label for="paper">检索范围 <span>单篇论文</span></label>
            <div class="scope-controls"><select id="paper" v-model="paperId" :disabled="busy || booting || !papers.length"><option v-if="!papers.length" value="">暂无论文，请先导入 Neo4j</option><option v-for="p in papers" :key="p.id" :value="p.id">{{ p.title }} · {{ p.id }}</option></select><button class="icon-button" :disabled="busy || booting" @click="load" aria-label="刷新连接和论文列表">↻</button></div>
            <p class="scope-note">每次提问独立检索当前论文；提及具体指标时请填写名称。</p>
          </div>
          <p v-if="error" role="alert" class="error">{{ error }}</p>
          <div v-if="!turns.length" class="empty-state"><div class="orbit">◎</div><h2>从一个问题开始</h2><p>Agent 将选择检索工具，查询图谱关系与证据，再生成回答。</p><div class="examples"><button v-for="example in examples" :key="example" @click="question = example">{{ example }} <span>↗</span></button></div></div>
          <div ref="resultsEl" class="turns" aria-live="polite">
            <article v-for="(turn, index) in turns" :key="index" class="turn">
              <div class="question"><span>你的问题</span><h2>{{ turn.question }}</h2><small>{{ turn.title }}</small></div>
              <details class="live-trace" :open="!turn.finished">
                <summary><span :class="{ spinner: !turn.finished }" />{{ turn.status }}<small v-if="!turn.finished">{{ elapsed }}s</small></summary>
                <ol><li v-for="(step, n) in turn.progress" :key="n"><span>{{ step.seconds }}s</span><div><strong>{{ step.label }}</strong><p>{{ step.detail }}</p></div></li></ol>
              </details>
              <div v-if="turn.draft" class="stream-draft"><span class="badge">正在生成 · 引用待核对</span><p v-for="(text, n) in turn.draft.texts" :key="n">{{ text }}</p><p v-if="turn.draft.limitation">{{ turn.draft.limitation }}</p><p v-if="turn.draft.follow_up">{{ turn.draft.follow_up }}</p></div>
              <div v-if="turn.error" class="error" role="alert">{{ turn.error }}</div>
              <div v-else-if="!turn.data && !turn.draft" class="loading">正在等待当前阶段返回…</div>
              <div v-if="turn.data" class="answer">
                <div class="answer-heading"><span>图谱回答</span><button class="text-button" @click="active = turn.data; activeCitation = ''">查看依据 ↗</button></div>
                <div v-for="(claim, n) in turn.data.answer.claims" :key="n" class="claim"><span :class="['badge', claim.assertion_mode]">{{ labels[claim.assertion_mode] }}</span><p>{{ claim.text }}</p><div class="citations"><button v-for="id in claim.citation_ids" :key="id" @click="showCitation(turn.data, id)">[{{ id }}] 证据</button></div></div>
                <p v-if="turn.data.answer.limitation" class="notice">{{ turn.data.answer.limitation }}</p>
                <p v-if="turn.data.answer.follow_up" class="followup">{{ turn.data.answer.follow_up }}</p>
                <p v-for="warning in turn.data.warnings" :key="warning" class="notice">{{ warning }}</p>
                <details class="trace"><summary>检索过程 · {{ turn.data.trace.length }} 次工具调用</summary><div v-for="(step, n) in turn.data.trace" :key="n"><strong>第 {{ step.round }} 轮 · {{ toolLabels[step.tool] }}</strong><p>{{ step.purpose }}</p><small>{{ step.predicates.join(' / ') || '未限制关系类型' }} · 返回 {{ step.returned }} 条，新增 {{ step.added }} 条</small></div><small class="request-id">请求 ID：{{ turn.data.request_id }}</small></details>
              </div>
            </article>
          </div>
          <form class="composer" @submit.prevent="ask"><label for="question">向知识图谱提问</label><textarea id="question" v-model="question" maxlength="2000" rows="3" placeholder="例如：本文提出了哪些指标？请给出原文证据。" :disabled="busy" @keydown.ctrl.enter.prevent="ask" @keydown.meta.enter.prevent="ask" /><div class="composer-footer"><small>{{ question.length }}/2000 · ⌘ / Ctrl + Enter 发送</small><button v-if="busy" type="button" class="text-button" @click="stop">停止生成</button><button type="submit" class="primary" :disabled="busy || !paperId || !question.trim()">{{ busy ? '正在检索…' : '检索并回答' }} <span>↗</span></button></div></form>
          <div class="bottom-note"><span>基于已抽取数据回答，引用可溯源不等于语义已验证。</span><button class="text-button" :disabled="busy || !turns.length" @click="reset">清空会话</button></div>
        </div>
        <aside class="evidence-panel"><div class="panel-heading"><div><span class="eyebrow">SOURCE NOTES</span><h2>证据与来源</h2></div><span class="count">{{ sources.length }}</span></div>
          <div v-if="!active" class="evidence-empty"><span>↖</span><p>回答生成后，点击引用编号，<br>在这里查看对应原文与来源。</p></div>
          <template v-else><p class="paper-caption">{{ active.paper.title }}</p><p v-if="!sources.length" class="evidence-empty">本次没有可展示的证据。</p><div class="source-list"><article v-for="source in sources" :id="`citation-${source.id}`" :key="source.id" :class="['source-card', { selected: source.id === activeCitation }]">
            <div class="source-heading"><strong>{{ source.id }}</strong><span>{{ source.evidence.kind === 'text' ? '原文证据' : source.evidence.kind === 'visual' ? '已有视觉观察' : '图谱属性记录' }}</span></div>
            <blockquote v-if="source.evidence.kind === 'text'">{{ source.evidence.quote }}</blockquote>
            <template v-else-if="source.evidence.kind === 'visual'"><p>{{ source.evidence.observation }}</p><a v-if="safeUrl(source.evidence.quote)" :href="safeUrl(source.evidence.quote) || undefined" target="_blank" rel="noopener noreferrer">打开原图 ↗</a><small v-else>{{ source.evidence.quote }}</small></template>
            <p v-else>{{ source.evidence.observation }}</p>
            <details><summary>溯源信息</summary><dl><dt>论文</dt><dd>{{ source.paper_id }}</dd><dt>事实 ID</dt><dd>{{ source.fact_id }}</dd><template v-if="source.evidence.source_file"><dt>原始文件</dt><dd>{{ source.evidence.source_file }}</dd></template><template v-if="source.evidence.source_spans?.length"><dt>字符位置（从 0 起）</dt><dd v-for="(span, n) in source.evidence.source_spans" :key="n">[{{ span.start }}, {{ span.end }})</dd></template><template v-else-if="source.evidence.source_start != null"><dt>字符位置</dt><dd>[{{ source.evidence.source_start }}, {{ source.evidence.source_end }})</dd></template></dl></details>
          </article></div></template>
        </aside>
      </section>
    </main><footer>SCIENTOMETRICS KNOWLEDGE EXPLORER <span>Vue · LangGraph · Neo4j</span></footer>
  </div>
</template>
