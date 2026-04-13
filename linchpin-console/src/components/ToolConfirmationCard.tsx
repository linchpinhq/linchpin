import { useState } from 'react';
import { apiClient, ApiError } from '../api/client';
import type { PostEventsRequest } from '../types';
import './ToolConfirmationCard.css';

interface ToolConfirmationCardProps {
  sessionId: string;
  toolName: string;
  arguments: Record<string, unknown>;
  onResolved: () => void;
}

export default function ToolConfirmationCard({
  sessionId,
  toolName,
  arguments: toolArgs,
  onResolved,
}: ToolConfirmationCardProps) {
  const [sending, setSending] = useState(false);
  const [resolved, setResolved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const sendConfirmation = async (approved: boolean) => {
    if (sending || resolved) return;
    setSending(true);
    setError(null);

    try {
      const body: PostEventsRequest = {
        events: [
          {
            type: 'user.tool_confirmation',
            payload: { approved },
          },
        ],
      };
      await apiClient.post(`/v1/sessions/${sessionId}/events`, body);
      setResolved(true);
      onResolved();
    } catch (err) {
      const apiErr = err as ApiError;
      setError(apiErr.message || 'Failed to send confirmation');
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="tool-confirmation-card" data-testid="tool-confirmation-card">
      <div className="tool-confirmation-header">
        Pending tool call: <span className="tool-confirmation-name" data-testid="tool-name">{toolName}</span>
      </div>
      <div className="tool-confirmation-args" data-testid="tool-args">
        {JSON.stringify(toolArgs, null, 2)}
      </div>
      {error && (
        <div className="tool-confirmation-error" data-testid="tool-confirmation-error">
          {error}
        </div>
      )}
      {!resolved && (
        <div className="tool-confirmation-actions">
          <button
            className="tool-confirmation-approve"
            onClick={() => sendConfirmation(true)}
            disabled={sending}
            data-testid="tool-approve-btn"
          >
            {sending ? 'Sending…' : 'Approve'}
          </button>
          <button
            className="tool-confirmation-reject"
            onClick={() => sendConfirmation(false)}
            disabled={sending}
            data-testid="tool-reject-btn"
          >
            {sending ? 'Sending…' : 'Reject'}
          </button>
        </div>
      )}
      {resolved && (
        <div className="tool-confirmation-resolved" data-testid="tool-resolved">
          Resolved
        </div>
      )}
    </div>
  );
}
