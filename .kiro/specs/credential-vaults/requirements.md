# Requirements Document

## Introduction

Credential Vaults is a secure credential management system for Linchpin, inspired by Anthropic's managed agents credential vaults. Vaults are workspace-scoped collections of credentials associated with an end-user. Each vault stores two categories of secrets: MCP server credentials (bearer tokens and OAuth 2.0) for authenticating with remote MCP servers via the linchpin-connector, and model provider API keys (Anthropic, OpenAI, Ollama) so that the self-hosted orchestrator can call LLM providers on behalf of the user. Credentials are encrypted at rest in Postgres using Fernet symmetric encryption, and secret values are write-only — never returned in API responses. Vaults are referenced at session creation via a `vault_ids` parameter, and the orchestrator and connector resolve credentials from the vault at runtime.

## Glossary

- **Vault**: A named collection of Credential resources, identified by a UUID, with display_name, optional metadata, and lifecycle timestamps (created_at, updated_at, archived_at).
- **Credential**: A secret entry within a Vault, bound to a specific target (an mcp_server_url for MCP credentials, or a provider name for API key credentials). Each Credential has exactly one credential type.
- **Credential_Type**: One of three types: `bearer_token` (static bearer token for MCP servers or custom HTTP tools), `api_key` (model provider API key keyed by provider name), or `oauth` (OAuth 2.0 credentials with access_token and optional refresh_token support for MCP servers).
- **Linchpin_API**: The primary FastAPI service that exposes vault and credential CRUD endpoints, resolves model provider credentials at session runtime, and passes MCP credentials to the connector.
- **Linchpin_Connector**: The secondary Python service that receives MCP credentials from the Linchpin_API and injects them when communicating with remote MCP servers.
- **Orchestrator**: The async task within Linchpin_API that drives the agent loop for a session. The Orchestrator resolves model provider API keys from the session's vaults before calling the Model_Provider adapter.
- **Encryption_Service**: A component within Linchpin_API that encrypts and decrypts secret values using Fernet symmetric encryption with a server-side key configured via the `VAULT_ENCRYPTION_KEY` environment variable.
- **Secret_Field**: A credential field that contains sensitive data (token, access_token, refresh_token, client_secret). Secret fields are encrypted at rest and are write-only — the API accepts them on create and update but never returns them in GET responses.
- **Vault_Resolution**: The process by which the Orchestrator or Connector looks up credentials from the session's referenced vaults at runtime, matching by mcp_server_url or provider name.
- **Archive**: A soft-delete operation that sets the archived_at timestamp, purges encrypted secret values from storage, but retains the credential and vault records for auditing.

## Requirements

### Requirement 1: Vault CRUD

**User Story:** As a developer, I want to create, retrieve, list, and delete vaults, so that I can organize credentials for different users or use cases.

#### Acceptance Criteria

1. WHEN a valid vault creation request is received with a display_name, THE Linchpin_API SHALL create a Vault resource with id (UUID), display_name, metadata (optional JSON object), created_at, and updated_at timestamps, and return an HTTP 201 response containing the full Vault resource.
2. WHEN a GET request is received for a specific vault by id, THE Linchpin_API SHALL return the full Vault resource including display_name, metadata, created_at, updated_at, and archived_at.
3. WHEN a GET request is received for the vaults collection, THE Linchpin_API SHALL return a paginated list of Vault resources.
4. IF a GET request references a vault id that does not exist, THEN THE Linchpin_API SHALL return an HTTP 404 response with a descriptive error message.
5. WHEN a DELETE request is received for a vault by id, THE Linchpin_API SHALL permanently delete the Vault resource and all associated Credential resources, and return an HTTP 204 response.
6. IF a DELETE request references a vault id that does not exist, THEN THE Linchpin_API SHALL return an HTTP 404 response with a descriptive error message.

### Requirement 2: Vault Archival

**User Story:** As a developer, I want to archive a vault so that its credentials are no longer usable but the records are retained for auditing.

#### Acceptance Criteria

1. WHEN an archive request is received for a vault by id, THE Linchpin_API SHALL set the archived_at timestamp on the Vault resource and on all associated Credential resources.
2. WHEN a vault is archived, THE Encryption_Service SHALL purge all encrypted secret values from the associated Credential records while retaining the non-secret metadata (id, vault_id, credential_type, target, created_at, updated_at, archived_at).
3. IF an archive request references a vault that is already archived, THEN THE Linchpin_API SHALL return an HTTP 409 response with a descriptive error message.
4. IF an archive request references a vault id that does not exist, THEN THE Linchpin_API SHALL return an HTTP 404 response with a descriptive error message.

### Requirement 3: Credential Creation

**User Story:** As a developer, I want to add credentials to a vault, so that sessions referencing the vault can authenticate with MCP servers and model providers.

