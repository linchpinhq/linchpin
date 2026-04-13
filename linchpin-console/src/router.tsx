import { createBrowserRouter, Navigate } from 'react-router-dom';
import AppLayout from './components/AppLayout';
import AgentListView from './pages/AgentListView';
import AgentCreateForm from './pages/AgentCreateForm';
import AgentDetailView from './pages/AgentDetailView';
import EnvironmentListView from './pages/EnvironmentListView';
import EnvironmentCreateForm from './pages/EnvironmentCreateForm';
import EnvironmentDetailView from './pages/EnvironmentDetailView';
import SessionListView from './pages/SessionListView';
import SessionDetailView from './pages/SessionDetailView';
import VaultListView from './pages/VaultListView';
import VaultDetailView from './pages/VaultDetailView';

export const router = createBrowserRouter([
  {
    element: <AppLayout />,
    children: [
      { path: '/', element: <Navigate to="/agents" replace /> },
      { path: '/agents', element: <AgentListView /> },
      { path: '/agents/new', element: <AgentCreateForm /> },
      { path: '/agents/:id', element: <AgentDetailView /> },
      { path: '/environments', element: <EnvironmentListView /> },
      { path: '/environments/new', element: <EnvironmentCreateForm /> },
      { path: '/environments/:id', element: <EnvironmentDetailView /> },
      { path: '/sessions', element: <SessionListView /> },
      { path: '/sessions/:id', element: <SessionDetailView /> },
      { path: '/vaults', element: <VaultListView /> },
      { path: '/vaults/:id', element: <VaultDetailView /> },
    ],
  },
]);
