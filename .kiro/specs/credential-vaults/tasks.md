# Implementation Plan: Credential Vaults

## Overview

Implement secure, per-user credential management for Linchpin. The plan proceeds bottom-up: encryption service → database migration → Pydantic models → vault/credential CRUD routes → credential resolver → orchestrator integration → connector integration → console UI. Each step builds on the previous, with property tests validating correctness properties from the design.

## Tasks

- [x] 1. Implement EncryptionService
  - [x] 1.1 Create `linchpin-api/app/encryption.py` with `EncryptionService` class
    - Initialize Fernet with `VAULT_ENCRYPTION_KEY` env var
    - `encrypt(plaintext: str) -> bytes` and `decrypt(ciphertext: bytes) -> str` methods
    - Validate key on import; raise `RuntimeError` if missing or invalid Fernet key
    - _Requirements: 8.1, 8.2, 8.4, 8.5_

  - [ ]* 1.2 Write property test for encryption round-trip and opacity
    - **Property 6: Encryption round-trip and ciphertext opacity**
    - Generate random strings (unicode, empty, long), encrypt then decrypt, verify equality
    - Verify ciphertext bytes do not contain plaintext as substring
    - **Validates: Requirements 8.1, 8.5, 3.6**

  - [ ]* 1.3 Write unit tests for EncryptionService
    - Test encrypt/decrypt with known values
    - Test missing VAULT_ENCRYPTION_KEY raises error
    - Test invalid Fernet key raises error
    - Test empty string edge case
    - _Requirements: 8.1, 8.2, 8.4, 8.5_

- [x] 2. Create Alembic migration for vaults and credentials
  - [x] 2.1 Create `linchpin-api/alembic/versions/0002_vaults_credentials.py`
    - `vaults` table: id (UUID PK), display_name (TEXT NOT NULL), metadata (JSONB), created_at, updated_at, archived_at
    - `credentials` table: id (UUID PK), vault_id (UUID FK → vaults ON DELETE CASCADE), credential_type (TEXT NOT NULL), mcp_server_url (TEXT), provider (TEXT), encrypted_secrets (BYTEA), client_id (TEXT), created_at, updated_at, archived_at
    - Unique partial index on (vault_id, mcp_server_url) WHERE mcp_server_url IS NOT NULL AND archived_at IS NULL
    - Unique partial index on (vault_id, provider) WHERE provider IS NOT NULL AND archived_at IS NULL
    - Add `vault_ids` JSONB column (default '[]') to existing `sessions` table
    - Include downgrade to drop tables and column
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6_

- [x] 3. Add Pydantic models for vaults and credentials
  - [x] 3.1 Add vault and credential models to `linchpin-api/app/models.py`
    - `CredentialType`, `ProviderName` literals
    - `Vault`, `Credential` domain models
    - `CreateVaultRequest`, `CreateCredentialRequest` (discriminated union on credential_type), `UpdateCredentialRequest`
    - `VaultResponse`, `CredentialResponse` (secret fields omitted)
    - Update `CreateSessionRequest` and `SessionResponse` with `vault_ids: list[str] = []`
    - _Requirements: 1.1, 3.1, 3.2, 3.3, 4.4, 5.1, 7.1, 7.2, 7.3, 9.1, 9.2_