#### Acceptance Criteria

1. WHEN a valid credential creation request is received for a vault with credential_type set to `bearer_token`, THE Linchpin_API SHALL create a Credential resource with id, vault_id, credential_type, mcp_server_url (required), and the encrypted token value, and return an HTTP 201 response containing the Credential resource with secret fields redacted.
2. WHEN a valid credential creation request is received for a vault with credential_type set to `api_key`, THE Linchpin_API SHALL create a Credential resource with id, vault_id, credential_type, provider (required, one of: anthropic, openai, ollama), and the encrypted api_key value, and return an HTTP 201 response containing the Credential resource with secret fields redacted.
3. WHEN a valid credential creation request is received for a vault with credential_type set to `oauth`, THE Linchpin_API SHALL create a Credential resource with id, vault_id, credential_type, mcp_server_url (required), and the encrypted OAuth fields (access_token required; refresh_token, client_id, client_secret optional), and return an HTTP 201 response containing the Credential resource with secret fields redacted.
4. IF a credential creation request specifies an mcp_server_url that already has an active (non-archived) credential in the same vault, THEN THE Linchpin_API SHALL return an HTTP 409 response indicating a duplicate credential for that URL.
5. IF a credential creation request specifies a provider that already has an active (non-archived) credential in the same vault, THEN THE Linchpin_API SHALL return an HTTP 409 response indicating a duplicate credential for that provider.
6. WHEN a credential is created, THE Encryption_Service SHALL encrypt all secret field values before persisting them to Postgres.
7. IF a credential creation request contains invalid or missing required fields, THEN THE Linchpin_API SHALL return an HTTP 422 response with a descriptive error message.
8. IF a credential creation request targets a vault that is archived, THEN THE Linchpin_API SHALL return an HTTP 409 response indicating the vault is archived.
9. THE Linchpin_API SHALL enforce a maximum of 20 active (non-archived) credentials per vault. IF a creation request would exceed this limit, THEN THE Linchpin_API SHALL return an HTTP 409 response with a descriptive error message.

### Requirement 4: Credential Retrieval and Listing

**User Story:** As a developer, I want to retrieve and list credentials in a vault, so that I can inspect which credentials are configured without exposing secret values.

#### Acceptance Criteria

1. WHEN a GET request is received for a specific credential by id within a vault, THE Linchpin_API SHALL return the Credential resource with all secret fields omitted from the response.
2. WHEN a GET request is received for the credentials collection within a vault, THE Linchpin_API SHALL return a paginated list of Credential resources with all secret fields omitted from the responses.
3. IF a GET request references a credential id that does not exist within the specified vault, THEN THE Linchpin_API SHALL return an HTTP 404 response with a descriptive error message.
4. THE Linchpin_API SHALL include the following non-secret fields in credential responses: id, vault_id, credential_type, mcp_server_url (when applicable), provider (when applicable), client_id (when applicable), created_at, updated_at, and archived_at.

### Requirement 5: Credential Update (Secret Rotation)

**User Story:** As a developer, I want to update the secret values of a credential without recreating it, so that I can rotate keys and tokens seamlessly.

#### Acceptance Criteria

1. WHEN a PATCH request is received for a credential with updated secret fields, THE Linchpin_API SHALL encrypt and overwrite the secret field values and update the updated_at timestamp, and return the Credential resource with secret fields omitted.
2. WHEN a credential is updated, THE Encryption_Service SHALL encrypt the new secret values before persisting them to Postgres.
3. IF a PATCH request targets a credential that is archived, THEN THE Linchpin_API SHALL return an HTTP 409 response indicating the credential is archived.
4. IF a PATCH request references a credential id that does not exist, THEN THE Linchpin_API SHALL return an HTTP 404 response with a descriptive error message.

### Requirement 6: Credential Deletion and Archival

**User Story:** As a developer, I want to delete or archive individual credentials, so that I can manage credential lifecycle independently of the vault.

#### Acceptance Criteria

1. WHEN a DELETE request is received for a credential by id within a vault, THE Linchpin_API SHALL permanently delete the Credential resource and return an HTTP 204 response.
2. WHEN an archive request is received for a credential by id within a vault, THE Linchpin_API SHALL set the archived_at timestamp and THE Encryption_Service SHALL purge the encrypted secret values while retaining the non-secret metadata.
3. IF a DELETE or archive request references a credential id that does not exist, THEN THE Linchpin_API SHALL return an HTTP 404 response with a descriptive error message.
4. IF an archive request targets a credential that is already archived, THEN THE Linchpin_API SHALL return an HTTP 409 response with a descriptive error message.

### Requirement 7: Write-Only Secret Fields

**User Story:** As a developer, I want secret values to be write-only, so that credentials cannot be exfiltrated through the API.

#### Acceptance Criteria

