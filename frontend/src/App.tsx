import React, { useState, useCallback, useRef } from 'react';
import SemanticCanvas from './components/SemanticCanvas';
import ResultsPanel from './components/ResultsPanel';
import VendorSelector from './components/VendorSelector';
import { useUpload, useExtraction, useVendors, useTemplates } from './hooks/useExtraction';
import type { Anchor, AnchorInput, Vendor } from './types';

const App: React.FC = () => {
  // State
  const [selectedVendor, setSelectedVendor] = useState<Vendor | null>(null);
  const [anchors, setAnchors] = useState<Anchor[]>([]);
  const [documentUrl, setDocumentUrl] = useState<string | null>(null);
  const [currentPage, setCurrentPage] = useState(0);
  const [pageCount, setPageCount] = useState(1);
  const [validationError, setValidationError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Hooks
  const { upload, uploading, uploadResult, error: uploadError, setUploadResult } = useUpload();
  const { submitExtraction, submitting, jobStatus, error: extractError } = useExtraction();
  const { vendors, loading: vendorsLoading, createVendor } = useVendors();
  const { templates, loading: templatesLoading } = useTemplates(selectedVendor?.id || null);

  const hasTemplates = templates.length > 0;

  // Build the page image URL from API
  const buildPageUrl = useCallback((s3Key: string, page: number) => {
    return `/api/pages?s3_key=${encodeURIComponent(s3Key)}&page=${page}&dpi=200`;
  }, []);

  // Handle file upload
  const handleFileSelect = useCallback(async (file: File) => {
    setValidationError(null);
    const result = await upload(file);
    if (result) {
      const url = buildPageUrl(result.s3_key, 0);
      setDocumentUrl(url);
      setAnchors([]);
      setCurrentPage(0);
      try {
        const res = await fetch(`/api/pages/count?s3_key=${encodeURIComponent(result.s3_key)}`);
        if (res.ok) {
          const data = await res.json();
          setPageCount(data.page_count);
        }
      } catch { setPageCount(1); }
    }
  }, [upload, buildPageUrl]);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    const file = e.dataTransfer.files[0];
    if (file) handleFileSelect(file);
  }, [handleFileSelect]);

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
  }, []);

  // Submit extraction — with vendor validation
  const handleSubmitExtraction = useCallback(async (submittedAnchors: Anchor[]) => {
    if (!selectedVendor) {
      setValidationError('SELECT A VENDOR BEFORE EXTRACTION');
      return;
    }
    if (!uploadResult) {
      setValidationError('UPLOAD A DOCUMENT FIRST');
      return;
    }
    setValidationError(null);

    const anchorInputs: AnchorInput[] = submittedAnchors.map((a) => ({
      field: a.field,
      prompt: a.prompt,
      x_pct: a.x_pct,
      y_pct: a.y_pct,
    }));

    await submitExtraction(uploadResult.s3_key, selectedVendor.id, anchorInputs, true);
  }, [uploadResult, selectedVendor, submitExtraction]);

  // Auto-extract (zero-touch path)
  const handleAutoExtract = useCallback(async () => {
    if (!selectedVendor) {
      setValidationError('SELECT A VENDOR BEFORE EXTRACTION');
      return;
    }
    if (!uploadResult) {
      setValidationError('UPLOAD A DOCUMENT FIRST');
      return;
    }
    setValidationError(null);
    await submitExtraction(uploadResult.s3_key, selectedVendor.id, [], true);
  }, [uploadResult, selectedVendor, submitExtraction]);

  // Page navigation
  const handlePageChange = useCallback((page: number) => {
    if (!uploadResult) return;
    setCurrentPage(page);
    setDocumentUrl(buildPageUrl(uploadResult.s3_key, page));
  }, [uploadResult, buildPageUrl]);

  // Determine system status
  const getSystemStatus = () => {
    if (submitting || jobStatus?.status === 'processing') return { text: 'PROCESSING', color: 'text-amber-400' };
    if (jobStatus?.status === 'failed') return { text: 'ERROR', color: 'text-red-400' };
    return { text: 'OPTIMAL', color: 'text-emerald-400' };
  };
  const sysStatus = getSystemStatus();

  return (
    <div className="h-screen flex flex-col" style={{ background: 'var(--sov-bg-deepest)' }}>
      {/* ── Sovereign Header Bar ─────────────────────────────────────── */}
      <header className="flex-none flex items-center justify-between px-6 py-3 sov-panel"
              style={{ borderTop: 'none', borderLeft: 'none', borderRight: 'none' }}>
        <div className="flex items-center gap-8">
          <span className="font-mono text-sm font-bold tracking-wider"
                style={{ color: 'var(--sov-accent-blue)' }}>
            AUGMENTED OCR
          </span>
          <nav className="flex items-center gap-6">
            <span className="font-mono text-xs font-semibold tracking-wider text-white cursor-pointer"
                  style={{ borderBottom: '2px solid var(--sov-accent-blue)', paddingBottom: '2px' }}>
              EXTRACTION
            </span>
            <span className="font-mono text-xs font-semibold tracking-wider cursor-pointer"
                  style={{ color: 'var(--sov-text-muted)' }}>
              HISTORY
            </span>
          </nav>
        </div>

        <div className="flex items-center gap-4">
          {/* Status indicators */}
          {(uploading || submitting || jobStatus?.status === 'processing') && (
            <span className="flex items-center gap-2 font-mono text-xs text-amber-400">
              <span className="status-dot status-dot-pending status-dot-pulse" />
              {uploading ? 'UPLOADING...' : submitting ? 'SUBMITTING...' : 'PROCESSING...'}
            </span>
          )}

          <span className="font-mono text-xs tracking-wider" style={{ color: 'var(--sov-text-muted)' }}>
            SYSTEM STATUS:
          </span>
          <span className={`font-mono text-xs font-bold tracking-wider ${sysStatus.color}`}>
            {sysStatus.text}
          </span>

          {/* Settings icon */}
          <button className="p-2 rounded" style={{ color: 'var(--sov-text-muted)' }}>
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
            </svg>
          </button>
        </div>
      </header>

      {/* ── Main 3-Panel Layout ──────────────────────────────────────── */}
      <div className="flex-1 flex overflow-hidden">

        {/* ── Left Panel: Nav + Vendor + Upload ─────────────────────── */}
        <aside className="flex-none flex flex-col" style={{ width: '280px' }}>
          {/* Sovereign brand */}
          <div className="px-4 py-3" style={{ borderBottom: '1px solid var(--sov-border)' }}>
            <div className="font-mono text-xs font-bold tracking-wider text-white">SOVEREIGN</div>
            <div className="font-mono text-[10px] tracking-wider" style={{ color: 'var(--sov-accent-cyan)' }}>
              TERMINAL V1.0.4
            </div>
          </div>

          {/* Nav items */}
          <nav>
            <div className="nav-item nav-item-active">
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                  d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4" />
              </svg>
              VENDORS
            </div>
            <div className="nav-item">
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                  d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zm10 0a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2z" />
              </svg>
              FIELD PALETTE
            </div>
            <div className="nav-item">
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                  d="M9 3v2m6-2v2M9 19v2m6-2v2M5 9H3m2 6H3m18-6h-2m2 6h-2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2z" />
              </svg>
              EXTRACTION
            </div>
            <div className="nav-item">
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                  d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
              </svg>
              LOGS
            </div>
            <div className="nav-item">
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                  d="M5 8h14M5 8a2 2 0 110-4h14a2 2 0 110 4M5 8v10a2 2 0 002 2h10a2 2 0 002-2V8m-9 4h4" />
              </svg>
              ARCHIVE
            </div>
          </nav>

          {/* Vendor selector panel */}
          <div className="flex-1 overflow-y-auto p-4 space-y-4"
               style={{ borderTop: '1px solid var(--sov-border)' }}>
            <VendorSelector
              vendors={vendors}
              selectedVendor={selectedVendor}
              onSelect={(v) => { setSelectedVendor(v); setValidationError(null); }}
              onCreate={createVendor}
              loading={vendorsLoading}
            />

            {/* Document upload */}
            <div className="space-y-2">
              <div className="text-label">Document</div>
              <input
                ref={fileInputRef}
                type="file"
                accept="image/jpeg,image/png,application/pdf"
                className="hidden"
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) handleFileSelect(file);
                }}
              />
              <div
                id="upload-dropzone"
                className="upload-zone rounded p-5 text-center cursor-pointer"
                onClick={() => fileInputRef.current?.click()}
                onDrop={handleDrop}
                onDragOver={handleDragOver}
              >
                <svg className="w-8 h-8 mx-auto mb-2" fill="none" viewBox="0 0 24 24" stroke="currentColor"
                     style={{ color: 'var(--sov-accent-blue)' }}>
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
                    d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" />
                </svg>
                <p className="text-xs" style={{ color: 'var(--sov-text-secondary)' }}>
                  Drop file or <span style={{ color: 'var(--sov-accent-blue)' }}>browse</span>
                </p>
                <p className="text-[10px] mt-1" style={{ color: 'var(--sov-text-dim)' }}>PDF, JPEG, PNG</p>
              </div>

              {uploadResult && (
                <div className="p-3 rounded animate-in"
                     style={{ background: 'rgba(16, 185, 129, 0.06)', border: '1px solid rgba(16, 185, 129, 0.2)' }}>
                  <p className="font-mono text-xs font-medium truncate" style={{ color: 'var(--sov-accent-green)' }}>
                    ✓ {uploadResult.filename}
                  </p>
                  <p className="font-mono text-[10px] mt-1" style={{ color: 'var(--sov-text-dim)' }}>
                    {(uploadResult.size / 1024).toFixed(1)} KB
                  </p>
                </div>
              )}
            </div>

            {/* Stored rules */}
            {selectedVendor && !templatesLoading && (
              <div className="space-y-2 animate-in">
                <div className="text-label">Active Fields</div>
                {hasTemplates ? (
                  <div className="space-y-1">
                    {templates.map((t) => (
                      <div key={t.id} className="flex items-center justify-between px-3 py-2 sov-card">
                        <span className="flex items-center gap-2">
                          <span className="font-mono text-[10px]" style={{ color: 'var(--sov-text-dim)' }}>#</span>
                          <span className="font-mono text-xs" style={{ color: 'var(--sov-text-secondary)' }}>
                            {t.field_name.toUpperCase().replace(/_/g, '_')}
                          </span>
                        </span>
                        {t.page_index !== null ? (
                          <span className="sov-badge sov-badge-green">p.{t.page_index + 1}</span>
                        ) : (
                          <span className="sov-badge sov-badge-amber">any</span>
                        )}
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="font-mono text-[10px]" style={{ color: 'var(--sov-text-dim)' }}>
                    No rules yet — annotate first document
                  </p>
                )}
              </div>
            )}
          </div>

          {/* Errors + validation at bottom */}
          {(uploadError || extractError || validationError) && (
            <div className="p-3 m-3 rounded animate-in"
                 style={{ background: 'rgba(239, 68, 68, 0.08)', border: '1px solid rgba(239, 68, 68, 0.2)' }}>
              <p className="font-mono text-xs font-bold" style={{ color: 'var(--sov-accent-red)' }}>
                ⚠ {validationError || uploadError || extractError}
              </p>
            </div>
          )}

          {/* Bottom nav */}
          <div className="p-4" style={{ borderTop: '1px solid var(--sov-border)' }}>
            <div className="nav-item" style={{ padding: '6px 0' }}>
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                  d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
              </svg>
              SETTINGS
            </div>
            <div className="nav-item" style={{ padding: '6px 0' }}>
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                  d="M8.228 9c.549-1.165 2.03-2 3.772-2 2.21 0 4 1.343 4 3 0 1.4-1.278 2.575-3.006 2.907-.542.104-.994.54-.994 1.093m0 3h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
              SUPPORT
            </div>
          </div>
        </aside>

        {/* ── Center Panel: Document Canvas ──────────────────────────── */}
        <main className="flex-1 flex flex-col overflow-hidden"
              style={{ borderLeft: '1px solid var(--sov-border)', borderRight: '1px solid var(--sov-border)' }}>
          <SemanticCanvas
            documentUrl={documentUrl}
            vendorId={selectedVendor?.id || ''}
            anchors={anchors}
            onAnchorsChange={setAnchors}
            onSubmit={handleSubmitExtraction}
            hasTemplates={hasTemplates && !!uploadResult}
            onAutoExtract={handleAutoExtract}
            currentPage={currentPage}
            pageCount={pageCount}
            onPageChange={handlePageChange}
          />
        </main>

        {/* ── Right Panel: Results / Intelligence ───────────────────── */}
        <aside className="flex-none overflow-y-auto" style={{ width: '320px' }}>
          {/* Active entity header */}
          {selectedVendor && (
            <div className="p-4" style={{ borderBottom: '1px solid var(--sov-border)' }}>
              <div className="flex items-center justify-between">
                <div>
                  <div className="font-mono text-[10px] tracking-wider" style={{ color: 'var(--sov-text-dim)' }}>
                    ACTIVE ENTITY
                  </div>
                  <div className="font-mono text-sm font-bold text-white mt-1">
                    {selectedVendor.name.toUpperCase()}
                  </div>
                </div>
                {hasTemplates && (
                  <span className="sov-badge sov-badge-green">ZERO-TOUCH</span>
                )}
              </div>
            </div>
          )}

          <ResultsPanel
            result={jobStatus}
            vendorId={selectedVendor?.id || null}
          />
        </aside>
      </div>
    </div>
  );
};

export default App;
