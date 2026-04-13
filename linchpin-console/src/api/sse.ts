import type { SessionEvent } from '../types';
import { getApiKey } from './client';

const BASE_URL: string =
  import.meta.env.VITE_API_URL || 'http://localhost:8000';

/**
 * Lightweight SSE client using native EventSource.
 *
 * Native EventSource doesn't support custom headers, so we pass the
 * API key as a query parameter. The server accepts both Bearer token
 * header and `token` query parameter for SSE endpoints.
 */
export class SSEClient {
  private eventSource: EventSource | null = null;
  private lastCursor: string | undefined;

  connect(
    sessionId: string,
    cursor: string | undefined,
    onEvent: (event: SessionEvent) => void,
  ): void {
    this.disconnect();
    this.lastCursor = cursor;
    this.open(sessionId, onEvent);
  }

  disconnect(): void {
    if (this.eventSource) {
      this.eventSource.close();
      this.eventSource = null;
    }
  }

  private open(sessionId: string, onEvent: (event: SessionEvent) => void): void {
    const url = this.buildUrl(sessionId);
    const es = new EventSource(url);
    this.eventSource = es;

    es.onmessage = (msg) => {
      if (!msg.data) return;
      try {
        const parsed = JSON.parse(msg.data) as SessionEvent;
        if (parsed.cursor) {
          this.lastCursor = parsed.cursor;
        }
        onEvent(parsed);
      } catch {
        console.warn('[SSE] Failed to parse event:', msg.data);
      }
    };

    es.onerror = () => {
      // EventSource auto-reconnects. When it does, it'll replay from
      // the beginning (no cursor in URL on reconnect). We close and
      // reopen with the latest cursor to avoid duplicates.
      es.close();
      setTimeout(() => {
        if (this.eventSource === es) {
          this.open(sessionId, onEvent);
        }
      }, 1000);
    };
  }

  private buildUrl(sessionId: string): string {
    const key = getApiKey();
    let url = `${BASE_URL}/v1/sessions/${sessionId}/stream`;
    const params: string[] = [];
    if (this.lastCursor) {
      params.push(`cursor=${encodeURIComponent(this.lastCursor)}`);
    }
    if (key) {
      params.push(`token=${encodeURIComponent(key)}`);
    }
    if (params.length > 0) {
      url += '?' + params.join('&');
    }
    return url;
  }
}