1. THE Linchpin_API SHALL accept secret field values (token, api_key, access_token, refresh_token, client_secret) in POST and PATCH request bodies for credential creation and update.
2. THE Linchpin_API SHALL omit all secret field values from every GET response for vaults, credentials, and any resource that embeds credential data.
3. THE Linchpin_API SHALL omit all secret field values from every POST and PATCH response body for credential creation and update operations.

### Requirement 8: Encryption at Rest

**User Story:** As a developer, I want credentials encrypted at rest in the database, so that a database compromise does not expose plaintext secrets.

#### Acceptance Criteria

1. THE Encryption_Service SHALL encrypt all secret field values using Fernet symmetric encryption before writing them to Postgres.
2. THE Encryption_Service SHALL derive the Fernet key from the `VAULT_ENCRYPTION_KEY` environment variable configured on the Linchpin_API service.
3. THE Encryption_Service SHALL decrypt secret field values only when they are needed at runtime by the Orchestrator or Connector, and SHALL hold decrypted values in memory only for the duration of the operation.
4. IF the `VAULT_ENCRYPTION_KEY` environment variable is not set, THEN THE Linchpin_API SHALL refuse to start and log a descriptive error message.
5. FOR ALL valid secret values, encrypting then decrypting a secret value SHALL produce the original plaintext value (round-trip property).

### Requirement 9: Session Vault Binding

**User Story:** As a developer, I want to reference vaults when creating a session, so that the agent can use the credentials stored in those vaults at runtime.

#### Acceptance Criteria

1. WHEN a session creation request includes a vault_ids parameter (list of vault UUIDs), THE Linchpin_API SHALL validate that each referenced vault exists and is not archived.
2. WHEN a session is created with vault_ids, THE Linchpin_API SHALL store the vault references on the Session resource.
3. IF a session creation request references a vault id that does not exist, THEN THE Linchpin_API SHALL return an HTTP 422 response with a descriptive error message identifying the invalid vault id.
4. IF a session creation request references a vault that is archived, THEN THE Linchpin_API SHALL return an HTTP 422 response indicating the vault is archived.
5. WHEN a session is created without a vault_ids parameter, THE Linchpin_API SHALL create the session with an empty vault list, and the Orchestrator SHALL fall back to environment variable-based API keys (ANTHROPIC_API_KEY, OPENAI_API_KEY) for model provider authentication.

### Requirement 10: Model Provider Credential Resolution

**User Story:** As a developer, I want the orchestrator to automatically resolve model provider API keys from the session's vaults, so that each session can use per-user credentials without global environment variables.

#### Acceptance Criteria

1. WHEN the Orchestrator prepares a model provider request for a session with vault references, THE Orchestrator SHALL query the session's vaults for an `api_key` credential matching the agent's configured model provider.
2. WHEN a matching `api_key` credential is found in the session's vaults, THE Orchestrator SHALL decrypt the api_key value and use it to authenticate with the model provider for that request.
3. WHEN multiple vaults are referenced by a session, THE Orchestrator SHALL search the vaults in the order specified in the vault_ids list and use the first matching credential found.
4. IF no matching `api_key` credential is found in any of the session's vaults, THEN THE Orchestrator SHALL fall back to the environment variable for that provider (ANTHROPIC_API_KEY, OPENAI_API_KEY).
5. IF no credential is found in vaults and no environment variable is set for the provider, THEN THE Orchestrator SHALL emit a session.error event with a descriptive message indicating the missing API key.

### Requirement 11: MCP Credential Resolution

**User Story:** As a developer, I want the connector to automatically inject MCP server credentials from the session's vaults, so that MCP servers requiring authentication work without hardcoding secrets in agent configs.

#### Acceptance Criteria

1. WHEN the Orchestrator forwards an MCP tool invocation to the Linchpin_Connector, THE Orchestrator SHALL query the session's vaults for a `bearer_token` or `oauth` credential matching the MCP server's URL.
2. WHEN a matching `bearer_token` credential is found, THE Orchestrator SHALL decrypt the token value and include it in the tool invocation request to the Linchpin_Connector.
3. WHEN a matching `oauth` credential is found, THE Orchestrator SHALL decrypt the access_token value and include it in the tool invocation request to the Linchpin_Connector.
4. WHEN the Linchpin_Connector receives MCP credentials in a tool invocation request, THE Linchpin_Connector SHALL inject the credentials into the MCP server communication as an Authorization header or environment variable as appropriate.
5. IF no matching credential is found in the session's vaults for an MCP server URL, THEN THE Orchestrator SHALL proceed with the invocation without credentials (the MCP server may not require authentication).

### Requirement 12: Database Schema for Vaults and Credentials

**User Story:** As a developer, I want vaults and credentials stored in Postgres with proper schema and migrations, so that the data model is versioned and reproducible.

