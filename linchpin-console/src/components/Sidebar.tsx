import { useEffect, useState, useCallback } from 'react';
import { NavLink, useNavigate } from 'react-router-dom';
import { clearApiKey } from '../api/client';
import './Sidebar.css';

type HealthStatus = 'checking' | 'healthy' | 'unhealthy';

const API_BASE =
  (typeof import.meta !== 'undefined' && import.meta.env?.VITE_API_URL) ||
  'http://localhost:8000';

export default function Sidebar() {
  const navigate = useNavigate();
  const [health, setHealth] = useState<HealthStatus>('checking');

  const checkHealth = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/health`);
      setHealth(res.ok ? 'healthy' : 'unhealthy');
    } catch {
      setHealth('unhealthy');
    }
  }, []);

  useEffect(() => {
    checkHealth();
    const id = setInterval(checkHealth, 30_000);
    return () => clearInterval(id);
  }, [checkHealth]);

  const handleClearKey = () => {
    clearApiKey();
    navigate('/', { replace: true });
    window.location.reload();
  };

  return (
    <aside className="sidebar" data-testid="sidebar">
      <div className="sidebar-header">
        <span className="sidebar-logo">Linchpin</span>
      </div>

      <nav className="sidebar-nav">
        <NavLink
          to="/agents"
          className={({ isActive }) =>
            `sidebar-nav-link${isActive ? ' active' : ''}`
          }
        >
          <svg className="sidebar-nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="3" />
            <path d="M12 1v4M12 19v4M4.22 4.22l2.83 2.83M16.95 16.95l2.83 2.83M1 12h4M19 12h4M4.22 19.78l2.83-2.83M16.95 7.05l2.83-2.83" />
          </svg>
          Agents
        </NavLink>

        <NavLink
          to="/sessions"
          className={({ isActive }) =>
            `sidebar-nav-link${isActive ? ' active' : ''}`
          }
        >
          <svg className="sidebar-nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
          </svg>
          Sessions
        </NavLink>

        <NavLink
          to="/environments"
          className={({ isActive }) =>
            `sidebar-nav-link${isActive ? ' active' : ''}`
          }
        >
          <svg className="sidebar-nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <rect x="2" y="2" width="20" height="8" rx="2" ry="2" />
            <rect x="2" y="14" width="20" height="8" rx="2" ry="2" />
            <line x1="6" y1="6" x2="6.01" y2="6" />
            <line x1="6" y1="18" x2="6.01" y2="18" />
          </svg>
          Environments
        </NavLink>

        <NavLink
          to="/vaults"
          className={({ isActive }) =>
            `sidebar-nav-link${isActive ? ' active' : ''}`
          }
        >
          <svg className="sidebar-nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
            <path d="M7 11V7a5 5 0 0 1 10 0v4" />
          </svg>
          Credential Vaults
        </NavLink>
      </nav>

      <div className="sidebar-footer">
        <div className="sidebar-health" data-testid="health-indicator">
          <span className={`sidebar-health-dot ${health}`} />
          <span>
            API {health === 'checking' ? 'checking…' : health === 'healthy' ? 'connected' : 'unreachable'}
          </span>
        </div>
        <button
          className="sidebar-clear-key"
          onClick={handleClearKey}
          data-testid="clear-api-key"
        >
          Clear API key
        </button>
      </div>
    </aside>
  );
}
