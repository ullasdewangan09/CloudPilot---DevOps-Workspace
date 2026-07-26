# CloudPilot OAuth Setup

Local password login works without any OAuth provider credentials.
Google, GitHub, and Microsoft login require OAuth apps created in your own provider accounts.

After adding any credential, restart Docker Compose:

```powershell
docker compose down
docker compose up --build
```

Check provider status in Swagger:

```txt
GET /api/v1/auth/providers
```

## Google

Add this authorized redirect URI in Google Cloud Console:

```txt
http://localhost:8000/api/v1/auth/google/callback
```

Then set:

```env
GOOGLE_CLIENT_ID=your_google_client_id
GOOGLE_CLIENT_SECRET=your_google_client_secret
GOOGLE_REDIRECT_URI=http://localhost:8000/api/v1/auth/google/callback
```

Login URL:

```txt
http://localhost:8000/api/v1/auth/google/login
```

## GitHub

Add this authorization callback URL in your GitHub OAuth App:

```txt
http://localhost:8000/api/v1/auth/github/callback
```

Then set:

```env
GITHUB_CLIENT_ID=your_github_client_id
GITHUB_CLIENT_SECRET=your_github_client_secret
GITHUB_REDIRECT_URI=http://localhost:8000/api/v1/auth/github/callback
```

Login URL:

```txt
http://localhost:8000/api/v1/auth/github/login
```

## Microsoft

Add this redirect URI in Microsoft Entra ID:

```txt
http://localhost:8000/api/v1/auth/microsoft/callback
```

Then set:

```env
MICROSOFT_CLIENT_ID=your_microsoft_client_id
MICROSOFT_CLIENT_SECRET=your_microsoft_client_secret
MICROSOFT_REDIRECT_URI=http://localhost:8000/api/v1/auth/microsoft/callback
MICROSOFT_TENANT=common
```

Login URL:

```txt
http://localhost:8000/api/v1/auth/microsoft/login
```

## Developer Mock OAuth Sandbox

To facilitate local development and verification without requiring actual developer accounts and client credentials:
- If a provider's client ID and secret are **not** configured in the `.env` file, the service automatically initiates a **Mock OAuth flow**.
- Navigating to any of the login endpoints (e.g. `/api/v1/auth/google/login`) will redirect you to a custom developer identity sandbox form.
- You can fill in any email address (e.g. a Gmail address, your own GitHub email, etc.) and display name, then click **Authorize & Login**.
- The login callback handler will register or login the user in the local PostgreSQL database, generate a secure JWT access token, and redirect your browser back to the frontend success URL (which defaults to `/docs` for Swagger UI testing) with the query params:
  `?token=ACCESS_TOKEN&token_type=bearer`