#### Acceptance Criteria

1. THE Linchpin_API SHALL define a `vaults` table with columns: id (UUID, primary key), display_name (text, required), metadata (JSONB, default empty object), created_at (timestamptz), updated_at (timestamptz), and archived_at (timestamptz, nullable).
2. THE Linchpin_API SHALL define a `credentials` table with columns: id (UUID, primary key), vault_id (UUID, foreign key to vaults), credential_type (text, required), mcp_server_url (text, nullable), provider (text, nullable), encrypted_secrets (BYTEA, nullable), client_id (text, nullable), created_at (timestamptz), updated_at (timestamptz), and archived_at (timestamptz, nullable).
3. THE credentials table SHALL have a unique constraint on (vault_id, mcp_server_url) WHERE mcp_server_url IS NOT NULL AND archived_at IS NULL, to enforce one active credential per MCP server URL per vault.
4. THE credentials table SHALL have a unique constraint on (vault_id, provider) WHERE provider IS NOT NULL AND archived_at IS NULL, to enforce one active credential per provider per vault.
5. THE Linchpin_API SHALL add a `vault_ids` column (JSONB, default empty array) to the existing sessions table.
6. THE Linchpin_API SHALL manage these schema changes via a new Alembic migration.

### Requirement 13: API Endpoints

**User Story:** As a developer, I want well-defined REST endpoints for managing vaults and credentials, so that I can integrate vault management into my workflows and tooling.

#### Acceptance Criteria

1. THE Linchpin_API SHALL expose `POST /v1/vaults` for creating a vault.
2. THE Linchpin_API SHALL expose `GET /v1/vaults` for listing vaults with pagination.
3. THE Linchpin_API SHALL expose `GET /v1/vaults/{vault_id}` for retrieving a single vault.
4. THE Linchpin_API SHALL expose `DELETE /v1/vaults/{vault_id}` for permanently deleting a vault and its credentials.
5. THE Linchpin_API SHALL expose `POST /v1/vaults/{vault_id}/archive` for archiving a vault.
6. THE Linchpin_API SHALL expose `POST /v1/vaults/{vault_id}/credentials` for creating a credential within a vault.
7. THE Linchpin_API SHALL expose `GET /v1/vaults/{vault_id}/credentials` for listing credentials within a vault with pagination.
8. THE Linchpin_API SHALL expose `GET /v1/vaults/{vault_id}/credentials/{credential_id}` for retrieving a single credential.
9. THE Linchpin_API SHALL expose `PATCH /v1/vaults/{vault_id}/credentials/{credential_id}` for updating credential secret values.
10. THE Linchpin_API SHALL expose `DELETE /v1/vaults/{vault_id}/credentials/{credential_id}` for permanently deleting a credential.
11. THE Linchpin_API SHALL expose `POST /v1/vaults/{vault_id}/credentials/{credential_id}/archive` for archiving a credential.
12. THE Linchpin_API SHALL require bearer token authentication for all vault and credential endpoints, consistent with existing API authentication.

### Requirement 14: Console UI — Vault Management

**User Story:** As a developer, I want to manage vaults and credentials through the Linchpin Console, so that I can configure credentials visually without using the API directly.

#### Acceptance Criteria

1. THE Console Sidebar SHALL include a "Credential Vaults" navigation link.
2. WHEN the developer navigates to the Credential Vaults section, THE Console SHALL display a list of vaults fetched from `GET /v1/vaults`, showing display_name, credential count, created_at, and archived status.
3. THE Console SHALL provide a "New vault" button that opens a form to create a vault with a display_name and optional metadata.
4. WHEN the developer clicks on a vault row, THE Console SHALL navigate to a vault detail view showing the vault's metadata and a list of its credentials.
5. THE vault detail view SHALL display each credential's type, target (mcp_server_url or provider), and timestamps, with secret values shown as masked placeholders.
6. THE vault detail view SHALL provide an "Add credential" button that opens a form to create a credential, with fields that adapt based on the selected credential_type.
7. THE vault detail view SHALL provide delete and archive actions for individual credentials, with a confirmation dialog for destructive actions.
8. THE vault list view SHALL provide delete and archive actions for vaults, with a confirmation dialog for destructive actions.

### Requirement 15: Console UI — Session Vault Selection

**User Story:** As a developer, I want to select vaults when creating a session through the console, so that the session can use the credentials stored in those vaults.

#### Acceptance Criteria

1. THE session creation form SHALL include an optional multi-select field for choosing vaults, populated from `GET /v1/vaults` (excluding archived vaults).
2. WHEN the developer selects one or more vaults and submits the session creation form, THE Console SHALL include the selected vault IDs in the `vault_ids` parameter of the `POST /v1/sessions` request.
3. THE Session_Detail_View SHALL display the list of vault IDs associated with the session.
