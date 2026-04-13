import type { SessionStatus } from '../types';
import './StatusBadge.css';

interface StatusBadgeProps {
  status: SessionStatus;
}

export default function StatusBadge({ status }: StatusBadgeProps) {
  return (
    <span
      className={`status-badge status-badge--${status}`}
      data-testid="status-badge"
    >
      <span className="status-badge-dot" />
      {status}
    </span>
  );
}
