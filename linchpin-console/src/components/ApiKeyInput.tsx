import { useState, type FormEvent } from 'react';
import { setApiKey } from '../api/client';
import './ApiKeyInput.css';

interface ApiKeyInputProps {
  onKeySet: () => void;
}

export default function ApiKeyInput({ onKeySet }: ApiKeyInputProps) {
  const [key, setKey] = useState('');

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    const trimmed = key.trim();
    if (!trimmed) return;
    setApiKey(trimmed);
    onKeySet();
  };

  return (
    <div className="api-key-screen">
      <form className="api-key-card" onSubmit={handleSubmit}>
        <h1>Linchpin Console</h1>
        <p>Enter your Linchpin API key to get started.</p>
        <label htmlFor="api-key-input">API Key</label>
        <input
          id="api-key-input"
          type="password"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          placeholder="lp_key_..."
          autoFocus
        />
        <button type="submit" disabled={!key.trim()}>
          Connect
        </button>
      </form>
    </div>
  );
}
