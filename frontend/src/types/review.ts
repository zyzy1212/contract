export interface Bbox {
  x0: number
  top: number
  x1: number
  bottom: number
}

export interface EvidenceSnapshot {
  title: string
  article_number?: string
  section_title?: string
  page_start?: number
  page_end?: number
  paragraph_index?: number
  version: string
  effective_date?: string
  source_url?: string
  bboxes: Bbox[]
}

export interface EvidenceRef {
  id: string
  text: string
  rank: number
  source_snapshot: EvidenceSnapshot
  source_content_sha256: string
}

export interface Finding {
  id: string
  clause_id: string
  risk_level: 'high' | 'medium' | 'low'
  problem: string
  reason: string
  suggestion: string
  proposed_clause: string
  evidence: EvidenceRef[]
}

export interface SourceClause {
  id: string
  text: string
  status?: string
  article_number?: string
  paragraph_index?: number
  page_start?: number
  page_end?: number
  bboxes: Bbox[]
}

export interface ReviewDetail {
  id: string
  status: string
  contract_id: string
  total_clauses: number
  completed_clauses: number
  unreviewed_clause_ids: string[]
  failure_reason?: string
  insufficient_clause_count?: number
  findings: Finding[]
  source_clauses: SourceClause[]
}

export interface ReviewHistoryItem {
  id: string
  contract_id: string
  filename: string
  content_type: string
  status: string
  total_clauses: number
  completed_clauses: number
  failure_reason?: string
  created_at?: string
  updated_at?: string
}
