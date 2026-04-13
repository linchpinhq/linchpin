import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { apiClient, ApiError } from '../api/client';
import type { Session } from '../types';
import LoadingSpinner from '../components/LoadingSpinner';
import ErrorMessage from '../components/ErrorMessage';
import StatusBadge from '../components/StatusBadge';
import EventStreamPanel from '../components/EventStreamPanel';
import MessageInput from '../components/MessageInput';
import ConfirmDialog from '../components/ConfirmDialog';
import './SessionDetailView.css';

const TERMINAL_STATUSES = ['terminated', 'failed'];

export default function SessionDetailView() {
  const { id } = useParams<{ id: string }>();
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);

  // Lifecycle action state
  const [showTerminateDialog, setShowTerminateDialog] = useState(false);
  const [terminating, setTerminating] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const fetchSession = async () => {
    try {
      setError(null);
      const data = await apiClient.get<Session>(`/v1/sessions/${id}`);
      setSession(data);
    } catch (err) {
      setError(err as ApiError);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    setLoading(true);
    fetchSession();
  }, [id]);

  const handleTerminate = async () => {
    if (!session || terminating) return;
    setTerminating(true);
    setActionError(null);
    setShowTerminateDialog(false);
    try {
      await apiClient.delete(`/v1/sessions/${session.id}`);
      setSession((prev) => prev ? { ...prev, status: 'terminated' } : prev);
    } catch (err) {
      const apiErr = err as ApiError;
      setActionError(apiErr.message || 'Failed to terminate session');
    } finally {
      setTerminating(false);
    }
  };

  const handleArchive = async () => {
    if (!session || archiving) return;
    setArchiving(true);
    setActionError(null);
    try {
      await apiClient.post(`/v1/sessions/${session.id}/archive`, {});
      setSession((prev) =>
        prev ? { ...prev, archived_at: new Date().toISOString() } : prev,
      );
    } catch (err) {
      const apiErr = err as ApiError;
      setActionError(apiErr.message || 'Failed to archive session');
    } finally {
      setArchiving(false);
    }
  };

  if (loading) return <LoadingSpinner />;

  if (error) {
    return (
      <ErrorMessage
        status={error.status}
        message={error.message}
        onRetry={() => {
          setLoading(true);
          fetchSession();
        }}
      />
    );
  }

  if (!session) return null;

  const isTerminal = TERMINAL_STATUSES.includes(session.status);
  const isArchived = !!session.archived_at;

  return (
    <div className="session-detail-view" data-testid="session-detail-view">
      <Link to="/sessions" className="back-link" data-testid="back-link">
        ← Sessions
      </Link>

      <div className="session-detail-header">
        <div className="session-detail-title-row">
          <h1 data-testid="session-title">{session.title || 'Untitled Session'}</h1>
          <StatusBadge status={session.status} />
          {isArchived && (
            <span className="archived-badge" data-testid="archived-badge">Archived</span>
          )}
        </div>
        <div className="session-detail-meta">
          <span data-testid="session-id">
            <code>{session.id}</code>
          </span>
          <span className="meta-separator">·</span>
          <span data-testid="session-created">
            Created {new Date(session.created_at).toLocaleString()}
          </span>
          <span className="meta-separator">·</span>
          <span data-testid="session-updated">
            Updated {new Date(session.updated_at).toLocaleString()}
          </span>
        </div>
      </div>

      {/* Lifecycle actions */}
      <section className="detail-section lifecycle-actions" data-testid="lifecycle-actions">
        {!isTerminal && (
          <button
            className="btn-terminate"
            onClick={() => setShowTerminateDialog(true)}
            disabled={terminating}
            data-testid="terminate-btn"
          >
            {terminating ? 'Terminating…' : 'Terminate'}
          </button>
        )}
        {!isArchived && (
          <button
            className="btn-archive"
            onClick={handleArchive}
            disabled={archiving}
            data-testid="archive-btn"
          >
            {archiving ? 'Archiving…' : 'Archive'}
          </button>
        )}
        {actionError && (
          <span className="lifecycle-error" data-testid="lifecycle-error">{actionError}</span>
        )}
      </section>

      <ConfirmDialog
        open={showTerminateDialog}
        title="Terminate Session"
        message="Are you sure you want to terminate this session? This action cannot be undone."
        onConfirm={handleTerminate}
        onCancel={() => setShowTerminateDialog(false)}
      />

      <section className="detail-section" data-testid="session-info-section">
        <h2>Session Info</h2>
        <dl className="detail-grid">
          <dt>Agent</dt>
          <dd data-testid="session-agent-id"><code>{session.agent_id}</code></dd>
          <dt>Environment</dt>
          <dd data-testid="session-environment-id"><code>{session.environment_id}</code></dd>
          {session.container_id && (
            <>
              <dt>Container</dt>
              <dd data-testid="session-container-id"><code>{session.container_id}</code></dd>
            </>
          )}
          {session.vault_ids && session.vault_ids.length > 0 && (
            <>
              <dt>Vaults</dt>
              <dd data-testid="session-vault-ids">
                {session.vault_ids.map(vid => (
                  <div key={vid}><code>{vid}</code></div>
                ))}
              </dd>
            </>
          )}
        </dl>
      </section>

      <section className="detail-section" data-testid="session-stats-section">
        <h2>Stats</h2>
        <div className="stats-grid">
          <div className="stat-card" data-testid="stat-total-events">
            <span className="stat-value">{session.stats.total_events}</span>
            <span className="stat-label">Total Events</span>
          </div>
          <div className="stat-card" data-testid="stat-tool-calls">
            <span className="stat-value">{session.stats.tool_calls}</span>
            <span className="stat-label">Tool Calls</span>
          </div>
          <div className="stat-card" data-testid="stat-model-turns">
            <span className="stat-value">{session.stats.model_turns}</span>
            <span className="stat-label">Model Turns</span>
          </div>
        </div>
      </section>

      <section className="detail-section" data-testid="session-usage-section">
        <h2>Token Usage</h2>
        <div className="stats-grid">
          <div className="stat-card" data-testid="usage-input-tokens">
            <span className="stat-value">{session.usage.input_tokens.toLocaleString()}</span>
            <span className="stat-label">Input Tokens</span>
          </div>
          <div className="stat-card" data-testid="usage-output-tokens">
            <span className="stat-value">{session.usage.output_tokens.toLocaleString()}</span>
            <span className="stat-label">Output Tokens</span>
          </div>
        </div>
      </section>

      <section className="detail-section" data-testid="event-stream-section">
        <h2>Events</h2>
        <EventStreamPanel sessionId={session.id} sessionStatus={session.status} />
      </section>

      <section className="detail-section" data-testid="message-input-section">
        <MessageInput sessionId={session.id} disabled={isTerminal} />
      </section>
    </div>
  );
}
