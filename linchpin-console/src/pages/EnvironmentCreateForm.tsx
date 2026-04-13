import { useState, FormEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { apiClient, ApiError } from '../api/client';
import type { CreateEnvironmentRequest, NetworkingConfig } from '../types';
import './EnvironmentCreateForm.css';

export default function EnvironmentCreateForm() {
  const navigate = useNavigate();

  const [name, setName] = useState('');
  const [networkingType, setNetworkingType] = useState<NetworkingConfig['type']>('none');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);

    const body: CreateEnvironmentRequest = {
      name,
      config: { networking: { type: networkingType } },
    };

    try {
      await apiClient.post('/v1/environments', body);
      navigate('/environments');
    } catch (err) {
      const apiErr = err as ApiError;
      setError(apiErr.message || 'Failed to create environment');
      setSubmitting(false);
    }
  };

  return (
    <div className="environment-create-form">
      <Link to="/environments" className="back-link">
        ← Environments
      </Link>
      <h1>Create Environment</h1>

      {error && (
        <div className="form-error" data-testid="form-error">
          {error}
        </div>
      )}

      <form onSubmit={handleSubmit}>
        <section className="form-section">
          <h2>Configuration</h2>
          <div className="form-field">
            <label htmlFor="env-name">Name</label>
            <input
              id="env-name"
              type="text"
              value={name}
              onChange={e => setName(e.target.value)}
              required
              placeholder="My Environment"
            />
          </div>
          <div className="form-field">
            <label htmlFor="networking-type">Networking Type</label>
            <select
              id="networking-type"
              value={networkingType}
              onChange={e => setNetworkingType(e.target.value as NetworkingConfig['type'])}
            >
              <option value="none">None</option>
              <option value="unrestricted">Unrestricted</option>
            </select>
          </div>
        </section>

        <div className="form-actions">
          <button type="submit" className="btn-submit" disabled={submitting}>
            {submitting ? 'Creating…' : 'Create Environment'}
          </button>
        </div>
      </form>
    </div>
  );
}
