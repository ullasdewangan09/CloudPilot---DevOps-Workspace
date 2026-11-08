# CloudPilot - DevOps Workspace

CloudPilot is a modular DevOps workspace and cloud management platform designed to orchestrate cloud infrastructure, microservices, and multi-tenant team workspaces with centralized authentication and role-based access control.

---

## Architecture Overview

CloudPilot follows a microservices architecture:

- **Auth Service** (`backend/auth-service`):
  - Port: `8000`
  - Manages User Registration, JWT Login, OAuth2 Social Logins (Google, GitHub, Microsoft).
  - Handles Workspace CRUD, member role assignments, and workspace audit logs.
- **Cloud Service** (`backend/cloud-service`):
  - Port: `8001`
  - Manages Cloud Provider Accounts (AWS, etc.) connected to individual workspaces.
  - Validates credentials and handles cloud resource interaction.
- **API Gateway** (`backend/api-gateway`):
  - Central ingress point routing requests to downstream microservices.
- **Infrastructure Services**:
  - PostgreSQL (`5432`): Persistent data storage for users, workspaces, memberships, and cloud account credentials.
  - Redis (`6379`): Token caching, sessions, and asynchronous job queuing.

---

## Prerequisites

Before setting up the project on a new laptop/environment, ensure you have installed:

- **Python 3.10+** (with `pip` and `venv`)
- **Docker Desktop** (or Docker Engine + Docker Compose)
- **Git**
- **PostgreSQL 14+** (if running services locally without Docker)

---

## Quick Start on a New Laptop

### 1. Clone the Repository
```bash
git clone https://github.com/ullasdewangan09/CloudPilot---DevOps-Workspace.git
cd CloudPilot---DevOps-Workspace
```

### 2. Configure Environment Variables
Copy the `.env.example` templates to `.env` in the root and in the respective service directories:

```bash
# In the project root
cp .env.example .env

# In Auth Service
cp backend/auth-service/.env.example backend/auth-service/.env

# In Cloud Service
cp backend/cloud-service/.env.example backend/cloud-service/.env
```

> **Important**: Populate the `.env` files with your actual database credentials, JWT secrets, and OAuth Client IDs / Secrets as required.

---

## Running with Docker Compose (Recommended)

To start the platform services together:

```bash
# Start all containers in detached mode:
docker compose up -d

# Rebuild images and start:
docker compose up --build -d

# View live container logs:
docker compose logs -f

# Stop all containers:
docker compose down
```

---

## Running Standalone Locally

### 1. Auth Service
```bash
cd backend/auth-service
python -m venv .venv
# Activate venv:
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# Linux / macOS:
source .venv/bin/activate

pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000
```

### 2. Cloud Service
```bash
cd backend/cloud-service
python -m venv .venv
# Activate venv:
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# Linux / macOS:
source .venv/bin/activate

pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8001
```

---

## API Documentation & Portals

When services are running, interactive Swagger OpenAPI documentation is available at:

- **Auth Service Docs:** [http://localhost:8000/docs](http://localhost:8000/docs)
- **Auth Login Page:** [http://localhost:8000/login](http://localhost:8000/login)
- **Cloud Service Docs:** [http://localhost:8001/docs](http://localhost:8001/docs)
- **Cloud Service Health:** [http://localhost:8001/health](http://localhost:8001/health)

---

## Automated Testing & CI/CD (Phase 4A)

CloudPilot maintains an automated regression testing framework built with `pytest`, `pytest-cov`, and `httpx`.

All tests execute against an isolated test database (`cloudpilot_test`) to ensure zero impact on development data.

### 1. Run Complete Test Suite
```bash
python run_tests.py
```
This executes all service test suites, measures line & branch coverage, and prints a consolidated pass/fail report.

### 2. Run Individual Service Suites
```bash
# Run Auth & Workspace Service tests:
pytest backend/auth-service/tests -v

# Run Cloud Service tests:
pytest backend/cloud-service/tests -v
```

### 3. Continuous Integration (CI)
GitHub Actions workflow (`.github/workflows/ci.yml`) runs on every pull request and push to `main`:
- Spins up an isolated PostgreSQL 16 container.
- Installs dependencies on Python 3.12.
- Executes all 46 automated regression tests.
- Enforces credential encryption, workspace RBAC, and multi-tenancy isolation.

---

## Quick Reference Commands

Refer to [`commands.txt`](./commands.txt) for a complete list of execution commands, Docker management workflows, and service URLs.

