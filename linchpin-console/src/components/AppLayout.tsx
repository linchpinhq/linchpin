import { useState } from 'react';
import { Outlet } from 'react-router-dom';
import Sidebar from './Sidebar';
import './AppLayout.css';

export default function AppLayout() {
  const [sidebarOpen, setSidebarOpen] = useState(false);

  return (
    <div className={`app-layout${sidebarOpen ? ' sidebar-open' : ''}`}>
      <button
        className="sidebar-toggle"
        onClick={() => setSidebarOpen((o) => !o)}
        aria-label={sidebarOpen ? 'Close navigation' : 'Open navigation'}
      >
        {sidebarOpen ? '✕' : '☰'}
      </button>

      <Sidebar />

      {/* Overlay to close sidebar on mobile tap */}
      <div
        className="sidebar-overlay"
        onClick={() => setSidebarOpen(false)}
      />

      <main className="app-main">
        <Outlet />
      </main>
    </div>
  );
}
