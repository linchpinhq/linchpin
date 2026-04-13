import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { apiClient, ApiError } from '../api/client';
import type { Vault, CredentialResponse, CredentialType, ProviderName, PaginatedResponse } from '../types';
import LoadingSpinner from '../components/LoadingSpinner';
import ErrorMessage from '../components/ErrorMessage';
import ConfirmDialog from '../components/ConfirmDialog';
import './VaultDetailView.css';

export default function VaultDetailView() {
  const { id } = useParams<{ id: string }>();
  const [vault, setVault] = useState<Vault | null>(null);
  const [credentials, setCredentials] = useState<CredentialResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);

  // Add credential form
  const [showAddForm, setShowAddForm] = useState(false);
  const [credType, setCredType] = useState<CredentialType>('bearer_token');
  const [mcpUrl, setMcpUrl] = useState('');
  const [provider, setProvider] = useState<ProviderName>('anthropic');
  const [tokenVal, setTokenVal] = useState('');
  const [apiKeyVal, setApiKeyVal] = useState('');
  const [accessTokenVal, setAccessTokenVal] = useState('');
  const [refreshTokenVal, setRefreshTokenVal] = useState('');
  const [clientIdVal, setClientIdVal] = useState('');
  const [clientSecretVal, setClientSecretVal] = useState('');
  const [addingCred, setAddingCred] = useState(false);
  const [addCredError, setAddCredError] = useState<string | null>(null);

  // Confirm dialogs
  const [deleteCredTarget, setDeleteCredTarget] = useState<CredentialResponse | null>(null);
  const [archiveCredTarget, setArchiveCredTarget] = useState<CredentialResponse | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const fetchData = async () => {
    try {
      setError(null);
      const [vaultData, credData] = await Promise.all([
        apiClient.get<Vault>(`/v1/vaults/${id}`),
        apiClient.get<PaginatedResponse<CredentialResponse>>(`/v1/vaults/${id}/credentials`),
      ]);
      setVault(vaultData);
      setCredentials(credData.data);
    } catch (err) {
      setError(err as ApiError);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    setLoading(true);
    fetchData();
  }, [id]);

  const resetAddForm = () => {
    setCredType('bearer_token');
    setMcpUrl('');
    setProvider('anthropic');
    setTokenVal('');
    setApiKeyVal('');
    setAccessTokenVal('');
    setRefreshTokenVal('');
    setClientIdVal('');
    setClientSecretVal('');
    setAddCredError(null);
  };

  const handleAddCredential = async (e: React.FormEvent) => {
    e.preventDefault();
    setAddingCred(true);
    setAddCredError(null);

    try {
      let body: Record<string, unknown>;
      if (credType === 'bearer_token') {
        body = { credential_type: 'bearer_token', mcp_server_url: mcpUrl, token: tokenVal };
      } else if (credType === 'api_key') {
        body = { credential_type: 'api_key', provider, api_key: apiKeyVal };
      } else {
        body = {
          credential_type: 'oauth',
          mcp_server_url: mcpUrl,
          access_token: accessTokenVal,
          ...(refreshTokenVal && { refresh_token: refreshTokenVal }),
          ...(clientIdVal && { client_id: clientIdVal }),
          ...(clientSecretVal && { client_secret: clientSecretVal }),
        };
      }

      const created = await apiClient.post<CredentialResponse>(
        `/v1/vaults/${id}/credentials`,
        body,
      );
      setCredentials(prev => [...prev, created]);
      resetAddForm();
      setShowAddForm(false);
    } catch (err) {
      const apiErr = err as ApiError;
      setAddCredError(apiErr.message || 'Failed to add credential');
    } finally {
      setAddingCred(false);
    }
  };

  const handleDeleteCred = async () => {
    if (!deleteCredTarget) return;
    setActionError(null);
    try {
      await apiClient.delete(`/v1/vaults/${id}/credentials/${deleteCredTarget.id}`);
      setCredentials(prev => prev.filter(c => c.id !== deleteCredTarget.id));
    } catch (err) {
      const apiErr = err as ApiError;
      setActionError(apiErr.message || 'Failed to delete credential');
    } finally {
      setDeleteCredTarget(null);
    }
  };

  const handleArchiveCred = async () => {
    if (!archiveCredTarget) return;
    setActionError(null);
    try {
      await apiClient.post(`/v1/vaults/${id}/credentials/${archiveCredTarget.id}/archive`, {});
      setCredentials(prev =>
        prev.map(c =>
          c.id === archiveCredTarget.id ? { ...c, archived_at: new Date().toISOString() } : c,
        ),
      );
    } catch (err) {
      const apiErr = err as ApiError;
      setActionError(apiErr.message || 'Failed to archive credential');
    } finally {
      setArchiveCredTarget(null);
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
          fetchData();
        }}
      />
    );
  }

  if (!vault) return null;

  const isArchived = !!vault.archived_at;

  return (
    <div className="vault-detail-view" data-testid="vault-detail-view">
      <Link to="/vaults" className="back-link" data-testid="back-link">
        ← Vaults
      </Link>

      <div className="vault-detail-header">
        <div className="vault-detail-title-row">
          <h1 data-testid="vault-name">{vault.display_name}</h1>
          {isArchived && (
            <span className="archived-badge" data-testid="archived-badge">Archived</span>
          )}
        </div>
        <div className="vault-detail-meta">
          <span data-testid="vault-id"><code>{vault.id}</code></span>
          <span className="meta-separator">·</span>
          <span data-testid="vault-created">
            Created {new Date(vault.created_at).toLocaleString()}
          </span>
          {vault.archived_at && (
            <>
              <span className="meta-separator">·</span>
              <span data-testid="vault-archived-at">
                Archived {new Date(vault.archived_at).toLocaleString()}
              </span>
            </>
          )}
        </div>
      </div>

      {actionError && (
        <div className="form-error" role="alert" data-testid="action-error">{actionError}</div>
      )}

      <section className="detail-section" data-testid="credentials-section">
        <h2>
          Credentials ({credentials.length})
          {!isArchived && (
            <button
              className="btn-primary"
              style={{ marginLeft: 'var(--spacing-md)', fontSize: '0.75rem', padding: '2px 10px' }}
              onClick={() => { setShowAddForm(s => !s); resetAddForm(); }}
              data-testid="add-credential-btn"
            >
              {showAddForm ? 'Cancel' : 'Add credential'}
            </button>
          )}
        </h2>

        {showAddForm && (
          <form className="add-credential-form" onSubmit={handleAddCredential} data-testid="add-credential-form">
            <div className="form-row">
              <div className="form-field">
                <label htmlFor="cred-type">Type</label>
                <select
                  id="cred-type"
                  value={credType}
                  onChange={e => setCredType(e.target.value as CredentialType)}
                  data-testid="cred-type-select"
                >
                  <option value="bearer_token">Bearer Token</option>
                  <option value="api_key">API Key</option>
                  <option value="oauth">OAuth</option>
                </select>
              </div>
            </div>

            {credType === 'bearer_token' && (
              <div className="form-row">
                <div className="form-field">
                  <label htmlFor="cred-mcp-url">MCP Server URL</label>
                  <input
                    id="cred-mcp-url"
                    type="text"
                    value={mcpUrl}
                    onChange={e => setMcpUrl(e.target.value)}
                    placeholder="https://mcp.example.com"
                    required
                    data-testid="cred-mcp-url-input"
                  />
                </div>
                <div className="form-field">
                  <label htmlFor="cred-token">Token</label>
                  <input
                    id="cred-token"
                    type="password"
                    value={tokenVal}
                    onChange={e => setTokenVal(e.target.value)}
                    placeholder="Bearer token"
                    required
                    data-testid="cred-token-input"
                  />
                </div>
              </div>
            )}

            {credType === 'api_key' && (
              <div className="form-row">
                <div className="form-field">
                  <label htmlFor="cred-provider">Provider</label>
                  <select
                    id="cred-provider"
                    value={provider}
                    onChange={e => setProvider(e.target.value as ProviderName)}
                    data-testid="cred-provider-select"
                  >
                    <option value="anthropic">Anthropic</option>
                    <option value="openai">OpenAI</option>
                    <option value="ollama">Ollama</option>
                  </select>
                </div>
                <div className="form-field">
                  <label htmlFor="cred-api-key">API Key</label>
                  <input
                    id="cred-api-key"
                    type="password"
                    value={apiKeyVal}
                    onChange={e => setApiKeyVal(e.target.value)}
                    placeholder="API key"
                    required
                    data-testid="cred-api-key-input"
                  />
                </div>
              </div>
            )}

            {credType === 'oauth' && (
              <>
                <div className="form-row">
                  <div className="form-field">
                    <label htmlFor="cred-oauth-url">MCP Server URL</label>
                    <input
                      id="cred-oauth-url"
                      type="text"
                      value={mcpUrl}
                      onChange={e => setMcpUrl(e.target.value)}
                      placeholder="https://mcp.example.com"
                      required
                      data-testid="cred-oauth-url-input"
                    />
                  </div>
                  <div className="form-field">
                    <label htmlFor="cred-access-token">Access Token</label>
                    <input
                      id="cred-access-token"
                      type="password"
                      value={accessTokenVal}
                      onChange={e => setAccessTokenVal(e.target.value)}
                      placeholder="Access token"
                      required
                      data-testid="cred-access-token-input"
                    />
                  </div>
                </div>
                <div className="form-row">
                  <div className="form-field">
                    <label htmlFor="cred-refresh-token">Refresh Token (optional)</label>
                    <input
                      id="cred-refresh-token"
                      type="password"
                      value={refreshTokenVal}
                      onChange={e => setRefreshTokenVal(e.target.value)}
                      placeholder="Refresh token"
                      data-testid="cred-refresh-token-input"
                    />
                  </div>
                  <div className="form-field">
                    <label htmlFor="cred-client-id">Client ID (optional)</label>
                    <input
                      id="cred-client-id"
                      type="text"
                      value={clientIdVal}
                      onChange={e => setClientIdVal(e.target.value)}
                      placeholder="Client ID"
                      data-testid="cred-client-id-input"
                    />
                  </div>
                  <div className="form-field">
                    <label htmlFor="cred-client-secret">Client Secret (optional)</label>
                    <input
                      id="cred-client-secret"
                      type="password"
                      value={clientSecretVal}
                      onChange={e => setClientSecretVal(e.target.value)}
                      placeholder="Client secret"
                      data-testid="cred-client-secret-input"
                    />
                  </div>
                </div>
              </>
            )}

            {addCredError && <div className="form-error" role="alert">{addCredError}</div>}

            <div className="add-credential-form-actions">
              <button
                type="submit"
                className="btn-primary"
                disabled={addingCred}
                data-testid="add-credential-submit"
              >
                {addingCred ? 'Adding…' : 'Add credential'}
              </button>
            </div>
          </form>
        )}

        {credentials.length === 0 ? (
          <p className="empty-text">No credentials in this vault.</p>
        ) : (
          <div className="credential-list">
            {credentials.map(cred => (
              <div key={cred.id} className="credential-card" data-testid={`credential-card-${cred.id}`}>
                <div className="credential-card-info">
                  <div className="credential-card-type">{cred.credential_type}</div>
                  <div className="credential-card-target">
                    {cred.mcp_server_url || cred.provider || '—'}
                  </div>
                  <div className="credential-card-meta">
                    Created {new Date(cred.created_at).toLocaleString()}
                    {cred.archived_at && ' · Archived'}
                  </div>
                </div>
                <div className="credential-card-secret" data-testid="credential-secret-mask">
                  ••••••••
                </div>
                <div className="credential-card-actions">
                  {!cred.archived_at && (
                    <button
                      className="btn-sm-secondary"
                      onClick={() => setArchiveCredTarget(cred)}
                      data-testid="cred-archive-btn"
                    >
                      Archive
                    </button>
                  )}
                  <button
                    className="btn-sm-danger"
                    onClick={() => setDeleteCredTarget(cred)}
                    data-testid="cred-delete-btn"
                  >
                    Delete
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      <ConfirmDialog
        open={!!deleteCredTarget}
        title="Delete Credential"
        message="Are you sure you want to permanently delete this credential?"
        onConfirm={handleDeleteCred}
        onCancel={() => setDeleteCredTarget(null)}
      />

      <ConfirmDialog
        open={!!archiveCredTarget}
        title="Archive Credential"
        message="Are you sure you want to archive this credential? Encrypted secrets will be purged."
        onConfirm={handleArchiveCred}
        onCancel={() => setArchiveCredTarget(null)}
      />
    </div>
  );
}
