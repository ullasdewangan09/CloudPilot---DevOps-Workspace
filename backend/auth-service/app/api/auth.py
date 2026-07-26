from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.security import OAuth2PasswordRequestForm
import httpx

from sqlalchemy.orm import Session

from app.core.database import get_db

from app.schemas.user import UserRegister
from app.schemas.user import UserResponse

from app.services.auth_service import create_user
from app.schemas.user import UserLogin
from app.schemas.user import TokenResponse
from app.services.auth_service import authenticate_user
from app.services.auth_service import get_or_create_social_user
from app.core.security import create_access_token
from app.core.security import get_current_user
from app.models.user import User
from app.core.config import (
    FRONTEND_SUCCESS_URL,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    GOOGLE_REDIRECT_URI,
    GITHUB_CLIENT_ID,
    GITHUB_CLIENT_SECRET,
    GITHUB_REDIRECT_URI,
    MICROSOFT_CLIENT_ID,
    MICROSOFT_CLIENT_SECRET,
    MICROSOFT_REDIRECT_URI
)
from app.services.social_oauth_service import (
    fetch_github_profile,
    fetch_google_profile,
    fetch_microsoft_profile,
    github_authorization_url,
    google_authorization_url,
    microsoft_authorization_url,
    provider_status
)

router = APIRouter()


def build_token_response(user: User):
    token = create_access_token(
        {
            "sub": user.email,
            "role": user.role
        }
    )

    return {
        "access_token": token,
        "token_type": "bearer"
    }


@router.get("/health")
def auth_health():
    return {
        "status": "healthy"
    }


@router.get("/providers")
def providers():
    return provider_status()


@router.post("/register")
def register(
    user: UserRegister,
    db: Session = Depends(get_db)
):
    created_user, error = create_user(
        db,
        user
    )

    if not created_user:
        raise HTTPException(
            status_code=400,
            detail=error or "User already exists"
        )

    return {
        "message": "User created successfully",
        "user_id": created_user.id
    }


@router.get("/me", response_model=UserResponse)
def me(
    current_user: User = Depends(get_current_user)
):
    return current_user

@router.post("/login", response_model=TokenResponse)
def login(
    user: UserLogin,
    db: Session = Depends(get_db)
):
    db_user = authenticate_user(
        db,
        user.email,
        user.password
    )

    if not db_user:
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials"
        )

    return build_token_response(db_user)


@router.post("/token", response_model=TokenResponse)
def token(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db)
):
    db_user = authenticate_user(
        db,
        form_data.username,
        form_data.password
    )

    if not db_user:
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials"
        )

    return build_token_response(db_user)


