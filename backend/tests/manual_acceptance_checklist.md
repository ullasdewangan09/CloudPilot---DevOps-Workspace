# Backend V2 Manual Acceptance Checklist

This document tracks verification tests for CloudPilot Authentication and Workspace Services. All tests are validated against the active PostgreSQL database instance.

---

## 1. Authentication Service

### [x] User Registration
- **Test**: `POST /api/v1/auth/register` with email/password payload.
- **Response**: `200 OK` (User created successfully).
- **Database**: Hashed password (bcrypt) and credentials stored under the `users` table.

### [x] User Login
- **Test**: `POST /api/v1/auth/login` with registered credentials.
- **Response**: `200 OK` returning:
  ```json
  {
    "access_token": "eyJhbG...",
    "token_type": "bearer"
  }
  ```

### [x] Verify Current User
- **Test**: `GET /api/v1/auth/me` with `Authorization: Bearer <token>`.
- **Response**: `200 OK` containing current authenticated user profile matching database columns.

### [x] Social OAuth Redirections
- **Test**: `GET /api/v1/auth/google/login` (and other providers).
- **Response**: Redirects to Sandbox portal when keys are absent, or to provider consent screen when keys are configured. Callback redirects back to `/docs?token=...` with valid token parameters.

---

## 2. Workspace Service

### [x] Create Workspace
- **Test**: `POST /api/v1/workspaces` with workspace details (icon, color, name, description, environment).
- **Response**: `201 Created` returning generated UUID workspace ID.
- **Database**: Inserted into `workspaces` table, automatic audit log `WORKSPACE_CREATED` entry added, and owner membership record set to `workspace_members`.

### [x] List Workspaces (with Pagination)
- **Test**: `GET /api/v1/workspaces?page=1&limit=10`.
- **Response**: `200 OK` returning total count and paginated active workspaces matching owner or member privileges.

### [x] Search Workspaces
- **Test**: `GET /api/v1/workspaces/search?q=testing`.
- **Response**: `200 OK` returning active user workspaces matching target substring in name or description.

### [x] Workspace Statistics
- **Test**: `GET /api/v1/workspaces/stats`.
- **Response**: `200 OK` returning exact grouped counts (total_workspaces, development, staging, testing, sandbox, production).

### [x] Update Workspace
- **Test**: `PUT /api/v1/workspaces/{workspace_id}`.
- **Response**: `200 OK` returning updated workspace properties, modification of `updated_at`, and logging of `WORKSPACE_UPDATED` audit entry.

### [x] Soft Delete Workspace
- **Test**: `DELETE /api/v1/workspaces/{workspace_id}`.
- **Response**: `200 OK` (Workspace deleted successfully) and logs `WORKSPACE_DELETED` audit log.

---

## 3. Security & Validation Rules

### [x] Check 1: Unauthorized Access (No JWT)
- **Test**: `GET /api/v1/workspaces` without Authorization Header.
- **Response**: `401 Unauthorized` (FastAPI dependency block).

### [x] Check 2: Cross-Workspace Access (User B accessing User A)
- **Test**: User B requests `GET /api/v1/workspaces/{workspace_id_owned_by_user_a}`.
- **Response**: `403 Forbidden` (returns permission error).

### [x] Check 3: Soft Delete DB Verification
- **Test**: Query database row for workspace after soft deletion:
  ```sql
  SELECT is_active FROM workspaces WHERE id = '<deleted_id>';
  ```
- **Response**: `is_active = false` (row persists but deactivated).
- **List check**: Submitting `GET /api/v1/workspaces` no longer lists the deleted workspace.
