const API_KEY_STORAGE_KEY = 'linchpin_api_key';

const BASE_URL: string =
  import.meta.env.VITE_API_URL || 'http://localhost:8000';

// ---------------------------------------------------------------------------
// API key helpers
// ---------------------------------------------------------------------------

export function getApiKey(): string | null {
  return localStorage.getItem(API_KEY_STORAGE_KEY);
}

export function setApiKey(key: string): void {
  localStorage.setItem(API_KEY_STORAGE_KEY, key);
}

export function clearApiKey(): void {
  localStorage.removeItem(API_KEY_STORAGE_KEY);
}

// ---------------------------------------------------------------------------
// Structured API error
// ---------------------------------------------------------------------------

export interface ApiError {
  status: number;
  error: string;
  message: string;
  details?: unknown;
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function authHeaders(): Record<string, string> {
  const key = getApiKey();
  if (!key) return {};
  return { Authorization: `Bearer ${key}` };
}

function buildUrl(path: string, params?: Record<string, string>): string {
  const url = new URL(path, BASE_URL);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      url.searchParams.set(k, v);
    }
  }
  return url.toString();
}

async function handleResponse<T>(res: Response): Promise<T> {
  if (res.status === 401) {
    clearApiKey();
    window.location.href = '/';
    throw { status: 401, error: 'Unauthorized', message: 'Invalid or missing API key' } as ApiError;
  }

  if (!res.ok) {
    let body: Record<string, unknown> = {};
    try {
      body = await res.json();
    } catch {
      // response may not be JSON
    }
    throw {
      status: res.status,
      error: body.error ?? res.statusText,
      message: (body.message ?? body.detail ?? res.statusText) as string,
      details: body.details ?? body,
    } as ApiError;
  }

  // 204 No Content
  if (res.status === 204) return undefined as unknown as T;
  return res.json() as Promise<T>;
}

// ---------------------------------------------------------------------------
// ApiClient
// ---------------------------------------------------------------------------

export const apiClient = {
  async get<T>(path: string, params?: Record<string, string>): Promise<T> {
    const res = await fetch(buildUrl(path, params), {
      method: 'GET',
      headers: { ...authHeaders() },
    });
    return handleResponse<T>(res);
  },

  async post<T>(path: string, body: unknown): Promise<T> {
    const res = await fetch(buildUrl(path), {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...authHeaders(),
      },
      body: JSON.stringify(body),
    });
    return handleResponse<T>(res);
  },

  async patch<T>(path: string, body: unknown): Promise<T> {
    const res = await fetch(buildUrl(path), {
      method: 'PATCH',
      headers: {
        'Content-Type': 'application/json',
        ...authHeaders(),
      },
      body: JSON.stringify(body),
    });
    return handleResponse<T>(res);
  },

  async delete<T>(path: string): Promise<T> {
    const res = await fetch(buildUrl(path), {
      method: 'DELETE',
      headers: { ...authHeaders() },
    });
    return handleResponse<T>(res);
  },
};
