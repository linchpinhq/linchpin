import { useState, FormEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { apiClient, ApiError } from '../api/client';
import type { Agent, CreateAgentRequest, ModelConfig, MCPServerConfig } from '../types';
import './AgentCreateForm.css';

interface ToolEntry {
  type: 'builtin' | 'custom';
  name: string;
  permission_policy: 'always_allow' | 'always_ask';
}

interface MCPEntry {
  name: string;
  command: string;
  args: string;
  env: Array<{ key: string; value: string }>;
}

export default function AgentCreateForm() {
  const navigate = useNavigate();

  // Basic info
  const [name, setName] = useState('');
  const [provider, setProvider] = useState<ModelConfig['provider']>('openrouter');
  const [modelId, setModelId] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [systemPrompt, setSystemPrompt] = useState('');

  // Dynamic tools
  const [tools, setTools] = useState<ToolEntry[]>([]);

  // Dynamic MCP servers
  const [mcpServers, setMcpServers] = useState<MCPEntry[]>([]);

  // Form state
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // --- Tool helpers ---
  const addTool = () => {
    setTools([...tools, { type: 'builtin', name: '', permission_policy: 'always_allow' }]);
  };

  const removeTool = (index: number) => {
    setTools(tools.filter((_, i) => i !== index));
  };

  const updateTool = (index: number, field: keyof ToolEntry, value: string) => {
    setTools(tools.map((t, i) => (i === index ? { ...t, [field]: value } : t)));
  };

  // --- MCP server helpers ---
  const addMcpServer = () => {
    setMcpServers([...mcpServers, { name: '', command: '', args: '', env: [] }]);
  };

  const removeMcpServer = (index: number) => {
    setMcpServers(mcpServers.filter((_, i) => i !== index));
  };

  const updateMcpServer = (index: number, field: keyof MCPEntry, value: string) => {
    setMcpServers(mcpServers.map((s, i) => (i === index ? { ...s, [field]: value } : s)));
  };

  const addEnvPair = (serverIndex: number) => {
    setMcpServers(
      mcpServers.map((s, i) =>
        i === serverIndex ? { ...s, env: [...s.env, { key: '', value: '' }] } : s,
      ),
    );
  };

  const removeEnvPair = (serverIndex: number, envIndex: number) => {
    setMcpServers(
      mcpServers.map((s, i) =>
        i === serverIndex ? { ...s, env: s.env.filter((_, ei) => ei !== envIndex) } : s,
      ),
    );
  };

  const updateEnvPair = (
    serverIndex: number,
    envIndex: number,
    field: 'key' | 'value',
    value: string,
  ) => {
    setMcpServers(
      mcpServers.map((s, i) =>
        i === serverIndex
          ? {
              ...s,
              env: s.env.map((e, ei) => (ei === envIndex ? { ...e, [field]: value } : e)),
            }
          : s,
      ),
    );
  };

  // --- Build request body ---
  const buildRequest = (): CreateAgentRequest => {
    const model: ModelConfig = { provider, id: modelId };
    if (provider === 'ollama' && baseUrl.trim()) {
      model.base_url = baseUrl.trim();
    }

    const toolConfigs = tools.map((t) => {
      if (t.type === 'builtin') {
        return {
          type: 'builtin' as const,
          default_config: {},
          configs: [{ name: t.name, permission_policy: t.permission_policy, enabled: true }],
        };
      }
      return {
        type: 'custom' as const,
        name: t.name,
        description: '',
        input_schema: {},
        permission_policy: t.permission_policy,
      };
    });

    const mcpConfigs: MCPServerConfig[] = mcpServers.map((s) => ({
      name: s.name,
      command: s.command,
      args: s.args
        .split(',')
        .map((a) => a.trim())
        .filter(Boolean),
      env: Object.fromEntries(s.env.filter((e) => e.key.trim()).map((e) => [e.key, e.value])),
    }));

    return {
      name,
      model,
      system: systemPrompt,
      tools: toolConfigs,
      mcp_servers: mcpConfigs,
    };
  };

  // --- Submit ---
  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);

    try {
      const agent = await apiClient.post<Agent>('/v1/agents', buildRequest());
      navigate(`/agents/${agent.id}`);
    } catch (err) {
      const apiErr = err as ApiError;
      setError(apiErr.message || 'Failed to create agent');
      setSubmitting(false);
    }
  };

  return (
    <div className="agent-create-form">
      <Link to="/agents" className="back-link">
        ← Agents
      </Link>
      <h1>Create Agent</h1>

      {error && (
        <div className="form-error" data-testid="form-error">
          {error}
        </div>
      )}

      <form onSubmit={handleSubmit}>
        {/* Basic Info */}
        <section className="form-section">
          <h2>Basic Info</h2>
          <div className="form-field">
            <label htmlFor="agent-name">Name</label>
            <input
              id="agent-name"
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
              placeholder="My Agent"
            />
          </div>
        </section>

        {/* Model Configuration */}
        <section className="form-section">
          <h2>Model Configuration</h2>
          <div className="form-field">
            <label htmlFor="model-provider">Provider</label>
            <select
              id="model-provider"
              value={provider}
              onChange={(e) => setProvider(e.target.value as ModelConfig['provider'])}
            >
              <option value="openrouter">OpenRouter</option>
              <option value="ollama">Ollama</option>
            </select>
          </div>
          <div className="form-field">
            <label htmlFor="model-id">Model ID</label>
            <input
              id="model-id"
              type="text"
              value={modelId}
              onChange={(e) => setModelId(e.target.value)}
              required
              placeholder="claude-sonnet-4-20250514"
            />
          </div>
          {provider === 'ollama' && (
            <div className="form-field">
              <label htmlFor="base-url">Base URL</label>
              <input
                id="base-url"
                type="text"
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder="http://localhost:11434"
              />
            </div>
          )}
        </section>

        {/* System Prompt */}
        <section className="form-section">
          <h2>System Prompt</h2>
          <div className="form-field">
            <label htmlFor="system-prompt">Prompt</label>
            <textarea
              id="system-prompt"
              value={systemPrompt}
              onChange={(e) => setSystemPrompt(e.target.value)}
              placeholder="You are a helpful assistant..."
            />
          </div>
        </section>

        {/* Tools */}
        <section className="form-section">
          <h2>Tools</h2>
          <div className="dynamic-entries">
            {tools.map((tool, idx) => (
              <div key={idx} className="dynamic-entry">
                <div className="entry-fields">
                  <div className="form-field">
                    <label>Type</label>
                    <select
                      value={tool.type}
                      onChange={(e) => updateTool(idx, 'type', e.target.value)}
                    >
                      <option value="builtin">Builtin</option>
                      <option value="custom">Custom</option>
                    </select>
                  </div>
                  <div className="form-field">
                    <label>Name</label>
                    <input
                      type="text"
                      value={tool.name}
                      onChange={(e) => updateTool(idx, 'name', e.target.value)}
                      placeholder="Tool name"
                    />
                  </div>
                  <div className="form-field">
                    <label>Permission</label>
                    <select
                      value={tool.permission_policy}
                      onChange={(e) => updateTool(idx, 'permission_policy', e.target.value)}
                    >
                      <option value="always_allow">Always Allow</option>
                      <option value="always_ask">Always Ask</option>
                    </select>
                  </div>
                </div>
                <button type="button" className="btn-remove" onClick={() => removeTool(idx)}>
                  Remove
                </button>
              </div>
            ))}
          </div>
          <button type="button" className="btn-add" onClick={addTool}>
            + Add Tool
          </button>
        </section>

        {/* MCP Servers */}
        <section className="form-section">
          <h2>MCP Servers</h2>
          <div className="dynamic-entries">
            {mcpServers.map((server, idx) => (
              <div key={idx} className="dynamic-entry">
                <div className="entry-fields">
                  <div className="form-field">
                    <label>Name</label>
                    <input
                      type="text"
                      value={server.name}
                      onChange={(e) => updateMcpServer(idx, 'name', e.target.value)}
                      placeholder="Server name"
                    />
                  </div>
                  <div className="form-field">
                    <label>Command</label>
                    <input
                      type="text"
                      value={server.command}
                      onChange={(e) => updateMcpServer(idx, 'command', e.target.value)}
                      placeholder="npx"
                    />
                  </div>
                  <div className="form-field full-width">
                    <label>Args (comma-separated)</label>
                    <input
                      type="text"
                      value={server.args}
                      onChange={(e) => updateMcpServer(idx, 'args', e.target.value)}
                      placeholder="-y, @modelcontextprotocol/server-filesystem"
                    />
                  </div>
                  <div className="form-field full-width">
                    <label>Environment Variables</label>
                    <div className="env-pairs">
                      {server.env.map((envPair, ei) => (
                        <div key={ei} className="env-pair">
                          <input
                            type="text"
                            value={envPair.key}
                            onChange={(e) => updateEnvPair(idx, ei, 'key', e.target.value)}
                            placeholder="KEY"
                          />
                          <input
                            type="text"
                            value={envPair.value}
                            onChange={(e) => updateEnvPair(idx, ei, 'value', e.target.value)}
                            placeholder="value"
                          />
                          <button
                            type="button"
                            className="btn-remove-sm"
                            onClick={() => removeEnvPair(idx, ei)}
                          >
                            ✕
                          </button>
                        </div>
                      ))}
                      <button type="button" className="btn-add-sm" onClick={() => addEnvPair(idx)}>
                        + Add env var
                      </button>
                    </div>
                  </div>
                </div>
                <button type="button" className="btn-remove" onClick={() => removeMcpServer(idx)}>
                  Remove
                </button>
              </div>
            ))}
          </div>
          <button type="button" className="btn-add" onClick={addMcpServer}>
            + Add MCP Server
          </button>
        </section>

        {/* Submit */}
        <div className="form-actions">
          <button type="submit" className="btn-submit" disabled={submitting}>
            {submitting ? 'Creating…' : 'Create Agent'}
          </button>
        </div>
      </form>
    </div>
  );
}
