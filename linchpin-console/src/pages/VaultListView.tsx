import { useEffect, useState, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { apiClient, ApiError } from '../api/client';
import type { Vault, PaginatedResponse } from '../types';
import LoadingSpinner from '../components/LoadingSpinner';
import ErrorMessage from '../components/ErrorMessage';
import Pagination from '../components/Pagination';
import ConfirmDialog from '../components/ConfirmDialog';
import './VaultListView.css';

function formatRelativeTime(dateStr: string): string {
  const now = Date.now();
  const then = new Date(dateStr).getTime();
  const diffMs = now - then;
  const diffSec = Math.floor(diffMs / 1000);

  if (diffSec < 60) return 'just now';
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin} minute${diffMin === 1 ? '' : 's'} ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr} hour${diffHr === 1 ? '' : 's'} ago`;
  const diffDay = Math.floor(diffHr / 24);
  if (diffDay < 30) return `${diffDay} day${diffDay === 1 ? '' : 's'} ago`;

  return new Date(dateStr).toLocaleDateString();
}

export default function VaultListView() {
  const navigate = useNavigate();

  const [vaults, setVaults] = useState<Vault[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [nextCursor, setNextCursor] = useState<string | undefined>();
  const [hasMore, setHasMore] = useState(false);

  // Inline create form
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState('');
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  // Confirm dialogs
  const [deleteTarget, setDeleteTarget] = useState<Vault | null>(null);
  const [archiveTarget, setArchiveTarget] = useState<Vault | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const fetchVaults = useCallback(async (cursor?: string) => {
    try {
      const params: Record<string, string> = {};
      if (cursor) params.cursor = cursor;

      const res = await apiClient.get<PaginatedResponse<Vault>>('/v1/vaults', params);
      if (cursor) {
        setVaults(prev => [...prev, ...res.data]);
      } else {
        setVaults(res.data);
      }
      setNextCursor(res.next_cursor);
      setHasMore(res.has_more);
      setError(null);
    } catch (err) {
      setError(err as ApiError);
    }
  }, []);

  useEffect(() => {
    setLoading(true);
    fetchVaults().finally(() => setLoading(false));
  }, [fetchVaults]);

  const handleLoadMore = async () => {
    if (!nextCursor) return;
    setLoadingMore(true);
    await fetchVaults(nextCursor);
    setLoadingMore(false);
  };

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newName.trim()) return;
    setCreating(true);
    setCreateError(null);
    try {
      await apiClient.post<Vault>('/v1/vaults', { display_name: newName.trim() });
      setNewName('');
      setShowCreate(false);
      setLoading(true);
      await fetchVaults();
      setLoading(false);
    } catch (err) {
      const apiErr = err as ApiError;
      setCreateError(apiErr.message || 'Failed to create vault');
    } finally {
      setCreating(false);
    }
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    setActionError(null);
    try {
      await apiClient.delete(`/v1/vaults/${deleteTarget.id}`);
      setVaults(prev => prev.filter(v => v.id !== deleteTarget.id));
    } catch (err) {
      const apiErr = err as ApiError;
      setActionError(apiErr.message || 'Failed to delete vault');
    } finally {
      setDeleteTarget(null);
    }
  };

  const handleArchive = async () => {
    if (!archiveTarget) return;
    setActionError(null);
    try {
      await apiClient.post(`/v1/vaults/${archiveTarget.id}/archive`, {});
      setVaults(prev =>
        prev.map(v =>
          v.id === archiveTarget.id ? { ...v, archived_at: new Date().toISOString() } : v,
        ),
      );
    } catch (err) {
      const apiErr = err as ApiError;
      setActionError(apiErr.message || 'Failed to archive vault');
    } finally {
      setArchiveTarget(null);
    }
  };

  if (loading) return <LoadingSpinner />;

  if (error) {
    return (
      <ErrorMessage
        status={error.status}
        message={error.message}
        onRetry={() => {
          setError(null);
          setLoading(true);
          fetchVaults().finally(() => setLoading(false));
        }}
      />
    );
  }

  return (
    <div className="vault-list-view" data-testid="vault-list-view">
      <div className="vault-list-header">
        <div>
          <h1>Credential Vaults</h1>
          <p>Manage encrypted credential vaults for sessions.</p>
        </div>
        <button
          className="btn-primary"
          data-testid="new-vault-btn"
          onClick={() => setShowCreate(s => !s)}
        >
          {showCreate ? 'Cancel' : 'New vault'}
        </button>
      </div>

      {showCreate && (
        <form className="inline-create-form" onSubmit={handleCreate} data-testid="vault-create-form">
          <div className="form-field">
            <label htmlFor="vault-name">Display Name</label>
            <input
              id="vault-name"
              type="text"
              value={newName}
              onChange={e => setNewName(e.target.value)}
              placeholder="e.g. Production Keys"
              required
              data-testid="vault-name-input"
            />
          </div>
          <button
            type="submit"
            className="btn-primary"
            disabled={creating || !newName.trim()}
            data-testid="vault-create-submit"
          >
            {creating ? 'Creating…' : 'Create'}
          </button>
        </form>
      )}
      {createError && <div className="inline-create-error" role="alert">{createError}</div>}
      {actionError && <div className="inline-create-error" role="alert">{actionError}</div>}

      <div className="vault-list-table-wrapper">
        <table className="vault-list-table" data-testid="vault-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Created</th>
              <th>Status</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {vaults.length === 0 ? (
              <tr>
                <td colSpan={4} className="empty-row">No vaults yet.</td>
              </tr>
            ) : (
              vaults.map(vault => (
                <tr
                  key={vault.id}
                  className="vault-row"
                  data-testid={`vault-row-${vault.id}`}
                  onClick={() => navigate(`/vaults/${vault.id}`)}
                  role="link"
                  tabIndex={0}
                  onKeyDown={e => {
                    if (e.key === 'Enter') navigate(`/vaults/${vault.id}`);
                  }}
                >
                  <td data-testid="vault-cell-name">{vault.display_name}</td>
                  <td data-testid="vault-cell-created">{formatRelativeTime(vault.created_at)}</td>
                  <td data-testid="vault-cell-status">
                    {vault.archived_at ? (
                      <span className="archived-badge">Archived</span>
                    ) : (
                      'Active'
                    )}
                  </td>
                  <td>
                    <div
                      className="vault-actions-cell"
                      onClick={e => e.stopPropagation()}
                      onKeyDown={e => e.stopPropagation()}
                    >
                      {!vault.archived_at && (
                        <button
                          className="btn-sm-secondary"
                          onClick={() => setArchiveTarget(vault)}
                          data-testid="vault-archive-btn"
                        >
                          Archive
                        </button>
                      )}
                      <button
                        className="btn-sm-danger"
                        onClick={() => setDeleteTarget(vault)}
                        data-testid="vault-delete-btn"
                      >
                        Delete
                      </button>
                    </div>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      <Pagination hasMore={hasMore} onLoadMore={handleLoadMore} loading={loadingMore} />

      <ConfirmDialog
        open={!!deleteTarget}
        title="Delete Vault"
        message={`Are you sure you want to permanently delete "${deleteTarget?.display_name}"? This will also delete all its credentials.`}
        onConfirm={handleDelete}
        onCancel={() => setDeleteTarget(null)}
      />

      <ConfirmDialog
        open={!!archiveTarget}
        title="Archive Vault"
        message={`Are you sure you want to archive "${archiveTarget?.display_name}"? Encrypted secrets will be purged.`}
        onConfirm={handleArchive}
        onCancel={() => setArchiveTarget(null)}
      />
    </div>
  );
}
