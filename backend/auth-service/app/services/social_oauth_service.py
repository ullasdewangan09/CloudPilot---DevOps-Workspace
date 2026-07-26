from fastapi import HTTPException, status
import httpx

from app.core.config import (
    GITHUB_CLIENT_ID,
    GITHUB_CLIENT_SECRET,
    GITHUB_REDIRECT_URI,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    GOOGLE_REDIRECT_URI,
    MICROSOFT_CLIENT_ID,
    MICROSOFT_CLIENT_SECRET,
    MICROSOFT_REDIRECT_URI,
    MICROSOFT_TENANT,
    with_query_params
)


PROVIDER_CONFIG = {
    "google": {
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "login_url": "/api/v1/auth/google/login"
    },
    "github": {
        "client_id": GITHUB_CLIENT_ID,
        "client_secret": GITHUB_CLIENT_SECRET,
        "redirect_uri": GITHUB_REDIRECT_URI,
        "login_url": "/api/v1/auth/github/login"
    },
    "microsoft": {
        "client_id": MICROSOFT_CLIENT_ID,
        "client_secret": MICROSOFT_CLIENT_SECRET,
        "redirect_uri": MICROSOFT_REDIRECT_URI,
        "login_url": "/api/v1/auth/microsoft/login"
    }
}


def provider_status() -> dict:
    return {
        name: {
            "configured": bool(config["client_id"] and config["client_secret"]),
            "redirect_uri": config["redirect_uri"],
            "login_url": config["login_url"]
        }
        for name, config in PROVIDER_CONFIG.items()
    }


def require_provider_config(
    provider: str,
    client_id: str | None,
    client_secret: str | None
):
    if not client_id or not client_secret:
        provider_key = provider.upper()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"{provider.title()} OAuth is not configured. "
                f"Set {provider_key}_CLIENT_ID and {provider_key}_CLIENT_SECRET "
                "in backend/auth-service/.env, then restart Docker Compose."
            )
        )


def google_authorization_url() -> str:
    require_provider_config(
        "google",
        GOOGLE_CLIENT_ID,
        GOOGLE_CLIENT_SECRET
    )

    return with_query_params(
        "https://accounts.google.com/o/oauth2/v2/auth",
        {
            "client_id": GOOGLE_CLIENT_ID,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "response_type": "code",
            "scope": "openid email profile",
            "access_type": "offline",
            "prompt": "consent"
        }
    )


def github_authorization_url() -> str:
    require_provider_config(
        "github",
        GITHUB_CLIENT_ID,
        GITHUB_CLIENT_SECRET
    )

    return with_query_params(
        "https://github.com/login/oauth/authorize",
        {
            "client_id": GITHUB_CLIENT_ID,
            "redirect_uri": GITHUB_REDIRECT_URI,
            "scope": "read:user user:email"
        }
    )


def microsoft_authorization_url() -> str:
    require_provider_config(
        "microsoft",
        MICROSOFT_CLIENT_ID,
        MICROSOFT_CLIENT_SECRET
    )

    return with_query_params(
        f"https://login.microsoftonline.com/{MICROSOFT_TENANT}/oauth2/v2.0/authorize",
        {
            "client_id": MICROSOFT_CLIENT_ID,
            "redirect_uri": MICROSOFT_REDIRECT_URI,
            "response_type": "code",
            "scope": "openid email profile User.Read",
            "response_mode": "query"
        }
    )


async def fetch_google_profile(code: str) -> dict:
    require_provider_config(
        "google",
        GOOGLE_CLIENT_ID,
        GOOGLE_CLIENT_SECRET
    )

    async with httpx.AsyncClient(timeout=10) as client:
        token_response = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": GOOGLE_REDIRECT_URI
            }
        )
        token_response.raise_for_status()
        access_token = token_response.json()["access_token"]

        profile_response = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        profile_response.raise_for_status()

    profile = profile_response.json()
    return {
        "provider_id": profile["id"],
        "email": profile["email"],
        "username": profile.get("name") or profile["email"].split("@")[0],
        "avatar_url": profile.get("picture")
    }


async def fetch_github_profile(code: str) -> dict:
    require_provider_config(
        "github",
        GITHUB_CLIENT_ID,
        GITHUB_CLIENT_SECRET
    )

    async with httpx.AsyncClient(timeout=10) as client:
        token_response = await client.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code": code,
                "redirect_uri": GITHUB_REDIRECT_URI
            },
            headers={"Accept": "application/json"}
        )
        token_response.raise_for_status()
        res_json = token_response.json()
        if "access_token" not in res_json:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"GitHub OAuth error: {res_json.get('error_description') or res_json.get('error') or 'No access token returned'}"
            )
        access_token = res_json["access_token"]

        profile_response = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        profile_response.raise_for_status()
        profile = profile_response.json()

        email = profile.get("email")
        if not email:
            email_response = await client.get(
                "https://api.github.com/user/emails",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            email_response.raise_for_status()
            emails = email_response.json()
            primary_email = next(
                (item for item in emails if item.get("primary")),
                None
            )
            email = primary_email["email"] if primary_email else None

    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="GitHub account email is not available"
        )

    return {
        "provider_id": str(profile["id"]),
        "email": email,
        "username": profile.get("login") or email.split("@")[0],
        "avatar_url": profile.get("avatar_url")
    }


async def fetch_microsoft_profile(code: str) -> dict:
    require_provider_config(
        "microsoft",
        MICROSOFT_CLIENT_ID,
        MICROSOFT_CLIENT_SECRET
    )

    async with httpx.AsyncClient(timeout=10) as client:
        token_response = await client.post(
            f"https://login.microsoftonline.com/{MICROSOFT_TENANT}/oauth2/v2.0/token",
            data={
                "client_id": MICROSOFT_CLIENT_ID,
                "client_secret": MICROSOFT_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": MICROSOFT_REDIRECT_URI,
                "scope": "openid email profile User.Read"
            }
        )
        token_response.raise_for_status()
        access_token = token_response.json()["access_token"]

        profile_response = await client.get(
            "https://graph.microsoft.com/v1.0/me",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        profile_response.raise_for_status()

    profile = profile_response.json()
    email = profile.get("mail") or profile.get("userPrincipalName")

    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Microsoft account email is not available"
        )

    return {
        "provider_id": profile["id"],
        "email": email,
        "username": profile.get("displayName") or email.split("@")[0],
        "avatar_url": None
    }
