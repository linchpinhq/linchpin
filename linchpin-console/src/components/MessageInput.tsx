import { useState } from 'react';
import { apiClient, ApiError } from '../api/client';
import type { PostEventsRequest } from '../types';
import './MessageInput.css';

interface MessageInputProps {
  sessionId: string;
  disabled: boolean;
}

export default function MessageInput({ sessionId, disabled }: MessageInputProps) {
  const [text, setText] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSend = async () => {
    const trimmed = text.trim();
    if (!trimmed || sending || disabled) return;

    setSending(true);
    setError(null);

    try {
      const body: PostEventsRequest = {
        events: [
          {
            type: 'user.message',
            payload: { content: trimmed },
          },
        ],
      };
      await apiClient.post(`/v1/sessions/${sessionId}/events`, body);
      setText('');
    } catch (err) {
      const apiErr = err as ApiError;
      setError(apiErr.message || 'Failed to send message');
    } finally {
      setSending(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  if (disabled) {
    return (
      <div className="message-input-container" data-testid="message-input-container">
        <div className="message-input-disabled-notice" data-testid="message-input-disabled-notice">
          This session is no longer active.
        </div>
        <div className="message-input-row">
          <textarea
            className="message-input-field"
            disabled
            placeholder="Session ended"
            data-testid="message-input-field"
          />
          <button className="message-input-send" disabled data-testid="message-input-send">
            Send
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="message-input-container" data-testid="message-input-container">
      {error && (
        <div className="message-input-error" data-testid="message-input-error">
          {error}
        </div>
      )}
      <div className="message-input-row">
        <textarea
          className="message-input-field"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Type a message…"
          disabled={sending}
          rows={1}
          data-testid="message-input-field"
        />
        <button
          className="message-input-send"
          onClick={handleSend}
          disabled={sending || !text.trim()}
          data-testid="message-input-send"
        >
          {sending ? 'Sending…' : 'Send'}
        </button>
      </div>
    </div>
  );
}
