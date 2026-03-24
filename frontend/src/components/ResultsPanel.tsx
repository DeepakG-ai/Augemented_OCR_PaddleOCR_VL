import React, { useState } from 'react';
import type { JobResult, ExtractionCandidate } from '../types';
import { confirmTemplate } from '../hooks/useExtraction';

interface Props {
  result: JobResult | null;
  vendorId: string | null;
  onConfirmed?: () => void;
}

const ResultsPanel: React.FC<Props> = ({ result, vendorId, onConfirmed }) => {
  const [confirmingField, setConfirmingField] = useState<string | null>(null);

  // ── Empty state ────────────────────────────────────────────────────
  if (!result) {
    return (
      <div className="h-full flex flex-col">
        <div className="p-4" style={{ borderBottom: '1px solid var(--sov-border)' }}>
          <div className="text-label">Extraction Map</div>
        </div>
        <div className="flex-1 flex items-center justify-center p-6">
          <div className="text-center space-y-3">
            <div className="w-14 h-14 mx-auto rounded flex items-center justify-center"
                 style={{ background: 'var(--sov-bg-surface)', border: '1px solid var(--sov-border)' }}>
              <svg className="w-7 h-7" fill="none" viewBox="0 0 24 24" stroke="currentColor"
                   style={{ color: 'var(--sov-text-dim)' }}>
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
                  d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
              </svg>
            </div>
            <p className="font-mono text-[10px]" style={{ color: 'var(--sov-text-dim)' }}>
              Extraction results will appear here
            </p>
          </div>
        </div>
      </div>
    );
  }

  // ── Status config ──────────────────────────────────────────────────
  const statusConfig: Record<string, { label: string; badgeClass: string; icon: string }> = {
    queued:       { label: 'QUEUED',       badgeClass: 'sov-badge-amber', icon: '⏳' },
    processing:   { label: 'PROCESSING',   badgeClass: 'sov-badge-blue',  icon: '⚙️' },
    completed:    { label: 'COMPLETED',    badgeClass: 'sov-badge-green', icon: '✅' },
    needs_review: { label: 'NEEDS REVIEW', badgeClass: 'sov-badge-amber', icon: '⚠️' },
    failed:       { label: 'FAILED',       badgeClass: 'sov-badge-red',   icon: '❌' },
  };

  const cfg = statusConfig[result.status] || statusConfig.processing;

  const handleConfirm = async (fieldName: string, candidate: ExtractionCandidate) => {
    if (!vendorId) return;
    setConfirmingField(fieldName);
    const ok = await confirmTemplate(vendorId, fieldName, candidate.page_index, candidate.value);
    setConfirmingField(null);
    if (ok && onConfirmed) onConfirmed();
  };

  return (
    <div className="h-full flex flex-col">
      {/* ── Status Header ──────────────────────────────────────────── */}
      <div className="px-4 py-3 flex items-center justify-between"
           style={{ borderBottom: '1px solid var(--sov-border)' }}>
        <div className="flex items-center gap-2">
          <span className="text-sm">{cfg.icon}</span>
          <span className={`sov-badge ${cfg.badgeClass}`}>{cfg.label}</span>
        </div>
      </div>

      {/* ── Content ────────────────────────────────────────────────── */}
      <div className="flex-1 overflow-y-auto">

        {/* Processing state */}
        {result.status === 'processing' && (
          <div className="p-6 text-center space-y-3 animate-in">
            <div className="w-8 h-8 mx-auto rounded-full flex items-center justify-center"
                 style={{ border: '2px solid var(--sov-accent-blue)' }}>
              <div className="w-3 h-3 rounded-full status-dot-pulse"
                   style={{ background: 'var(--sov-accent-blue)' }} />
            </div>
            <p className="font-mono text-xs font-bold text-white">EXTRACTION IN PROGRESS</p>
            <p className="font-mono text-[10px]" style={{ color: 'var(--sov-text-dim)' }}>
              Sovereign engine processing document...
            </p>
          </div>
        )}

        {/* Error state */}
        {result.status === 'failed' && result.error && (
          <div className="p-4">
            <div className="p-3 rounded" style={{ background: 'rgba(239, 68, 68, 0.08)', border: '1px solid rgba(239, 68, 68, 0.2)' }}>
              <p className="font-mono text-xs" style={{ color: 'var(--sov-accent-red)' }}>
                {result.error}
              </p>
            </div>
          </div>
        )}

        {/* ── Extracted Data (completed) ────────────────────────────── */}
        {result.data && Object.keys(result.data).length > 0 && (
          <div className="p-4 space-y-3 animate-in">
            <div className="text-label" style={{ color: 'var(--sov-accent-blue)' }}>
              Extracted Data Payload
            </div>

            <table className="sov-table">
              <thead>
                <tr>
                  <th>Field ID</th>
                  <th>Value</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(result.data).map(([field, value]) => (
                  <tr key={field}>
                    <td className="mono" style={{ color: 'var(--sov-text-secondary)', fontSize: '11px' }}>
                      {field.toUpperCase()}
                    </td>
                    <td className="mono" style={{ fontSize: '12px', fontWeight: 600 }}>
                      {value ? (
                        <span style={{ color: 'var(--sov-accent-cyan)' }}>{value}</span>
                      ) : (
                        <span style={{ color: 'var(--sov-text-dim)' }}>NULL</span>
                      )}
                    </td>
                    <td>
                      {value ? (
                        <span className="sov-badge sov-badge-green">VERIFIED</span>
                      ) : (
                        <span className="sov-badge sov-badge-red">MISSING</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* ── Conflict Resolution (needs_review) ───────────────────── */}
        {result.status === 'needs_review' && result.candidates && (
          <div className="p-4 space-y-4 animate-in">
            <div className="text-label" style={{ color: 'var(--sov-accent-amber)' }}>
              Resolve Conflict
            </div>

            {Object.entries(result.candidates).map(([field, candidates]) => (
              <div key={field} className="space-y-2">
                <p className="font-mono text-xs" style={{ color: 'var(--sov-text-secondary)' }}>
                  Multiple extraction candidates found for "<span className="font-bold text-white">
                  {field.replace(/_/g, ' ')}</span>".
                </p>

                {(candidates as ExtractionCandidate[]).map((c, idx) => (
                  <button
                    key={idx}
                    id={`confirm-${field}-page-${c.page_index}`}
                    onClick={() => handleConfirm(field, c)}
                    disabled={confirmingField === field}
                    className="w-full text-left p-3 rounded transition-all duration-150 group
                               disabled:opacity-50 disabled:cursor-not-allowed"
                    style={{
                      background: 'var(--sov-bg-surface)',
                      border: '1px solid var(--sov-border)',
                    }}
                  >
                    <div className="flex items-center justify-between">
                      <div>
                        <div className="flex items-center gap-2 mb-1">
                          <span className="sov-badge sov-badge-blue">
                            CANDIDATE {String.fromCharCode(65 + idx)}
                          </span>
                          <span className="font-mono text-[9px]" style={{ color: 'var(--sov-text-dim)' }}>
                            PAGE {c.page_index + 1}
                          </span>
                        </div>
                        <div className="font-mono text-lg font-bold text-white">
                          {c.value}
                        </div>
                      </div>
                      <div className="sov-btn-primary rounded text-[10px] opacity-0 group-hover:opacity-100 transition-opacity"
                           style={{ padding: '6px 12px' }}>
                        ACCEPT
                      </div>
                    </div>
                  </button>
                ))}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* ── Footer Actions ─────────────────────────────────────────── */}
      {result.status === 'completed' && result.data && Object.keys(result.data).length > 0 && (
        <div className="p-4 space-y-2" style={{ borderTop: '1px solid var(--sov-border)' }}>
          <button className="w-full sov-btn-primary rounded text-[11px]">
            EXPORT TO ERP ↗
          </button>
          <div className="flex gap-2">
            <button className="flex-1 sov-btn-outline rounded text-[10px]" style={{ padding: '8px' }}>
              DISCARD
            </button>
            <button className="flex-1 sov-btn-outline rounded text-[10px]" style={{ padding: '8px' }}>
              RE-SCAN
            </button>
          </div>
        </div>
      )}
    </div>
  );
};

export default ResultsPanel;
