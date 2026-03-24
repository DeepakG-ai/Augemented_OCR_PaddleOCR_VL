// ── Vendor ─────────────────────────────────────────────────────────

export interface Vendor {
  id: string;
  name: string;
  created_at: string;
}

// ── Anchor (canvas annotation) ────────────────────────────────────

export interface Anchor {
  id: string;
  field: string;
  prompt: string;
  x_pct: number;    // normalized [0.0, 1.0] — center of selection, sent to API
  y_pct: number;
  pixelX: number;   // raw pixels — for canvas rendering only
  pixelY: number;
  rectX?: number;   // selection rectangle bounds (pixels)
  rectY?: number;
  rectW?: number;
  rectH?: number;
}

// ── Semantic Template ─────────────────────────────────────────────

export interface SemanticTemplate {
  id: string;
  vendor_id: string;
  field_name: string;
  semantic_prompt: string;
  norm_x: number;
  norm_y: number;
  page_index: number | null;
  sample_count: number;
  created_at: string;
  updated_at: string;
}

// ── Extraction ────────────────────────────────────────────────────

export interface ExtractionCandidate {
  value: string;
  page_index: number;
}

export interface ExtractionResult {
  status: 'queued' | 'processing' | 'completed' | 'needs_review' | 'failed';
  data: Record<string, string | null>;
  candidates?: Record<string, ExtractionCandidate[]>;
  error?: string;
}

export interface JobStatus {
  job_id: string;
  document_id: string;
  status: string;
}

export interface JobResult {
  job_id: string;
  document_id: string;
  status: string;
  data?: Record<string, string | null>;
  candidates?: Record<string, ExtractionCandidate[]>;
  error?: string;
}

// ── Upload ────────────────────────────────────────────────────────

export interface UploadResponse {
  s3_key: string;
  filename: string;
  size: number;
  presigned_url: string;
}

// ── API Request ───────────────────────────────────────────────────

export interface AnchorInput {
  field: string;
  prompt: string;
  x_pct: number;
  y_pct: number;
  page_index?: number | null;
}

export interface ExtractionRequest {
  s3_key: string;
  vendor_id: string;
  anchors: AnchorInput[];
  save_rules: boolean;
}

// ── Field Options ─────────────────────────────────────────────────

export const FIELD_OPTIONS = [
  'invoice_total',
  'invoice_number',
  'invoice_date',
  'vendor_name',
  'tax_amount',
  'subtotal',
  'due_date',
  'po_number',
  'bill_to',
  'payment_terms',
] as const;

export type FieldOption = typeof FIELD_OPTIONS[number];
