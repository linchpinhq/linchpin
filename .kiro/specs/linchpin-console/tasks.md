# Implementation Plan: Linchpin Console

## Overview

Build a React + TypeScript + Vite SPA that provides a visual management interface for the Linchpin managed-agent runtime. The implementation proceeds bottom-up: CORS on the API, then project scaffolding, core modules (API client, SSE client), shared components, page-level views (agents, environments, sessions), real-time streaming, and finally Docker deployment.

## Tasks

- [x] 1. Add CORS middleware to linchpin-api
  - [x] 1.1 Add CORSMiddleware to FastAPI app
    - Add `CORSMiddleware` to `linchpin-api/app/main.py`
    - Read `CORS_ALLOWED_ORIGINS` env var (default `http://localhost:3000`), split on comma
    - Allow `Authorization` header, all standard methods, credentials
    - _Requirements: 20.1, 20.2, 20.3_

  - [ ]* 1.2 Write unit tests for CORS configuration
    - Test that OPTIONS preflight returns correct headers
    - Test that configurable origins are respected
    - _Requirements: 20.1, 20.2, 20.3_

- [x] 2. Scaffold linchpin-console project
  - [x] 2.1 Initialize Vite + React + TypeScript project
    - Run `npm create vite@latest linchpin-console -- --template react-ts` (or create equivalent files manually)
    - Install dependencies: `react-router-dom`, `@microsoft/fetch-event-source`
    - Install dev dependencies: `vitest`, `@testing-library/react`, `@testing-library/jest-dom`, `jsdom`, `fast-check`
    - Configure `vite.config.ts` with test setup and `VITE_API_URL` env var
    - _Requirements: 1.3, 1.4_

  - [x] 2.2 Define TypeScript types and interfaces
    - Create `src/types.ts` with all TypeScript interfaces from the design: `ModelConfig`, `Agent`, `Environment`, `Session`, `SessionEvent`, `EventType`, `PaginatedResponse`, `PaginatedEventsResponse`, request types
    - _Requirements: 4.2, 7.2, 9.2, 11.2, 12.3_

  - [x] 2.3 Set up React Router with route structure
    - Create `src/router.tsx` with all routes from the design routing table
    - `/` redirects to `/agents`
    - Routes for `/agents`, `/agents/new`, `/agents/:id`, `/environments`, `/environments/new`, `/environments/:id`, `/sessions`, `/sessions/:id`
    - _Requirements: 3.2_

- [x] 3. Implement API client module
  - [x] 3.1 Create API client with Bearer token injection
    - Create `src/api/client.ts` implementing the `ApiClient` interface from the design
    - Read `VITE_API_URL` for base URL (default `http://localhost:8000`)
    - Read API key from localStorage, inject as `Authorization: Bearer <key>`
    - On 401: clear stored key, redirect to API key input
    - On error: throw structured error with `status`, `error`, `message`, `details`
    - Set `Content-Type: application/json` for POST requests
    - _Requirements: 2.2, 2.3, 2.4, 19.3_

  - [ ]* 3.2 Write property test for API client Bearer token injection
    - **Property 1: API client injects Bearer token on every request**
    - Generate random API keys and paths, verify Authorization header contains exact stored key
    - **Validates: Requirements 2.2**

  - [ ]* 3.3 Write unit tests for API client
    - Test base URL configuration from env var
    - Test 401 handling clears key and redirects
    - Test error response parsing for 404, 422, 5xx
    - Test network error handling
    - _Requirements: 2.2, 2.4, 19.2, 19.3_

- [x] 4. Implement SSE client module
  - [x] 4.1 Create SSE client using @microsoft/fetch-event-source
    - Create `src/api/sse.ts` implementing the `SSEClient` interface from the design
    - Use `fetchEventSource` for SSE with `Authorization` header support
    - Track last received cursor for reconnection
    - Auto-reconnect on connection loss with last cursor
    - Cleanup on disconnect
    - _Requirements: 12.1, 12.4, 12.5_

  - [ ]* 4.2 Write unit tests for SSE client
    - Test connection lifecycle (connect, disconnect)
    - Test cursor tracking on event receipt
    - Test reconnection with last cursor
    - _Requirements: 12.1, 12.4, 12.5_

