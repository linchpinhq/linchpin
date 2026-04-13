import { useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import { apiClient } from '../api/client';
import type { SessionEvent, SessionStatus, PaginatedEventsResponse } from '../types';
import ToolConfirmationCard from './ToolConfirmationCard';
import LoadingSpinner from './LoadingSpinner';
import './EventStreamPanel.css';

interface EventStreamPanelProps {
  sessionId: string;
  sessionStatus: SessionStatus;
}

/** Merge two event arrays, deduplicate by cursor, sort by seq ascending. */
export function mergeEvents(
  existing: SessionEvent[],
  incoming: SessionEvent[],
): SessionEvent[] {
  const map = new Map<string, SessionEvent>();
  for (const e of existing) map.set(e.cursor, e);
  for (const e of incoming) map.set(e.cursor, e);
  return Array.from(map.values()).sort((a, b) => a.seq - b.seq);
}

/** Return a CSS class suffix for the given event type. */
export function eventTypeClass(type: string): string {
  if (type === 'user.message') return 'user-message';
  if (type === 'agent.message') return 'agent-message';
  if (type === 'agent.message_delta') return 'agent-message-delta';
  if (type === 'agent.thinking') return 'agent-thinking';
  if (
    type === 'agent.tool_use' ||
    type === 'agent.tool_result' ||
    type === 'agent.mcp_tool_use' ||
    type === 'agent.mcp_tool_result' ||
    type === 'agent.custom_tool_use'
  ) return 'tool';
  if (type === 'session.error') return 'error';
  if (type === 'session.requires_action') return 'requires-action';
  if (type.startsWith('session.')) return 'status';
  return 'agent-message';
}

const TERMINAL_STATUSES: SessionStatus[] = ['terminated', 'failed'];
const POLL_INTERVAL_MS = 1000;

export default function EventStreamPanel({ sessionId, sessionStatus }: EventStreamPanelProps) {
  const [events, setEvents] = useState<SessionEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [streamingMessages, setStreamingMessages] = useState<Map<string, string>>(new Map());
  const listRef = useRef<HTMLDivElement>(null);
  const userScrolledUp = useRef(false);

  const scrollToBottom = () => {
    if (listRef.current && !userScrolledUp.current) {
      listRef.current.scrollTop = listRef.current.scrollHeight;
    }
  };

  const handleScroll = () => {
    if (!listRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = listRef.current;
    userScrolledUp.current = scrollHeight - scrollTop - clientHeight > 50;
  };

  // Initial fetch + continuous polling for non-terminal sessions
  useEffect(() => {
    let cancelled = false;

    async function fetchEvents() {
      try {
        const res = await apiClient.get<PaginatedEventsResponse>(
          `/v1/sessions/${sessionId}/events`,
        );
        if (cancelled) return;
        setEvents((prev) => {
          const merged = mergeEvents(prev, res.events);
          if (merged.length !== prev.length) return merged;
          if (merged.some((e, i) => e.cursor !== prev[i]?.cursor)) return merged;
          return prev;
        });
      } catch {
        // Silently handle
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    async function fetchStreaming() {
      try {
        const res = await apiClient.get<{ streaming: { message_id: string; text: string; done: boolean } | null }>(
          `/v1/sessions/${sessionId}/streaming`,
        );
        if (cancelled) return;
        if (res.streaming && res.streaming.text && !res.streaming.done) {
          setStreamingMessages(new Map([[res.streaming.message_id, res.streaming.text]]));
        } else {
          setStreamingMessages(new Map());
        }
      } catch {
        // Silently handle
      }
    }

    async function poll() {
      await fetchEvents();
      await fetchStreaming();
    }

    setLoading(true);
    setEvents([]);
    fetchEvents();

    // Start polling if session is not terminal
    let interval: ReturnType<typeof setInterval> | undefined;
    if (!TERMINAL_STATUSES.includes(sessionStatus)) {
      interval = setInterval(poll, POLL_INTERVAL_MS);
    }

    return () => {
      cancelled = true;
      if (interval) clearInterval(interval);
    };
  }, [sessionId, sessionStatus]);

  // Scroll to bottom when events or streaming messages change
  useEffect(() => {
    scrollToBottom();
  }, [events, streamingMessages]);

  const renderEvent = (event: SessionEvent) => {
    const cls = eventTypeClass(event.type);

    // Task 8.3: Skip rendering delta events as separate bubbles
    if (event.type === 'agent.message_delta') {
      return null;
    }

    if (event.type === 'session.requires_action') {
      const toolName = (event.payload.tool_name as string) ?? 'Unknown tool';
      const args = (event.payload.arguments as Record<string, unknown>) ?? {};
      return (
        <div key={event.cursor} className="event-bubble event-bubble--requires-action" data-testid="event-requires-action">
          <ToolConfirmationCard
            sessionId={sessionId}
            toolName={toolName}
            arguments={args}
            onResolved={() => {}}
          />
        </div>
      );
    }

    if (cls === 'tool') {
      const label = event.type.replace('agent.', '').replace(/_/g, ' ');
      return (
        <div key={event.cursor} className="event-bubble event-bubble--tool" data-testid={`event-${cls}`}>
          <div className="tool-label">{label}</div>
          <pre>{JSON.stringify(event.payload, null, 2)}</pre>
        </div>
      );
    }

    if (cls === 'error') {
      return (
        <div key={event.cursor} className="event-bubble event-bubble--error" data-testid="event-error">
          {(event.payload.message as string) ?? (event.payload.error as string) ?? 'An error occurred'}
        </div>
      );
    }

    if (cls === 'status') {
      return null;
    }

    // user.message, agent.message, agent.thinking
    const text = (event.payload.text as string) ?? (event.payload.content as string) ?? (event.payload.message as string) ?? '';

    // Render markdown for agent messages
    if (cls === 'agent-message' && text) {
      return (
        <div key={event.cursor} className={`event-bubble event-bubble--${cls}`} data-testid={`event-${cls}`}>
          <ReactMarkdown>{text}</ReactMarkdown>
        </div>
      );
    }

    return (
      <div key={event.cursor} className={`event-bubble event-bubble--${cls}`} data-testid={`event-${cls}`}>
        {text}
      </div>
    );
  };

  if (loading) return <LoadingSpinner inline />;

  return (
    <div className="event-stream-panel" data-testid="event-stream-panel">
      <div
        className="event-stream-list"
        ref={listRef}
        onScroll={handleScroll}
        data-testid="event-stream-list"
      >
        {events.length === 0 && streamingMessages.size === 0 ? (
          <div className="event-stream-empty">No events yet.</div>
        ) : (
          <>
            {events.map(renderEvent)}
            {/* Render active streaming bubbles */}
            {Array.from(streamingMessages.entries()).map(([messageId, text]) => (
              <div
                key={`streaming-${messageId}`}
                className="event-bubble event-bubble--agent-message event-bubble--streaming"
                data-testid="event-streaming"
              >
                <ReactMarkdown>{text}</ReactMarkdown>
                <span className="streaming-cursor" />
              </div>
            ))}
          </>
        )}
      </div>
    </div>
  );
}
