import { useEffect, useState } from 'react';
import { apiClient, ApiError } from '../api/client';
import type { Agent, Environment, Vault, Session, PaginatedResponse } from '../types';
import './SessionCreateDialog.css';

interface SessionCreateDialogProps {
  open: boolean;
  onClose: () => void;
  onCreated: (id: string) => void;
}

export default function SessionCreateDialog({ open, onClose, onCreated }: SessionCreateDialogProps) {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [environments, setEnvironments] = useState<Environment[]>([]);
  const [vaults, setVaults] = useState<Vault[]>([]);
  const [agentId, setAgentId] = useState('');
  const [environmentId, setEnvironmentId] = useState('');
  const [title, setTitle] = useState('');
  const [ttl, setTtl] = useState('');
  const [selectedVaultIds, setSelectedVaultIds] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    // Reset form state when dialog opens
    setAgentId('');
    setEnvironmentId('');
    setTitle('');
    setTtl('');
    setSelectedVaultIds([]);
    setError(null);

    const load = async () => {
      try {
        const [agentRes, envRes, vaultRes] = await Promise.all([
          apiClient.get<PaginatedResponse<Agent>>('/v1/agents'),
          apiClient.get<PaginatedResponse<Environment>>('/v1/environments'),
          apiClient.get<PaginatedResponse<Vault>>('/v1/vaults'),
        ]);
        setAgents(agentRes.data);
        setEnvironments(envRes.data);
        setVaults(vaultRes.data.filter(v => !v.archived_at));
      } catch (err) {
        const apiErr = err as ApiError;
        setError(apiErr.message || 'Failed to load agents or environments');
      }
    };
    load();
  }, [open]);

  if (!open) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!agentId || !environmentId) return;

    setSubmitting(true);
    setError(null);

    try {
      const body: Record<string, unknown> = {
        agent_id: agentId,
        environment_id: environmentId,
      };
      if (title.trim()) body.title = title.trim();
      if (ttl.trim()) body.ttl_seconds = Number(ttl);
      if (selectedVaultIds.length > 0) body.vault_ids = selectedVaultIds;

      const session = await apiClient.post<Session>('/v1/sessions', body);
      onCreated(session.id);
    } catch (err) {
      const apiErr = err as ApiError;
      setError(apiErr.message || 'Failed to create session');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="dialog-overlay" data-testid="session-create-overlay" onClick={onClose}>
      <div
        className="dialog-panel"
        data-testid="session-create-dialog"
        role="dialog"
        aria-labelledby="session-create-title"
        onClick={e => e.stopPropagation()}
      >
        <h2 id="session-create-title" className="dialog-title">New Session</h2>

        <form onSubmit={handleSubmit} className="dialog-form">
          <div className="form-field">
            <label htmlFor="session-agent">Agent</label>
            <select
              id="session-agent"
              value={agentId}
              onChange={e => setAgentId(e.target.value)}
              required
              data-testid="session-agent-select"
            >
              <option value="">Select an agent</option>
              {agents.map(a => (
                <option key={a.id} value={a.id}>
                  {a.name} ({a.id.slice(0, 8)})
                </option>
              ))}
            </select>
          </div>

          <div className="form-field">
            <label htmlFor="session-environment">Environment</label>
            <select
              id="session-environment"
              value={environmentId}
              onChange={e => setEnvironmentId(e.target.value)}
              required
              data-testid="session-environment-select"
            >
              <option value="">Select an environment</option>
              {environments.map(env => (
                <option key={env.id} value={env.id}>
                  {env.name} ({env.id.slice(0, 8)})
                </option>
              ))}
            </select>
          </div>

          <div className="form-field">
            <label htmlFor="session-title">Title (optional)</label>
            <input
              id="session-title"
              type="text"
              value={title}
              onChange={e => setTitle(e.target.value)}
              placeholder="Session title"
              data-testid="session-title-input"
            />
          </div>

          <div className="form-field">
            <label htmlFor="session-ttl">TTL in seconds (optional)</label>
            <input
              id="session-ttl"
              type="number"
              min="0"
              value={ttl}
              onChange={e => setTtl(e.target.value)}
              placeholder="e.g. 3600"
              data-testid="session-ttl-input"
            />
          </div>

          <div className="form-field">
            <label>Credential Vaults (optional)</label>
            <div className="vault-checkbox-list" data-testid="session-vault-select">
              {vaults.length === 0 ? (
                <span className="vault-checkbox-empty">No vaults available</span>
              ) : (
                vaults.map(v => (
                  <label key={v.id} className="vault-checkbox-item">
                    <input
                      type="checkbox"
                      checked={selectedVaultIds.includes(v.id)}
                      onChange={e => {
                        if (e.target.checked) {
                          setSelectedVaultIds(prev => [...prev, v.id]);
                        } else {
                          setSelectedVaultIds(prev => prev.filter(id => id !== v.id));
                        }
                      }}
                      data-testid={`vault-checkbox-${v.id}`}
                    />
                    <span>{v.display_name}</span>
                    <code className="vault-checkbox-id">{v.id.slice(0, 8)}</code>
                  </label>
                ))
              )}
            </div>
          </div>

          {error && (
            <div className="dialog-error" data-testid="session-create-error" role="alert">
              {error}
            </div>
          )}

          <div className="dialog-actions">
            <button
              type="button"
              className="dialog-cancel-btn"
              onClick={onClose}
              data-testid="session-create-cancel"
            >
              Cancel
            </button>
            <button
              type="submit"
              className="btn-primary"
              disabled={submitting || !agentId || !environmentId}
              data-testid="session-create-submit"
            >
              {submitting ? 'Creating…' : 'Create session'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