- [x] 5. Checkpoint — Core modules complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Implement shared UI components and layout
  - [x] 6.1 Create AppLayout and Sidebar components
    - Create `src/components/AppLayout.tsx` with sidebar + main content area
    - Create `src/components/Sidebar.tsx` with logo, nav links (Agents, Sessions, Environments), active indicator, health status, API key clear action
    - Use React Router `NavLink` for active highlighting
    - Responsive: collapse sidebar below 1024px viewport
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 18.1, 18.2_

  - [ ]* 6.2 Write property test for Sidebar active highlighting
    - **Property 2: Sidebar presence and active highlighting across all routes**
    - Generate random valid route paths, verify correct nav link is highlighted
    - **Validates: Requirements 3.1, 3.3**

  - [x] 6.3 Create StatusBadge component
    - Create `src/components/StatusBadge.tsx`
    - Map session statuses to colors: running→green, idle→blue, rescheduling→yellow, terminated→gray, failed→red
    - _Requirements: 17.1, 17.2_

  - [ ]* 6.4 Write property test for StatusBadge mapping
    - **Property 12: StatusBadge renders correct style per session status**
    - Generate random SessionStatus values, verify correct color and label
    - **Validates: Requirements 17.1**

  - [x] 6.5 Create shared utility components
    - Create `src/components/LoadingSpinner.tsx` — full-page and inline loading indicator
    - Create `src/components/ErrorMessage.tsx` — displays API error with status code, detail, optional retry button
    - Create `src/components/ConfirmDialog.tsx` — modal confirmation for destructive actions
    - Create `src/components/Pagination.tsx` — load-more or next/prev controls
    - _Requirements: 17.3, 17.4, 19.1, 19.2_

  - [ ]* 6.6 Write property test for ErrorMessage rendering
    - **Property 13: Error message renders status code and detail**
    - Generate random status codes and detail strings, verify both rendered
    - **Validates: Requirements 17.4**

  - [x] 6.7 Create API key input screen
    - Create `src/components/ApiKeyInput.tsx`
    - Shown when no API key in localStorage
    - Saves key to localStorage on submit
    - _Requirements: 2.1, 2.3, 2.5_

- [x] 7. Checkpoint — Layout and shared components complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Implement Agent views
  - [x] 8.1 Implement AgentListView
    - Create `src/pages/AgentListView.tsx`
    - Fetch `GET /v1/agents`, render table with columns: ID, Name, Model (provider + model id), Created
    - Client-side search by ID
    - Pagination support
    - "New agent" button navigating to `/agents/new`
    - Row click navigates to `/agents/:id`
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6_

  - [ ]* 8.2 Write property test for Agent list row rendering
    - **Property 3: Agent list row renders all required columns**
    - Generate random Agent objects, verify all columns present in rendered output
    - **Validates: Requirements 4.2**

  - [ ]* 8.3 Write property test for client-side ID search filtering
    - **Property 4: Client-side ID search filtering**
    - Generate random agent lists and search strings, verify correct filtering
    - **Validates: Requirements 4.3, 9.3**

  - [x] 8.4 Implement AgentDetailView
    - Create `src/pages/AgentDetailView.tsx`
    - Fetch `GET /v1/agents/{id}`, display full config: name, ID, version, model config, system prompt, tools with permissions, MCP servers, created_at
    - Back link to agent list
    - 404 handling
    - _Requirements: 5.1, 5.2, 5.3, 5.4_

  - [ ]* 8.5 Write property test for Agent detail rendering
    - **Property 5: Agent detail view renders all configuration fields**
    - Generate random Agent objects, verify all fields present
    - **Validates: Requirements 5.2**

  - [x] 8.6 Implement AgentCreateForm
    - Create `src/pages/AgentCreateForm.tsx`
    - Fields: name, model provider select, model id, conditional base_url (ollama only), system prompt textarea
    - Dynamic tool entries (type, name, permission_policy)
    - Dynamic MCP server entries (name, command, args, env key-value pairs)
    - POST `/v1/agents`, navigate to detail on success
    - Inline 422 error display, submit button disabled while in flight
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6_

  - [ ]* 8.7 Write unit tests for AgentCreateForm
    - Test form submission sends correct request body
    - Test 422 error display
    - Test submit button disabled during flight
    - _Requirements: 6.4, 6.5, 6.6_

- [x] 9. Implement Environment views
  - [x] 9.1 Implement EnvironmentListView
    - Create `src/pages/EnvironmentListView.tsx`
    - Fetch `GET /v1/environments`, render table with columns: ID, Name, Networking Type, Created
    - Pagination support
    - "New environment" button navigating to `/environments/new`
    - Row click navigates to `/environments/:id`
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5_

  - [ ]* 9.2 Write property test for Environment list row rendering
    - **Property 6: Environment list row renders all required columns**
    - Generate random Environment objects, verify all columns present
    - **Validates: Requirements 7.2**

  - [x] 9.3 Implement EnvironmentDetailView
    - Create `src/pages/EnvironmentDetailView.tsx`
    - Fetch `GET /v1/environments/{id}`, display full config
    - Back link to environment list
    - _Requirements: 7.4_

  - [x] 9.4 Implement EnvironmentCreateForm
    - Create `src/pages/EnvironmentCreateForm.tsx`
    - Fields: name (text), networking type (select: none, unrestricted)
    - POST `/v1/environments`, navigate to environment list on success
    - 422 error display, submit button disabled while in flight
    - _Requirements: 8.1, 8.2, 8.3, 8.4_

