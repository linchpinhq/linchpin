import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { apiClient, ApiError } from '../api/client';
import type { Agent, ModelConfig, UpdateAgentRequest } from '../types';
import LoadingSpinner from '../components/LoadingSpinner';
import ErrorMessage from '../components/ErrorMessage';
import './AgentDetailView.css';

export default function AgentDetailView() {
  const { id } = useParams<{ id: string }>();
  const [agent, setAgent] = useState<Agent | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);

  // Edit mode state
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  // Editable field state
  const [editName, setEditName] = useState('');
  const [editProvider, setEditProvider] = useState<ModelConfig['provider']>('openrouter');
  const [editModelId, setEditModelId] = useState('');
  const [editBaseUrl, setEditBaseUrl] = useState('');
  const [editSystem, setEditSystem] = useState('');

  const fetchAgent = async () => {
    try {
      setError(null);
      const data = await apiClient.get<Agent>(`/v1/agents/${id}`);
      setAgent(data);
    } catch (err) {
      setError(err as ApiError);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    setLoading(true);
    fetchAgent();
  }, [id]);

  const enterEditMode = () => {
    if (!agent) return;
    setEditName(agent.name);
    setEditProvider(agent.model.provider);
    setEditModelId(agent.model.id);
    setEditBaseUrl(agent.model.base_url || '');
    setEditSystem(agent.system);
    setSaveError(null);
    setEditing(true);
  };

  const cancelEdit = () => {
    setEditing(false);
    setSaveError(null);
  };

  const handleSave = async () => {
    if (!agent) return;
    setSaving(true);
    setSaveError(null);

    // Build patch with only changed fields
    const patch: UpdateAgentRequest = {};

    if (editName !== agent.name) {
      patch.name = editName;
    }

    const modelChanged =
      editProvider !== agent.model.provider ||
      editModelId !== agent.model.id ||
      editBaseUrl !== (agent.model.base_url || '');

    if (modelChanged) {
      const model: ModelConfig = { provider: editProvider, id: editModelId };
      if (editProvider === 'ollama' && editBaseUrl.trim()) {
        model.base_url = editBaseUrl.trim();
      }
      patch.model = model;
    }

    if (editSystem !== agent.system) {
      patch.system = editSystem;
    }

    try {
      const updated = await apiClient.patch<Agent>(`/v1/agents/${id}`, patch);
      setAgent(updated);
      setEditing(false);
    } catch (err) {
      const apiErr = err as ApiError;
      setSaveError(apiErr.message || 'Failed to save changes');
    } finally {
      setSaving(false);
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
          fetchAgent();
        }}
      />
    );
  }

  if (!agent) return null;

  return (
    <div className="agent-detail-view" data-testid="agent-detail-view">
      <Link to="/agents" className="back-link" data-testid="back-link">
        ← Agents
      </Link>

      <div className="agent-detail-header">
        <div className="agent-detail-title-row">
          {editing ? (
            <input
              className="edit-name-input"
              type="text"
              value={editName}
              onChange={(e) => setEditName(e.target.value)}
              data-testid="edit-name-input"
            />
          ) : (
            <h1 data-testid="agent-name">{agent.name}</h1>
          )}
          {!editing && (
            <button className="btn-edit" onClick={enterEditMode} data-testid="edit-button">
              Edit
            </button>
          )}
        </div>
        <div className="agent-detail-meta">
          <span data-testid="agent-id">
            <code>{agent.id}</code>
          </span>
          <span className="meta-separator">·</span>
          <span data-testid="agent-version">Version {agent.version}</span>
          <span className="meta-separator">·</span>
          <span data-testid="agent-created">
            Created {new Date(agent.created_at).toLocaleString()}
          </span>
        </div>
      </div>

      {saveError && (
        <div className="save-error" data-testid="save-error">
          {saveError}
        </div>
      )}

      <section className="detail-section" data-testid="model-config-section">
        <h2>Model Configuration</h2>
        {editing ? (
          <div className="edit-fields">
            <div className="edit-field">
              <label htmlFor="edit-provider">Provider</label>
              <select
                id="edit-provider"
                value={editProvider}
                onChange={(e) => setEditProvider(e.target.value as ModelConfig['provider'])}
                data-testid="edit-provider-select"
              >
                <option value="openrouter">OpenRouter</option>
                <option value="ollama">Ollama</option>
              </select>
            </div>
            <div className="edit-field">
              <label htmlFor="edit-model-id">Model ID</label>
              <input
                id="edit-model-id"
                type="text"
                value={editModelId}
                onChange={(e) => setEditModelId(e.target.value)}
                data-testid="edit-model-id-input"
              />
            </div>
            {editProvider === 'ollama' && (
              <div className="edit-field">
                <label htmlFor="edit-base-url">Base URL</label>
                <input
                  id="edit-base-url"
                  type="text"
                  value={editBaseUrl}
                  onChange={(e) => setEditBaseUrl(e.target.value)}
                  placeholder="http://localhost:11434"
                  data-testid="edit-base-url-input"
                />
              </div>
            )}
          </div>
        ) : (
          <dl className="detail-grid">
            <dt>Provider</dt>
            <dd data-testid="model-provider">{agent.model.provider}</dd>
            <dt>Model ID</dt>
            <dd data-testid="model-id">{agent.model.id}</dd>
            {agent.model.base_url && (
              <>
                <dt>Base URL</dt>
                <dd data-testid="model-base-url">{agent.model.base_url}</dd>
              </>
            )}
          </dl>
        )}
      </section>

      <section className="detail-section" data-testid="system-prompt-section">
        <h2>System Prompt</h2>
        {editing ? (
          <textarea
            className="edit-system-textarea"
            value={editSystem}
            onChange={(e) => setEditSystem(e.target.value)}
            data-testid="edit-system-textarea"
          />
        ) : (
          <pre className="system-prompt" data-testid="system-prompt">
            {agent.system}
          </pre>
        )}
      </section>

      {editing && (
        <div className="edit-actions" data-testid="edit-actions">
          <button
            className="btn-save"
            onClick={handleSave}
            disabled={saving}
            data-testid="save-button"
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
          <button
            className="btn-cancel"
            onClick={cancelEdit}
            disabled={saving}
            data-testid="cancel-button"
          >
            Cancel
          </button>
        </div>
      )}

      <section className="detail-section" data-testid="tools-section">
        <h2>Tools</h2>
        {agent.tools.length === 0 ? (
          <p className="empty-text">No tools configured.</p>
        ) : (
          <ul className="tools-list">
            {agent.tools.map((tool, idx) =>
              tool.type === 'builtin' ? (
                tool.configs.map((cfg) => (
                  <li key={`builtin-${idx}-${cfg.name}`} className="tool-item" data-testid="tool-item">
                    <span className="tool-name">{cfg.name}</span>
                    <span className="tool-type">builtin</span>
                    <span
                      className={`tool-permission ${cfg.permission_policy}`}
                      data-testid="tool-permission"
                    >
                      {cfg.permission_policy}
                    </span>
                  </li>
                ))
              ) : (
                <li key={`custom-${idx}-${tool.name}`} className="tool-item" data-testid="tool-item">
                  <span className="tool-name">{tool.name}</span>
                  <span className="tool-type">custom</span>
                  <span
                    className={`tool-permission ${tool.permission_policy}`}
                    data-testid="tool-permission"
                  >
                    {tool.permission_policy}
                  </span>
                </li>
              ),
            )}
          </ul>
        )}
      </section>

      <section className="detail-section" data-testid="mcp-servers-section">
        <h2>MCP Servers</h2>
        {agent.mcp_servers.length === 0 ? (
          <p className="empty-text">No MCP servers configured.</p>
        ) : (
          <div className="mcp-servers-list">
            {agent.mcp_servers.map((server) => (
              <div key={server.name} className="mcp-server-card" data-testid="mcp-server-card">
                <h3 data-testid="mcp-server-name">{server.name}</h3>
                <dl className="detail-grid">
                  <dt>Command</dt>
                  <dd data-testid="mcp-server-command">
                    <code>{server.command}</code>
                  </dd>
                  {server.args.length > 0 && (
                    <>
                      <dt>Args</dt>
                      <dd data-testid="mcp-server-args">
                        <code>{server.args.join(' ')}</code>
                      </dd>
                    </>
                  )}
                  {Object.keys(server.env).length > 0 && (
                    <>
                      <dt>Env</dt>
                      <dd data-testid="mcp-server-env">
                        {Object.entries(server.env).map(([k, v]) => (
                          <div key={k} className="env-entry">
                            <code>
                              {k}={v}
                            </code>
                          </div>
                        ))}
                      </dd>
                    </>
                  )}
                </dl>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
