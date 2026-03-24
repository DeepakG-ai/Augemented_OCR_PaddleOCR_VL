import React, { useState } from 'react';
import type { Vendor } from '../types';

interface Props {
  vendors: Vendor[];
  selectedVendor: Vendor | null;
  onSelect: (vendor: Vendor) => void;
  onCreate: (name: string) => Promise<Vendor | null>;
  loading: boolean;
}

const VendorSelector: React.FC<Props> = ({
  vendors,
  selectedVendor,
  onSelect,
  onCreate,
  loading,
}) => {
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState('');
  const [creating, setCreating] = useState(false);

  const handleCreate = async () => {
    if (!newName.trim()) return;
    setCreating(true);
    const vendor = await onCreate(newName.trim());
    if (vendor) {
      onSelect(vendor);
      setNewName('');
      setShowCreate(false);
    }
    setCreating(false);
  };

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <div className="text-label" style={{ color: 'var(--sov-accent-blue)' }}>Active Vendors</div>
        {loading && (
          <span className="font-mono text-[10px]" style={{ color: 'var(--sov-text-dim)' }}>
            LOADING...
          </span>
        )}
      </div>

      {/* Vendor list */}
      <div className="space-y-1">
        {vendors.map((v) => {
          const isActive = selectedVendor?.id === v.id;
          return (
            <button
              key={v.id}
              onClick={() => onSelect(v)}
              className={`w-full text-left px-3 py-2.5 rounded transition-all duration-150 ${
                isActive ? 'sov-card-active' : 'sov-card'
              }`}
              style={isActive ? {
                background: 'var(--sov-bg-elevated)',
                borderLeft: '3px solid var(--sov-accent-blue)',
              } : undefined}
            >
              <div className="flex items-center justify-between">
                <div>
                  <div className={`font-mono text-xs font-bold ${isActive ? 'text-white' : ''}`}
                       style={!isActive ? { color: 'var(--sov-text-secondary)' } : undefined}>
                    {v.name.toUpperCase()}
                  </div>
                  <div className="font-mono text-[9px] mt-0.5" style={{ color: 'var(--sov-text-dim)' }}>
                    ID: {v.id.substring(0, 8).toUpperCase()}
                  </div>
                </div>
                <span className={`sov-badge ${isActive ? 'sov-badge-green' : 'sov-badge-blue'}`}>
                  {isActive ? 'ACTIVE' : 'IDLE'}
                </span>
              </div>
            </button>
          );
        })}

        {vendors.length === 0 && !loading && (
          <div className="text-center py-4">
            <p className="font-mono text-[10px]" style={{ color: 'var(--sov-text-dim)' }}>
              NO VENDORS REGISTERED
            </p>
          </div>
        )}
      </div>

      {/* Create vendor */}
      {!showCreate ? (
        <button
          id="create-vendor-toggle"
          onClick={() => setShowCreate(true)}
          className="flex items-center gap-2 font-mono text-[11px] font-medium transition-colors"
          style={{ color: 'var(--sov-accent-blue)' }}
        >
          <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
          </svg>
          ADD NEW VENDOR
        </button>
      ) : (
        <div className="space-y-2 animate-in">
          <input
            id="new-vendor-name"
            type="text"
            placeholder="Vendor name"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleCreate()}
            className="w-full px-3 py-2 rounded font-mono text-xs text-white placeholder-gray-600
                       focus:outline-none focus:ring-1"
            style={{
              background: 'var(--sov-bg-surface)',
              border: '1px solid var(--sov-border)',
            }}
          />
          <div className="flex gap-2">
            <button
              id="create-vendor-submit"
              onClick={handleCreate}
              disabled={creating || !newName.trim()}
              className="flex-1 sov-btn-primary rounded text-[11px] disabled:opacity-40"
              style={{ padding: '8px 12px' }}
            >
              {creating ? 'CREATING...' : 'CREATE'}
            </button>
            <button
              onClick={() => { setShowCreate(false); setNewName(''); }}
              className="sov-btn-outline rounded text-[11px]"
              style={{ padding: '8px 12px' }}
            >
              CANCEL
            </button>
          </div>
        </div>
      )}
    </div>
  );
};

export default VendorSelector;