- [x] 4. Checkpoint — Ensure models and migration are correct
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Implement vault and credential CRUD routes
  - [x] 5.1 Create `linchpin-api/app/routes/vaults.py` with vault CRUD endpoints
    - `POST /v1/vaults` — create vault, return 201
    - `GET /v1/vaults` — list vaults (paginated)
    - `GET /v1/vaults/{vault_id}` — get vault, 404 if not found
    - `DELETE /v1/vaults/{vault_id}` — delete vault + cascade credentials, return 204
    - `POST /v1/vaults/{vault_id}/archive` — archive vault, set archived_at on vault and all credentials, purge encrypted_secrets, 409 if already archived
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.1, 2.2, 2.3, 2.4, 13.1, 13.2, 13.3, 13.4, 13.5_

  - [x] 5.2 Add credential CRUD endpoints to `linchpin-api/app/routes/vaults.py`
    - `POST /v1/vaults/{vault_id}/credentials` — create credential, encrypt secrets, enforce 20-credential limit, reject if vault archived, reject duplicate mcp_server_url/provider, return 201 with secrets redacted
    - `GET /v1/vaults/{vault_id}/credentials` — list credentials (paginated, secrets omitted)
    - `GET /v1/vaults/{vault_id}/credentials/{credential_id}` — get credential (secrets omitted)
    - `PATCH /v1/vaults/{vault_id}/credentials/{credential_id}` — update secret fields, reject if archived
    - `DELETE /v1/vaults/{vault_id}/credentials/{credential_id}` — delete credential, return 204
    - `POST /v1/vaults/{vault_id}/credentials/{credential_id}/archive` — archive credential, purge secrets
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 4.1, 4.2, 4.3, 4.4, 5.1, 5.2, 5.3, 5.4, 6.1, 6.2, 6.3, 6.4, 7.1, 7.2, 7.3, 13.6, 13.7, 13.8, 13.9, 13.10, 13.11, 13.12_

  - [x] 5.3 Register vault router in `linchpin-api/app/main.py`
    - Import and include vaults router on the v1_router
    - Add `VAULT_ENCRYPTION_KEY` validation at startup in lifespan
    - _Requirements: 8.4, 13.12_

  - [ ]* 5.4 Write property test for vault round-trip
    - **Property 1: Vault round-trip**
    - Generate random display_name + metadata, create + retrieve, verify equivalence
    - **Validates: Requirements 1.1, 1.2**

  - [ ]* 5.5 Write property test for vault delete cascades
    - **Property 2: Vault delete cascades to credentials**
    - Generate vaults with 0-5 credentials, delete vault, verify all gone (404)
    - **Validates: Requirements 1.5**

  - [ ]* 5.6 Write property test for vault archive purges secrets
    - **Property 3: Vault archive sets timestamps and purges secrets**
    - Generate vaults with credentials, archive, verify archived_at set and encrypted_secrets NULL
    - **Validates: Requirements 2.1, 2.2**

  - [ ]* 5.7 Write property test for credential round-trip with redaction
    - **Property 4: Credential round-trip with secret redaction**
    - Generate credentials of all three types, create + retrieve, verify non-secret fields match and secret fields absent
    - **Validates: Requirements 3.1, 3.2, 3.3, 4.1, 4.4**

  - [ ]* 5.8 Write property test for duplicate credential rejection
    - **Property 5: Duplicate credential rejection**
    - Generate vault + target (URL or provider), create twice, verify 409 on second
    - **Validates: Requirements 3.4, 3.5, 12.3, 12.4**

  - [ ]* 5.9 Write property test for write-only secret invariant
    - **Property 7: Write-only secret invariant**
    - Generate credentials, hit all response-producing endpoints, verify zero secret keys in every response
    - **Validates: Requirements 7.1, 7.2, 7.3**

  - [ ]* 5.10 Write property test for credential archive purges secrets
    - **Property 8: Credential archive purges secrets**
    - Generate credentials, archive, verify archived_at set and encrypted_secrets NULL
    - **Validates: Requirements 6.2**

  - [ ]* 5.11 Write unit tests for vault and credential routes
    - Test 201/204/404/409/422 responses for vault CRUD
    - Test all three credential types creation and retrieval
    - Test 20-credential limit enforcement
    - Test archived vault rejection for credential creation
    - _Requirements: 1.1–1.6, 2.1–2.4, 3.1–3.9, 4.1–4.4, 5.1–5.4, 6.1–6.4_

- [x] 6. Checkpoint — Ensure vault/credential CRUD works
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Implement credential resolver and session vault binding
  - [x] 7.1 Create `linchpin-api/app/credentials.py` with `CredentialResolver` class
    - `resolve_api_key(vault_ids, provider)` — search vaults in order for api_key credential, decrypt, return plaintext or None
    - `resolve_mcp_credential(vault_ids, mcp_server_url)` — search vaults in order for bearer_token/oauth credential, decrypt, return dict or None
    - Handle decryption failures gracefully (log, return None)
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 11.1, 11.2, 11.3, 11.5_

  - [x] 7.2 Update session creation in `linchpin-api/app/routes/sessions.py`
    - Validate `vault_ids` on session creation: each must exist and not be archived, else 422
    - Store vault_ids in session record (JSONB column)
    - Update `_row_to_session` to include vault_ids in SessionResponse
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5_

  - [ ]* 7.3 Write property test for session vault binding validation
    - **Property 9: Session vault binding validation**
    - Generate random vault_ids lists (valid, invalid, archived mix), verify correct accept/reject
    - **Validates: Requirements 9.1, 9.2, 9.3, 9.4**

  - [ ]* 7.4 Write property test for API key resolution with vault ordering
    - **Property 10: API key resolution with vault ordering and fallback**
    - Generate multi-vault configurations, verify first-match ordering and None fallback
    - **Validates: Requirements 10.1, 10.2, 10.3, 10.4**

  - [ ]* 7.5 Write property test for MCP credential resolution
    - **Property 11: MCP credential resolution**
    - Generate vault configurations with MCP credentials, verify correct resolution and None fallback
    - **Validates: Requirements 11.1, 11.2, 11.3, 11.5**