- [x] 10. Checkpoint — Agent and Environment views complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 11. Implement Session views
  - [x] 11.1 Implement SessionListView
    - Create `src/pages/SessionListView.tsx`
    - Fetch `GET /v1/sessions`, render table with columns: ID, Title (or "Untitled"), Status (StatusBadge), Agent (agent_id), Created
    - Client-side search by ID
    - Agent dropdown filter using `agent_id` query param
    - Archived toggle
    - Pagination support
    - "New session" button opens SessionCreateDialog
    - Row click navigates to `/sessions/:id`
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8_

  - [ ]* 11.2 Write property test for Session list row rendering
    - **Property 7: Session list row renders all required columns**
    - Generate random Session objects, verify all columns and StatusBadge
    - **Validates: Requirements 9.2**

  - [x] 11.3 Implement SessionCreateDialog
    - Create `src/components/SessionCreateDialog.tsx`
    - Agent dropdown (fetched from API), environment dropdown (fetched from API)
    - Optional title, optional TTL
    - POST `/v1/sessions`, navigate to session detail on success
    - Error display
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5_

  - [x] 11.4 Implement SessionDetailView
    - Create `src/pages/SessionDetailView.tsx`
    - Fetch `GET /v1/sessions/{id}`, display metadata: ID, title, status badge, agent_id, environment_id, created_at, updated_at, container_id, stats, usage
    - Back link to session list
    - 404 handling
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5_

  - [ ]* 11.5 Write property test for Session detail rendering
    - **Property 8: Session detail view renders all metadata fields**
    - Generate random Session objects, verify all fields present
    - **Validates: Requirements 11.2**

- [x] 12. Implement Event Stream and Session Interactions
  - [x] 12.1 Implement EventStreamPanel
    - Create `src/components/EventStreamPanel.tsx`
    - Fetch historical events from `GET /v1/sessions/{id}/events` with cursor pagination
    - Connect SSE for live events on non-terminated sessions
    - Merge historical + live events without duplicates, ordered by seq
    - Visual distinction per event type (user messages, agent messages, thinking, tool use, tool results, status changes, errors)
    - Auto-scroll to latest event unless user has scrolled up
    - _Requirements: 12.1, 12.2, 12.3, 12.6, 16.1, 16.2, 16.3_

  - [ ]* 12.2 Write property test for event merge logic
    - **Property 9: Event list merge preserves chronological order without duplicates**
    - Generate random historical + live event lists with overlapping cursors, verify unique cursors in ascending seq order
    - **Validates: Requirements 12.2, 16.3**

  - [ ]* 12.3 Write property test for event type visual classification
    - **Property 10: Event type visual classification**
    - Generate random EventType values, verify distinct visual category assigned
    - **Validates: Requirements 12.3**

  - [x] 12.4 Implement MessageInput component
    - Create `src/components/MessageInput.tsx`
    - Text input + send button, POST `/v1/sessions/{id}/events` with `user.message` type
    - Disabled when session is terminal (terminated/failed)
    - Submit button disabled while sending
    - Error display near input
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5_

  - [ ]* 12.5 Write property test for terminal session input disabled
    - **Property 11: Terminal session disables message input**
    - Generate sessions with terminal statuses, verify input disabled
    - **Validates: Requirements 13.5**

  - [x] 12.6 Implement ToolConfirmationCard component
    - Create `src/components/ToolConfirmationCard.tsx`
    - Display pending tool call details (tool name, arguments) with Approve/Reject buttons
    - Approve sends `user.tool_confirmation` with approval payload
    - Reject sends `user.tool_confirmation` with rejection payload
    - Visual highlight for pending action
    - _Requirements: 15.1, 15.2, 15.3, 15.4_

  - [x] 12.7 Implement session lifecycle actions
    - Add Terminate button to SessionDetailView (with ConfirmDialog)
    - `DELETE /v1/sessions/{id}` on confirm, update displayed status
    - Add Archive button for non-archived sessions
    - `POST /v1/sessions/{id}/archive` on click, update displayed state
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.5_

- [x] 13. Checkpoint — All views and interactions complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 14. Docker deployment and docker-compose integration
  - [x] 14.1 Create Dockerfile for linchpin-console
    - Create `linchpin-console/Dockerfile`
    - Multi-stage build: Node for build, nginx for serving
    - Copy built SPA to nginx html directory
    - Configure nginx for SPA routing (fallback to index.html)
    - _Requirements: 1.1, 1.2, 1.3_

  - [x] 14.2 Add linchpin-console service to docker-compose.yml
    - Add `linchpin-console` service on port 3000
    - Set `VITE_API_URL` environment variable
    - Add `depends_on: linchpin-api`
    - Add `CORS_ALLOWED_ORIGINS` env var to linchpin-api service
    - _Requirements: 1.1, 1.2, 1.4, 1.5, 20.3_

- [x] 15. Final checkpoint — Full integration
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design (13 properties)
- The implementation language is TypeScript (React + Vite) for the console, Python for the CORS addition to linchpin-api
