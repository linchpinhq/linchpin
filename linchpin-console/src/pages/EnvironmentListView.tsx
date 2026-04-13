import { useEffect, useState, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { apiClient, ApiError } from '../api/client';
import type { Environment, PaginatedResponse } from '../types';
import LoadingSpinner from '../components/LoadingSpinner';
import ErrorMessage from '../components/ErrorMessage';
import Pagination from '../components/Pagination';
import './EnvironmentListView.css';

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

export default function EnvironmentListView() {
  const navigate = useNavigate();

  const [environments, setEnvironments] = useState<Environment[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [nextCursor, setNextCursor] = useState<string | undefined>();
  const [hasMore, setHasMore] = useState(false);

  const fetchEnvironments = useCallback(async (cursor?: string) => {
    try {
      const params: Record<string, string> = {};
      if (cursor) params.cursor = cursor;

      const res = await apiClient.get<PaginatedResponse<Environment>>('/v1/environments', params);
      if (cursor) {
        setEnvironments(prev => [...prev, ...res.data]);
      } else {
        setEnvironments(res.data);
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
    fetchEnvironments().finally(() => setLoading(false));
  }, [fetchEnvironments]);

  const handleLoadMore = async () => {
    if (!nextCursor) return;
    setLoadingMore(true);
    await fetchEnvironments(nextCursor);
    setLoadingMore(false);
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
          fetchEnvironments().finally(() => setLoading(false));
        }}
      />
    );
  }

  return (
    <div className="environment-list-view" data-testid="environment-list-view">
      <div className="environment-list-header">
        <div>
          <h1>Environments</h1>
          <p>Create and manage container environments.</p>
        </div>
        <button
          className="btn-primary"
          data-testid="new-environment-btn"
          onClick={() => navigate('/environments/new')}
        >
          New environment
        </button>
      </div>

      <div className="environment-list-table-wrapper">
        <table className="environment-list-table" data-testid="environment-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>Name</th>
              <th>Networking Type</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {environments.length === 0 ? (
              <tr>
                <td colSpan={4} className="empty-row">
                  No environments yet.
                </td>
              </tr>
            ) : (
              environments.map(env => (
                <tr
                  key={env.id}
                  className="environment-row"
                  data-testid={`environment-row-${env.id}`}
                  onClick={() => navigate(`/environments/${env.id}`)}
                  role="link"
                  tabIndex={0}
                  onKeyDown={e => {
                    if (e.key === 'Enter') navigate(`/environments/${env.id}`);
                  }}
                >
                  <td className="cell-id" data-testid="environment-cell-id">
                    <code>{env.id}</code>
                  </td>
                  <td data-testid="environment-cell-name">{env.name}</td>
                  <td data-testid="environment-cell-networking">{env.config.networking.type}</td>
                  <td data-testid="environment-cell-created">
                    {formatRelativeTime(env.created_at)}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      <Pagination hasMore={hasMore} onLoadMore={handleLoadMore} loading={loadingMore} />
    </div>
  );
}