@router.get("/mock/login", response_class=HTMLResponse)
def mock_login_page(provider: str, redirect_uri: str):
    title = provider.title()
    default_email = f"developer_{provider}@example.com"
    default_username = f"Dev{title}User"
    
    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>CloudPilot Identity Sandbox</title>
        <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&display=swap" rel="stylesheet">
        <style>
            :root {{
                --bg-gradient: linear-gradient(135deg, #0f172a 0%, #1e1b4b 100%);
                --card-bg: rgba(30, 41, 59, 0.7);
                --card-border: rgba(255, 255, 255, 0.08);
                --accent-google: #ef4444;
                --accent-github: #24292f;
                --accent-microsoft: #00a4ef;
                --text-primary: #f8fafc;
                --text-secondary: #94a3b8;
            }}
            
            * {{
                box-sizing: border-box;
                margin: 0;
                padding: 0;
            }}
            
            body {{
                font-family: 'Outfit', sans-serif;
                background: var(--bg-gradient);
                color: var(--text-primary);
                min-height: 100vh;
                display: flex;
                align-items: center;
                justify-content: center;
                overflow: hidden;
                position: relative;
            }}
            
            .blob {{
                position: absolute;
                border-radius: 50%;
                filter: blur(80px);
                opacity: 0.15;
                z-index: 0;
                animation: float 10s ease-in-out infinite;
            }}
            
            .blob-1 {{
                width: 400px;
                height: 400px;
                background: #4f46e5;
                top: -100px;
                left: -100px;
            }}
            
            .blob-2 {{
                width: 350px;
                height: 350px;
                background: #06b6d4;
                bottom: -50px;
                right: -50px;
                animation-delay: -5s;
            }}
            
            @keyframes float {{
                0%, 100% {{ transform: translate(0, 0) scale(1); }}
                50% {{ transform: translate(20px, -20px) scale(1.05); }}
            }}
            
            .container {{
                width: 100%;
                max-width: 450px;
                padding: 20px;
                z-index: 10;
            }}
            
            .card {{
                background: var(--card-bg);
                backdrop-filter: blur(16px);
                -webkit-backdrop-filter: blur(16px);
                border: 1px solid var(--card-border);
                border-radius: 24px;
                padding: 40px;
                box-shadow: 0 20px 40px rgba(0, 0, 0, 0.4);
                transform: translateY(20px);
                opacity: 0;
                animation: slideUp 0.6s cubic-bezier(0.16, 1, 0.3, 1) forwards;
            }}
            
            @keyframes slideUp {{
                to {{
                    transform: translateY(0);
                    opacity: 1;
                }}
            }}
            
            .header {{
                text-align: center;
                margin-bottom: 30px;
            }}
            
            .badge {{
                display: inline-block;
                padding: 6px 12px;
                background: rgba(99, 102, 241, 0.15);
                color: #818cf8;
                border-radius: 100px;
                font-size: 0.8rem;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 0.05em;
                margin-bottom: 12px;
                border: 1px solid rgba(99, 102, 241, 0.2);
            }}
            
            h1 {{
                font-size: 1.8rem;
                font-weight: 800;
                margin-bottom: 8px;
                background: linear-gradient(to right, #ffffff, #cbd5e1);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
            }}
            
            .subtitle {{
                font-size: 0.95rem;
                color: var(--text-secondary);
            }}
            
            .provider-badge {{
                display: flex;
                align-items: center;
                justify-content: center;
                gap: 10px;
                margin: 20px 0;
                padding: 12px;
                border-radius: 12px;
                background: rgba(255, 255, 255, 0.03);
                border: 1px solid rgba(255, 255, 255, 0.05);
                font-weight: 600;
            }}
            
            .provider-icon {{
                width: 24px;
                height: 24px;
                border-radius: 6px;
                display: flex;
                align-items: center;
                justify-content: center;
                font-size: 0.85rem;
                font-weight: bold;
                color: white;
            }}
            
            .provider-google {{
                background: #ea4335;
            }}
            .provider-github {{
                background: #24292f;
                border: 1px solid rgba(255, 255, 255, 0.15);
            }}
            .provider-microsoft {{
                background: #00a4ef;
            }}
            
            .form-group {{
                margin-bottom: 20px;
            }}
            
            label {{
                display: block;
                font-size: 0.85rem;
                font-weight: 600;
                color: var(--text-secondary);
                margin-bottom: 8px;
                text-transform: uppercase;
                letter-spacing: 0.05em;
            }}
            
            input {{
                width: 100%;
                background: rgba(15, 23, 42, 0.6);
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 12px;
                padding: 14px 16px;
                color: white;
                font-family: inherit;
                font-size: 1rem;
                transition: all 0.2s ease;
            }}
            
            input:focus {{
                outline: none;
                border-color: #6366f1;
                box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.25);
                background: rgba(15, 23, 42, 0.8);
            }}
            
            .btn {{
                width: 100%;
                background: linear-gradient(135deg, #6366f1 0%, #4f46e5 100%);
                color: white;
                border: none;
                border-radius: 12px;
                padding: 15px;
                font-family: inherit;
                font-size: 1rem;
                font-weight: 600;
                cursor: pointer;
                transition: all 0.2s ease;
                margin-top: 10px;
                box-shadow: 0 4px 12px rgba(79, 70, 229, 0.3);
            }}
            
            .btn:hover {{
                transform: translateY(-2px);
                box-shadow: 0 6px 20px rgba(79, 70, 229, 0.4);
                background: linear-gradient(135deg, #818cf8 0%, #6366f1 100%);
            }}
            
            .btn:active {{
                transform: translateY(0);
            }}
            
            .footer-info {{
                text-align: center;
                font-size: 0.8rem;
                color: #64748b;
                margin-top: 25px;
            }}
        </style>
    </head>
    <body>
        <div class="blob blob-1"></div>
        <div class="blob blob-2"></div>
        
        <div class="container">
            <div class="card">
                <div class="header">
                    <span class="badge">Sandbox Environment</span>
                    <h1>CloudPilot Auth</h1>
                    <p class="subtitle">Mock identity portal for local verification</p>
                </div>
                
                <div class="provider-badge">
                    <div class="provider-icon provider-{provider}">
                        {provider[0].upper()}
                    </div>
                    <span>Simulated {title} Authentication</span>
                </div>
                
                <form id="mock-form">
                    <div class="form-group">
                        <label for="email">Email Address</label>
                        <input type="email" id="email" value="{default_email}" required>
                    </div>
                    
                    <div class="form-group">
                        <label for="username">Display Name / Username</label>
                        <input type="text" id="username" value="{default_username}" required>
                    </div>
                    
                    <button type="submit" class="btn">Authorize & Login</button>
                </form>
                
                <div class="footer-info">
                    You are seeing this page because no OAuth credentials for {title} were detected in your environment.
                </div>
            </div>
        </div>
        
        <script>
            document.getElementById('mock-form').addEventListener('submit', function(e) {{
                e.preventDefault();
                const email = document.getElementById('email').value;
                const username = document.getElementById('username').value;
                const redirect_uri = "{redirect_uri}";
                
                const url = new URL(redirect_uri);
                url.searchParams.set('code', 'mock_code_' + Math.random().toString(36).substring(2, 15));
                url.searchParams.set('email', email);
                url.searchParams.set('username', username);
                url.searchParams.set('avatar_url', 'https://api.dicebear.com/7.x/adventurer/svg?seed=' + encodeURIComponent(username));
                
                window.location.href = url.toString();
            }});
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@router.get("/google/login")
def google_login(request: Request):
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        target_url = f"/api/v1/auth/mock/login?provider=google&redirect_uri={GOOGLE_REDIRECT_URI}"
    else:
        target_url = google_authorization_url()

    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return {"url": target_url}
    return RedirectResponse(target_url)


@router.get("/google/callback")
async def google_callback(
    request: Request,
    code: str,
    db: Session = Depends(get_db)
):
    return await social_callback(
        db,
        "google",
        code,
        fetch_google_profile,
        request
    )


@router.get("/github/login")
def github_login(request: Request):
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        target_url = f"/api/v1/auth/mock/login?provider=github&redirect_uri={GITHUB_REDIRECT_URI}"
    else:
        target_url = github_authorization_url()

    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return {"url": target_url}
    return RedirectResponse(target_url)


@router.get("/github/callback")
async def github_callback(
    request: Request,
    code: str,
    db: Session = Depends(get_db)
):
    return await social_callback(
        db,
        "github",
        code,
        fetch_github_profile,
        request
    )


@router.get("/microsoft/login")
def microsoft_login(request: Request):
    if not MICROSOFT_CLIENT_ID or not MICROSOFT_CLIENT_SECRET:
        target_url = f"/api/v1/auth/mock/login?provider=microsoft&redirect_uri={MICROSOFT_REDIRECT_URI}"
    else:
        target_url = microsoft_authorization_url()

    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return {"url": target_url}
    return RedirectResponse(target_url)


@router.get("/microsoft/callback")
async def microsoft_callback(
    request: Request,
    code: str,
    db: Session = Depends(get_db)
):
    return await social_callback(
        db,
        "microsoft",
        code,
        fetch_microsoft_profile,
        request
    )


async def social_callback(
    db: Session,
    provider: str,
    code: str,
    profile_loader,
    request: Request
):
    # Check if we are in mock mode
    is_mock = False
    if code.startswith("mock_") or code == "mock":
        is_mock = True

    from app.services.social_oauth_service import PROVIDER_CONFIG
    config = PROVIDER_CONFIG.get(provider, {})
    if not config.get("client_id") or not config.get("client_secret"):
        is_mock = True

    if is_mock:
        # Extract fields from query parameters
        email = request.query_params.get("email")
        username = request.query_params.get("username")
        avatar_url = request.query_params.get("avatar_url")

        if not email:
            email = f"mock_{provider}_user@example.com"
        if not username:
            username = email.split("@")[0]

        profile = {
            "provider_id": f"mock_{provider}_{abs(hash(email)) % 1000000}",
            "email": email,
            "username": username,
            "avatar_url": avatar_url or f"https://api.dicebear.com/7.x/adventurer/svg?seed={username}"
        }
    else:
        try:
            profile = await profile_loader(code)
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{provider.title()} OAuth failed"
            ) from exc

    user = get_or_create_social_user(
        db,
        provider=provider,
        provider_id=profile["provider_id"],
        email=profile["email"],
        username=profile["username"],
        avatar_url=profile.get("avatar_url")
    )

    token_data = build_token_response(user)
    access_token = token_data["access_token"]

    return RedirectResponse(
        url=f"{FRONTEND_SUCCESS_URL}?token={access_token}&token_type=bearer"
    )
