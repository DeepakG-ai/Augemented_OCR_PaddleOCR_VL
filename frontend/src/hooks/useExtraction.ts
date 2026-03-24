import { useState, useEffect, useCallback, useRef } from 'react';
import type {
  UploadResponse,
  JobStatus,
  JobResult,
  Vendor,
  SemanticTemplate,
  AnchorInput,
} from '../types';

const API_BASE = import.meta.env.VITE_API_BASE_URL || '';
const WS_BASE = import.meta.env.VITE_WS_BASE_URL || `ws://${window.location.host}`;

// ── Upload File ──────────────────────────────────────────────────

export function useUpload() {
  const [uploading, setUploading] = useState(false);
  const [uploadResult, setUploadResult] = useState<UploadResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const upload = useCallback(async (file: File): Promise<UploadResponse | null> => {
    setUploading(true);
    setError(null);
    try {
      const formData = new FormData();
      formData.append('file', file);
      const res = await fetch(`${API_BASE}/api/upload`, { method: 'POST', body: formData });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || 'Upload failed');
      }
      const data: UploadResponse = await res.json();
      setUploadResult(data);
      return data;
    } catch (e: any) {
      setError(e.message);
      return null;
    } finally {
      setUploading(false);
    }
  }, []);

  return { upload, uploading, uploadResult, error, setUploadResult };
}

// ── Extract ──────────────────────────────────────────────────────

export function useExtraction() {
  const [submitting, setSubmitting] = useState(false);
  const [jobStatus, setJobStatus] = useState<JobResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  const submitExtraction = useCallback(async (
    s3Key: string,
    vendorId: string,
    anchors: AnchorInput[],
    saveRules: boolean = true,
  ): Promise<JobStatus | null> => {
    setSubmitting(true);
    setError(null);
    setJobStatus(null);
    try {
      const res = await fetch(`${API_BASE}/api/extract`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          s3_key: s3Key,
          vendor_id: vendorId,
          anchors,
          save_rules: saveRules,
        }),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || 'Extraction failed');
      }
      const data: JobStatus = await res.json();

      // Connect WebSocket for real-time updates
      connectWebSocket(data.job_id);

      return data;
    } catch (e: any) {
      setError(e.message);
      return null;
    } finally {
      setSubmitting(false);
    }
  }, []);

  const connectWebSocket = useCallback((jobId: string) => {
    // Close existing connection
    if (wsRef.current) {
      wsRef.current.close();
    }

    const ws = new WebSocket(`${WS_BASE}/ws/jobs/${jobId}`);
    wsRef.current = ws;

    ws.onmessage = (event) => {
      try {
        const data: JobResult = JSON.parse(event.data);
        setJobStatus(data);

        // Close on terminal status
        if (['completed', 'failed', 'needs_review'].includes(data.status)) {
          ws.close();
        }
      } catch (e) {
        console.error('Failed to parse WebSocket message:', e);
      }
    };

    ws.onerror = (event) => {
      console.error('WebSocket error:', event);
      setError('WebSocket connection error');
    };

    ws.onclose = () => {
      wsRef.current = null;
    };
  }, []);

  // Poll fallback
  const pollJobStatus = useCallback(async (jobId: string) => {
    try {
      const res = await fetch(`${API_BASE}/api/jobs/${jobId}`);
      if (res.ok) {
        const data: JobResult = await res.json();
        setJobStatus(data);
        return data;
      }
    } catch (e) {
      console.error('Poll error:', e);
    }
    return null;
  }, []);

  // Cleanup WebSocket on unmount
  useEffect(() => {
    return () => {
      if (wsRef.current) {
        wsRef.current.close();
      }
    };
  }, []);

  return { submitExtraction, submitting, jobStatus, error, pollJobStatus, setJobStatus };
}

// ── Vendors ──────────────────────────────────────────────────────

export function useVendors() {
  const [vendors, setVendors] = useState<Vendor[]>([]);
  const [loading, setLoading] = useState(false);

  const fetchVendors = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/vendors`);
      if (res.ok) {
        const data = await res.json();
        setVendors(data);
      }
    } catch (e) {
      console.error('Failed to fetch vendors:', e);
    } finally {
      setLoading(false);
    }
  }, []);

  const createVendor = useCallback(async (name: string): Promise<Vendor | null> => {
    try {
      const res = await fetch(`${API_BASE}/api/vendors`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      if (res.ok) {
        const vendor: Vendor = await res.json();
        setVendors((prev) => [...prev, vendor].sort((a, b) => a.name.localeCompare(b.name)));
        return vendor;
      }
    } catch (e) {
      console.error('Failed to create vendor:', e);
    }
    return null;
  }, []);

  useEffect(() => {
    fetchVendors();
  }, [fetchVendors]);

  return { vendors, loading, fetchVendors, createVendor };
}

// ── Templates ────────────────────────────────────────────────────

export function useTemplates(vendorId: string | null) {
  const [templates, setTemplates] = useState<SemanticTemplate[]>([]);
  const [loading, setLoading] = useState(false);

  const fetchTemplates = useCallback(async () => {
    if (!vendorId) {
      setTemplates([]);
      return;
    }
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/vendors/${vendorId}/templates`);
      if (res.ok) {
        setTemplates(await res.json());
      }
    } catch (e) {
      console.error('Failed to fetch templates:', e);
    } finally {
      setLoading(false);
    }
  }, [vendorId]);

  useEffect(() => {
    fetchTemplates();
  }, [fetchTemplates]);

  return { templates, loading, fetchTemplates };
}

// ── Confirm Template ─────────────────────────────────────────────

export async function confirmTemplate(
  vendorId: string,
  fieldName: string,
  confirmedPageIndex: number,
  confirmedValue: string,
) {
  const res = await fetch(`${API_BASE}/api/templates/confirm`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      vendor_id: vendorId,
      field_name: fieldName,
      confirmed_page_index: confirmedPageIndex,
      confirmed_value: confirmedValue,
    }),
  });
  return res.ok;
}