- [x] 8. Integrate credential resolution into orchestrator and providers
  - [x] 8.1 Modify provider adapters in `linchpin-api/app/providers.py`
    - Add optional `api_key: str | None = None` parameter to `send()` on all three providers
    - When `api_key` is provided, use it instead of SDK default env-var-based auth
    - AnthropicProvider: create client with explicit api_key
    - OpenAIProvider: create client with explicit api_key
    - OllamaProvider: pass api_key in headers if provided
    - _Requirements: 10.2_

  - [x] 8.2 Modify orchestrator in `linchpin-api/app/orchestrator.py`
    - Before calling `provider.send()`, resolve API key via `CredentialResolver.resolve_api_key(session.vault_ids, agent.model.provider)`
    - Fall back to env var if resolver returns None
    - Emit `session.error` and transition to `failed` if no key found anywhere
    - Before MCP tool dispatch, resolve MCP credentials via `CredentialResolver.resolve_mcp_credential(session.vault_ids, mcp_server_url)`
    - Pass resolved credentials to `invoke_connector()`
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 11.1, 11.2, 11.3, 11.5_

  - [ ]* 8.3 Write unit tests for orchestrator credential resolution
    - Test API key resolution from vault, fallback to env var, and failure case
    - Test MCP credential resolution and pass-through to connector
    - _Requirements: 10.1–10.5, 11.1–11.5_

- [x] 9. Integrate MCP credentials into connector
  - [x] 9.1 Modify `linchpin-connector/app/main.py` to handle credential injection
    - Update `ToolInvokeRequest.credentials` to carry `auth_type` + `token`/`access_token`
    - When credentials are present, inject as Authorization header on MCP HTTP calls
    - _Requirements: 11.4_

  - [ ]* 9.2 Write unit tests for connector credential injection
    - Test bearer token injection as Authorization header
    - Test oauth access_token injection
    - Test no credentials (proceed without auth)
    - _Requirements: 11.4, 11.5_

- [x] 10. Checkpoint — Ensure backend integration works end-to-end
  - Ensure all tests pass, ask the user if questions arise.

- [x] 11. Update docker-compose.yml
  - [x] 11.1 Add `VAULT_ENCRYPTION_KEY` env var to linchpin-api service in `docker-compose.yml`
    - Use `${VAULT_ENCRYPTION_KEY:-}` pattern consistent with other env vars
    - _Requirements: 8.2_

- [x] 12. Implement console UI — Vault management
  - [x] 12.1 Add vault API functions to `linchpin-console/src/api/client.ts`
    - Add `patch` method to apiClient
    - Add vault and credential TypeScript types to `linchpin-console/src/types.ts`
    - _Requirements: 13.1–13.11_

  - [x] 12.2 Create `VaultListView` page at `linchpin-console/src/pages/VaultListView.tsx`
    - Fetch vaults from `GET /v1/vaults`
    - Display display_name, credential count, created_at, archived status
    - "New vault" button opens create form
    - Delete and archive actions with confirmation dialogs
    - _Requirements: 14.2, 14.3, 14.8_

  - [x] 12.3 Create `VaultDetailView` page at `linchpin-console/src/pages/VaultDetailView.tsx`
    - Show vault metadata and list of credentials
    - Display credential type, target (mcp_server_url or provider), timestamps
    - Secret values shown as masked placeholders
    - "Add credential" button with form adapting to credential_type
    - Delete and archive actions for individual credentials with confirmation
    - _Requirements: 14.4, 14.5, 14.6, 14.7_

  - [x] 12.4 Add "Credential Vaults" link to Sidebar and register routes
    - Add NavLink to `linchpin-console/src/components/Sidebar.tsx`
    - Add `/vaults`, `/vaults/:id` routes to `linchpin-console/src/router.tsx`
    - _Requirements: 14.1_

- [x] 13. Implement console UI — Session vault selection
  - [x] 13.1 Update `SessionCreateDialog` to include vault multi-select
    - Add optional multi-select field populated from `GET /v1/vaults` (exclude archived)
    - Include selected vault IDs in `vault_ids` parameter of `POST /v1/sessions`
    - _Requirements: 15.1, 15.2_

  - [x] 13.2 Update `SessionDetailView` to display vault IDs
    - Show list of vault IDs associated with the session
    - _Requirements: 15.3_

- [x] 14. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- The implementation proceeds bottom-up: encryption → schema → models → routes → resolver → orchestrator → connector → UI
