import React, { useState, useRef, useCallback, useEffect } from 'react';
import { Stage, Layer, Image as KonvaImage, Rect, Text, Group } from 'react-konva';
import useImage from 'use-image';
import { v4 as uuidv4 } from 'uuid';
import type { Anchor, FieldOption } from '../types';
import { FIELD_OPTIONS } from '../types';

interface Props {
  documentUrl: string | null;
  vendorId: string;
  anchors: Anchor[];
  onAnchorsChange: (anchors: Anchor[]) => void;
  onSubmit: (anchors: Anchor[]) => void;
  hasTemplates: boolean;
  onAutoExtract: () => void;
  currentPage: number;
  pageCount: number;
  onPageChange: (page: number) => void;
}

function buildSemanticPrompt(field: string, norm_x: number, norm_y: number): string {
  const label = field.replace(/_/g, ' ');
  return `Extract the "${label}" value from coordinates (${norm_x.toFixed(3)}, ${norm_y.toFixed(3)}).`;
}

const MIN_SCALE = 0.5;
const MAX_SCALE = 4.0;
const SCALE_STEP = 0.1;

const SemanticCanvas: React.FC<Props> = ({
  documentUrl,
  vendorId,
  anchors,
  onAnchorsChange,
  onSubmit,
  hasTemplates,
  onAutoExtract,
  currentPage,
  pageCount,
  onPageChange,
}) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const [image] = useImage(documentUrl || '', 'anonymous');
  const [selectedField, setSelectedField] = useState<FieldOption>('invoice_total');
  const [stageSize, setStageSize] = useState({ width: 800, height: 600 });
  const [scale, setScale] = useState(1);
  const [position, setPosition] = useState({ x: 0, y: 0 });
  const [isPanning, setIsPanning] = useState(false);
  const lastPointerRef = useRef({ x: 0, y: 0 });

  // Rectangle drag state
  const [isDrawing, setIsDrawing] = useState(false);
  const [drawStart, setDrawStart] = useState<{ x: number; y: number } | null>(null);
  const [drawEnd, setDrawEnd] = useState<{ x: number; y: number } | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        setStageSize({ width: entry.contentRect.width, height: entry.contentRect.height });
      }
    });
    observer.observe(containerRef.current);
    return () => observer.disconnect();
  }, []);

  const getImageDimensions = useCallback(() => {
    if (!image) return { width: stageSize.width, height: stageSize.height, offsetX: 0, offsetY: 0 };
    const imgRatio = image.width / image.height;
    const stageRatio = stageSize.width / stageSize.height;
    let width: number, height: number;
    if (imgRatio > stageRatio) {
      width = stageSize.width;
      height = stageSize.width / imgRatio;
    } else {
      height = stageSize.height;
      width = stageSize.height * imgRatio;
    }
    return {
      width, height,
      offsetX: (stageSize.width - width) / 2,
      offsetY: (stageSize.height - height) / 2,
    };
  }, [image, stageSize]);

  // Transform pointer position to image-space coordinates
  const getTransformed = useCallback((pos: { x: number; y: number }) => {
    return {
      x: (pos.x - position.x) / scale,
      y: (pos.y - position.y) / scale,
    };
  }, [position, scale]);

  // ── Mouse handlers for rectangle drawing ──────────────────────────
  const handleMouseDown = useCallback((e: any) => {
    // Shift = pan mode
    if (e.evt.shiftKey || e.evt.button === 1) {
      setIsPanning(true);
      const pos = e.target.getStage()?.getPointerPosition();
      if (pos) lastPointerRef.current = pos;
      return;
    }

    if (!image) return;
    const stage = e.target.getStage();
    const pos = stage?.getPointerPosition();
    if (!pos) return;

    const transformed = getTransformed(pos);
    const dims = getImageDimensions();

    // Only start drawing if within image bounds
    if (transformed.x >= dims.offsetX && transformed.x <= dims.offsetX + dims.width &&
        transformed.y >= dims.offsetY && transformed.y <= dims.offsetY + dims.height) {
      setIsDrawing(true);
      setDrawStart(transformed);
      setDrawEnd(transformed);
    }
  }, [image, getTransformed, getImageDimensions]);

  const handleMouseMove = useCallback((e: any) => {
    if (isPanning) {
      const pos = e.target.getStage()?.getPointerPosition();
      if (!pos) return;
      setPosition((prev) => ({
        x: prev.x + pos.x - lastPointerRef.current.x,
        y: prev.y + pos.y - lastPointerRef.current.y,
      }));
      lastPointerRef.current = pos;
      return;
    }

    if (isDrawing) {
      const pos = e.target.getStage()?.getPointerPosition();
      if (!pos) return;
      setDrawEnd(getTransformed(pos));
    }
  }, [isPanning, isDrawing, getTransformed]);

  const handleMouseUp = useCallback(() => {
    if (isPanning) {
      setIsPanning(false);
      return;
    }

    if (isDrawing && drawStart && drawEnd) {
      const dims = getImageDimensions();
      const minSize = 8; // minimum drag distance in pixels

      const rectW = Math.abs(drawEnd.x - drawStart.x);
      const rectH = Math.abs(drawEnd.y - drawStart.y);

      if (rectW > minSize || rectH > minSize) {
        // Calculate center of the rectangle as the anchor point
        const centerX = (drawStart.x + drawEnd.x) / 2;
        const centerY = (drawStart.y + drawEnd.y) / 2;

        // Normalize relative to image
        const norm_x = (centerX - dims.offsetX) / dims.width;
        const norm_y = (centerY - dims.offsetY) / dims.height;

        // Clamp to [0, 1]
        const clamped_x = Math.max(0, Math.min(1, norm_x));
        const clamped_y = Math.max(0, Math.min(1, norm_y));

        const newAnchor: Anchor = {
          id: uuidv4(),
          field: selectedField,
          prompt: buildSemanticPrompt(selectedField, clamped_x, clamped_y),
          x_pct: clamped_x,
          y_pct: clamped_y,
          pixelX: centerX,
          pixelY: centerY,
          // Store rect bounds for rendering
          rectX: Math.min(drawStart.x, drawEnd.x),
          rectY: Math.min(drawStart.y, drawEnd.y),
          rectW: rectW,
          rectH: rectH,
        } as any;

        const updated = anchors.filter((a) => a.field !== selectedField);
        updated.push(newAnchor);
        onAnchorsChange(updated);
      }
    }

    setIsDrawing(false);
    setDrawStart(null);
    setDrawEnd(null);
  }, [isPanning, isDrawing, drawStart, drawEnd, selectedField, anchors, onAnchorsChange, getImageDimensions]);

  const handleWheel = useCallback((e: any) => {
    e.evt.preventDefault();
    const delta = e.evt.deltaY > 0 ? -SCALE_STEP : SCALE_STEP;
    setScale(Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale + delta)));
  }, [scale]);

  const dims = getImageDimensions();

  // Recalculate anchor rectangles from normalized coords when image changes
  const anchorsWithPixels = anchors.map((a: any) => {
    const cx = dims.offsetX + a.x_pct * dims.width;
    const cy = dims.offsetY + a.y_pct * dims.height;
    // Default rect size if not stored (backward compat)
    const rw = a.rectW || dims.width * 0.15;
    const rh = a.rectH || dims.height * 0.05;
    return {
      ...a,
      pixelX: cx,
      pixelY: cy,
      rectX: a.rectX || (cx - rw / 2),
      rectY: a.rectY || (cy - rh / 2),
      rectW: rw,
      rectH: rh,
    };
  });

  const removeAnchor = (field: string) => {
    onAnchorsChange(anchors.filter((a) => a.field !== field));
  };

  // Current draw rectangle
  const drawRect = (isDrawing && drawStart && drawEnd) ? {
    x: Math.min(drawStart.x, drawEnd.x),
    y: Math.min(drawStart.y, drawEnd.y),
    w: Math.abs(drawEnd.x - drawStart.x),
    h: Math.abs(drawEnd.y - drawStart.y),
  } : null;

  return (
    <div className="flex flex-col h-full" style={{ background: 'var(--sov-bg-deepest)' }}>
      {/* ── Top Toolbar ──────────────────────────────────────────────── */}
      <div className="flex items-center gap-3 px-4 py-2.5"
           style={{ background: 'var(--sov-bg-panel)', borderBottom: '1px solid var(--sov-border)' }}>
        <div className="text-label" style={{ color: 'var(--sov-text-dim)' }}>FIELD:</div>
        <select
          id="field-selector"
          value={selectedField}
          onChange={(e) => setSelectedField(e.target.value as FieldOption)}
          className="font-mono text-xs text-white px-3 py-1.5 rounded focus:outline-none"
          style={{ background: 'var(--sov-bg-surface)', border: '1px solid var(--sov-border)' }}
        >
          {FIELD_OPTIONS.map((f) => (
            <option key={f} value={f}>{f.toUpperCase().replace(/_/g, ' ')}</option>
          ))}
        </select>

        {/* Page navigation */}
        {pageCount > 1 && (
          <div className="flex items-center gap-2 ml-2">
            <button
              onClick={() => onPageChange(Math.max(0, currentPage - 1))}
              disabled={currentPage === 0}
              className="font-mono text-[10px] px-2 py-1 rounded transition-colors disabled:opacity-30"
              style={{ color: 'var(--sov-text-secondary)', background: 'var(--sov-bg-surface)', border: '1px solid var(--sov-border)' }}
            >
              ◀ PREV
            </button>
            <span className="font-mono text-[10px] font-medium" style={{ color: 'var(--sov-text-secondary)' }}>
              PAGE {currentPage + 1} / {pageCount}
            </span>
            <button
              onClick={() => onPageChange(Math.min(pageCount - 1, currentPage + 1))}
              disabled={currentPage >= pageCount - 1}
              className="font-mono text-[10px] px-2 py-1 rounded transition-colors disabled:opacity-30"
              style={{ color: 'var(--sov-text-secondary)', background: 'var(--sov-bg-surface)', border: '1px solid var(--sov-border)' }}
            >
              NEXT ▶
            </button>
          </div>
        )}

        <div className="flex-1" />

        <span className="font-mono text-[10px]" style={{ color: 'var(--sov-text-dim)' }}>
          ZOOM: {Math.round(scale * 100)}% · SHIFT+DRAG TO PAN · DRAG TO SELECT AREA
        </span>

        <button
          id="reset-view-btn"
          onClick={() => { setScale(1); setPosition({ x: 0, y: 0 }); }}
          className="sov-btn-outline rounded text-[10px]"
          style={{ padding: '4px 10px' }}
        >
          RESET VIEW
        </button>
      </div>

      {/* ── Canvas ───────────────────────────────────────────────────── */}
      <div ref={containerRef} className="flex-1 relative overflow-hidden"
           style={{ background: 'var(--sov-bg-deepest)' }}>
        {!documentUrl ? (
          <div className="absolute inset-0 flex items-center justify-center">
            <div className="text-center space-y-3">
              <div className="w-16 h-16 mx-auto rounded flex items-center justify-center"
                   style={{ background: 'var(--sov-bg-surface)', border: '1px solid var(--sov-border)' }}>
                <svg className="w-8 h-8" fill="none" viewBox="0 0 24 24" stroke="currentColor"
                     style={{ color: 'var(--sov-accent-blue)' }}>
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
                    d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                </svg>
              </div>
              <div>
                <p className="font-mono text-sm font-bold text-white">SYSTEM READY</p>
                <p className="font-mono text-[10px] mt-1" style={{ color: 'var(--sov-text-dim)' }}>
                  Select a document or drop a high-resolution PDF to begin<br/>
                  extraction using the Sovereign model.
                </p>
              </div>
            </div>
          </div>
        ) : (
          <Stage
            width={stageSize.width} height={stageSize.height}
            scaleX={scale} scaleY={scale}
            x={position.x} y={position.y}
            onWheel={handleWheel}
            onMouseDown={handleMouseDown}
            onMouseMove={handleMouseMove}
            onMouseUp={handleMouseUp}
            style={{ cursor: isPanning ? 'grabbing' : isDrawing ? 'crosshair' : 'crosshair' }}
          >
            {/* Layer 1: Document image */}
            <Layer>
              {image && (
                <KonvaImage
                  image={image}
                  x={dims.offsetX} y={dims.offsetY}
                  width={dims.width} height={dims.height}
                />
              )}
            </Layer>

            {/* Layer 2: Existing anchor rectangles */}
            <Layer>
              {anchorsWithPixels.map((anchor: any) => (
                <Group key={anchor.id}>
                  {/* Selection rectangle */}
                  <Rect
                    x={anchor.rectX} y={anchor.rectY}
                    width={anchor.rectW} height={anchor.rectH}
                    fill="rgba(59, 130, 246, 0.08)"
                    stroke="#3b82f6"
                    strokeWidth={1.5}
                    dash={[4, 3]}
                  />
                  {/* Label tag */}
                  <Rect
                    x={anchor.rectX} y={anchor.rectY - 16}
                    width={Math.max(anchor.rectW, 120)} height={16}
                    fill="rgba(59, 130, 246, 0.9)"
                    cornerRadius={[2, 2, 0, 0]}
                  />
                  <Text
                    text={`FIELD: ${anchor.field.toUpperCase().replace(/_/g, '_')}`}
                    x={anchor.rectX + 4} y={anchor.rectY - 14}
                    fontSize={9}
                    fontFamily="JetBrains Mono, monospace"
                    fontStyle="bold"
                    fill="white"
                  />
                </Group>
              ))}

              {/* Currently drawing rectangle */}
              {drawRect && (
                <Group>
                  <Rect
                    x={drawRect.x} y={drawRect.y}
                    width={drawRect.w} height={drawRect.h}
                    fill="rgba(59, 130, 246, 0.12)"
                    stroke="#60a5fa"
                    strokeWidth={2}
                    dash={[6, 3]}
                  />
                  <Rect
                    x={drawRect.x} y={drawRect.y - 16}
                    width={120} height={16}
                    fill="rgba(96, 165, 250, 0.9)"
                    cornerRadius={[2, 2, 0, 0]}
                  />
                  <Text
                    text={selectedField.toUpperCase().replace(/_/g, '_')}
                    x={drawRect.x + 4} y={drawRect.y - 14}
                    fontSize={9}
                    fontFamily="JetBrains Mono, monospace"
                    fontStyle="bold"
                    fill="white"
                  />
                </Group>
              )}
            </Layer>
          </Stage>
        )}
      </div>

      {/* ── Bottom Bar: Anchors + Actions ────────────────────────────── */}
      <div className="px-4 py-3" style={{ background: 'var(--sov-bg-panel)', borderTop: '1px solid var(--sov-border)' }}>
        {/* Anchor chips */}
        {anchors.length > 0 && (
          <div className="flex flex-wrap gap-2 mb-3">
            {anchors.map((a) => (
              <span key={a.id}
                className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded font-mono text-[10px] font-semibold"
                style={{ background: 'rgba(59, 130, 246, 0.12)', border: '1px solid rgba(59, 130, 246, 0.25)', color: '#60a5fa' }}>
                <span className="w-1.5 h-1.5 rounded-full" style={{ background: '#3b82f6' }} />
                {a.field.toUpperCase().replace(/_/g, '_')}
                <button onClick={() => removeAnchor(a.field)}
                  className="ml-1 hover:text-white transition-colors">×</button>
              </span>
            ))}
          </div>
        )}

        {/* Action buttons */}
        <div className="flex gap-3">
          {anchors.length > 0 && (
            <button
              id="submit-extraction-btn"
              onClick={() => onSubmit(anchors)}
              className="flex-1 sov-btn-primary rounded"
            >
              EXTRACT {anchors.length} FIELD{anchors.length > 1 ? 'S' : ''}
            </button>
          )}
          {hasTemplates && (
            <button
              id="auto-extract-btn"
              onClick={onAutoExtract}
              className="flex-1 rounded font-mono text-xs font-bold tracking-wider uppercase"
              style={{
                background: 'linear-gradient(135deg, #059669, #10b981)',
                color: 'white', padding: '12px 24px', border: 'none', cursor: 'pointer',
              }}
            >
              ⚡ AUTO-EXTRACT
            </button>
          )}
        </div>

        {/* Status bar */}
        <div className="flex items-center justify-between mt-2">
          <span className="font-mono text-[9px]" style={{ color: 'var(--sov-text-dim)' }}>
            <span className="status-dot status-dot-active mr-1" style={{ width: 5, height: 5 }} />
            ENGINE: LIVE
          </span>
          <span className="font-mono text-[9px]" style={{ color: 'var(--sov-text-dim)' }}>
            {anchors.length} ANCHOR{anchors.length !== 1 ? 'S' : ''} PLACED · LATENCY: 14MS
          </span>
        </div>
      </div>
    </div>
  );
};

export default SemanticCanvas;
