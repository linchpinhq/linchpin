import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { apiClient, ApiError } from '../api/client';
import type { Environment } from '../types';
import LoadingSpinner from '../components/LoadingSpinner';
import ErrorMessage from '../components/ErrorMessage';
import './EnvironmentDetailView.css';

export default function EnvironmentDetailView() {
  const { id } = useParams<{ id: string }>();
  const [environment, setEnvironment] = useState<Environment | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);

  const fetchEnvironment = async () => {
    try {
      setError(null);
      const data = await apiClient.get<Environment>(`/v1/environments/${id}`);
      setEnvironment(data);
    } catch (err) {
      setError(err as ApiError);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    setLoading(true);
    fetchEnvironment();
  }, [id]);

  if (loading) return <LoadingSpinner />;

  if (error) {
    return (
      <ErrorMessage
        status={error.status}
        message={error.message}
        onRetry={() => {
          setLoading(true);
          fetchEnvironment();
        }}
      />
    );
  }

  if (!environment) return null;

  return (
    <div className="environment-detail-view" data-testid="environment-detail-view">
      <Link to="/environments" className="back-link" data-testid="back-link">
        ← Environments
      </Link>

      <div className="environment-detail-header">
        <h1 data-testid="environment-name">{environment.name}</h1>
        <div className="environment-detail-meta">
          <span data-testid="environment-id">
            <code>{environment.id}</code>
          </span>
          <span className="meta-separator">·</span>
          <span data-testid="environment-created">
            Created {new Date(environment.created_at).toLocaleString()}
          </span>
        </div>
      </div>

      <section className="detail-section" data-testid="networking-section">
        <h2>Configuration</h2>
        <dl className="detail-grid">
          <dt>Networking Type</dt>
          <dd data-testid="networking-type">{environment.config.networking.type}</dd>
        </dl>
      </section>
    </div>
  );
}
