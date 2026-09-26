export interface Paper { id: string; title: string; status?: string }
export type AssertionMode = 'explicit' | 'inferred' | 'graph_record'
export type ToolName = 'indicator_details' | 'relations' | 'no_relation'
export interface Evidence {
  kind: 'text' | 'visual' | 'graph_record'; quote: string; observation?: string
  source_file?: string; source_start?: number; source_end?: number
  source_spans?: { start: number; end: number; quote: string }[]
}
export interface Source { id: string; fact_id: string; paper_id: string; paper_title: string; evidence: Evidence }
export interface SourceBundle { paper: Paper; sources: Source[] }
export interface Claim { text: string; assertion_mode: AssertionMode; fact_ids: string[]; citation_ids: string[] }
export interface Answer { status: 'answered' | 'insufficient' | 'clarification'; claims: Claim[]; limitation: string; follow_up: string | null }
export interface Trace { round: number; tool: ToolName; indicator_ids: string[]; predicates: string[]; returned: number; added: number; truncated: boolean; purpose: string }
export interface Result extends SourceBundle { request_id: string; answer: Answer; facts: Record<string, unknown>[]; trace: Trace[]; warnings: string[] }
export interface Draft { texts: string[]; limitation: string; follow_up: string | null }
export interface Progress { label: string; detail: string; seconds: number }
export interface Turn { question: string; title: string; data: Result | null; error: string; status: string; progress: Progress[]; draft: Draft | null; requestId: string; finished: boolean }
export type StreamEvent =
  | { event: 'meta'; data: { request_id: string } }
  | { event: 'status'; data: { stage: string; label: string; detail: string } }
  | { event: 'plan'; data: { explanation: string; clarification: string | null; calls: unknown[] } }
  | { event: 'tool_start'; data: { round: number; tool: ToolName } }
  | { event: 'tool_end'; data: Trace }
  | { event: 'sources'; data: SourceBundle & { warnings: string[] } }
  | { event: 'draft'; data: Draft }
  | { event: 'done'; data: Omit<Result, 'request_id'> }
  | { event: 'error'; data: { message: string } }
