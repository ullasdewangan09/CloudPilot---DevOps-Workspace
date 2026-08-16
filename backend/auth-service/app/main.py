from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from app.api.auth import router as auth_router
from app.api.workspaces import router as workspace_router
from app.core.database import init_db

init_db()

app = FastAPI(
    title="CloudPilot Backend",
    version="1.0.0",
    description="Cloud-native DevOps platform backend"
)

# Configure CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return RedirectResponse(url="/login")




@app.get("/login", response_class=HTMLResponse)
def login_page():
    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>CloudPilot Portal</title>
        <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
        <style>
            :root {
                --bg-gradient: linear-gradient(135deg, #090d16 0%, #111827 50%, #1e1b4b 100%);
                --card-bg: rgba(17, 24, 39, 0.7);
                --card-border: rgba(255, 255, 255, 0.08);
                --accent-primary: #6366f1;
                --accent-primary-hover: #4f46e5;
                --text-primary: #f8fafc;
                --text-secondary: #94a3b8;
                --input-bg: rgba(15, 23, 42, 0.6);
            }
            
            * {
                box-sizing: border-box;
                margin: 0;
                padding: 0;
            }
            
            body {
                font-family: 'Outfit', sans-serif;
                background: var(--bg-gradient);
                color: var(--text-primary);
                min-height: 100vh;
                display: flex;
                align-items: center;
                justify-content: center;
                overflow-x: hidden;
                position: relative;
            }
            
            /* Background Blobs */
            .blob {
                position: absolute;
                border-radius: 50%;
                filter: blur(90px);
                opacity: 0.15;
                z-index: 0;
                animation: float 12s ease-in-out infinite;
            }
            
            .blob-1 {
                width: 450px;
                height: 450px;
                background: #4f46e5;
                top: -150px;
                left: -150px;
            }
            
            .blob-2 {
                width: 400px;
                height: 400px;
                background: #06b6d4;
                bottom: -100px;
                right: -100px;
                animation-delay: -6s;
            }
            
            @keyframes float {
                0%, 100% { transform: translate(0, 0) scale(1); }
                50% { transform: translate(30px, -30px) scale(1.05); }
            }
            
            .container {
                width: 100%;
                max-width: 460px;
                padding: 20px;
                z-index: 10;
            }
            
            .card {
                background: var(--card-bg);
                backdrop-filter: blur(16px);
                -webkit-backdrop-filter: blur(16px);
                border: 1px solid var(--card-border);
                border-radius: 28px;
                padding: 40px;
                box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
                transition: all 0.3s ease;
            }
            
            .header {
                text-align: center;
                margin-bottom: 30px;
            }
            
            .logo {
                font-size: 2.2rem;
                font-weight: 800;
                background: linear-gradient(135deg, #ffffff 0%, #cbd5e1 50%, #818cf8 100%);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                margin-bottom: 6px;
            }
            
            .subtitle {
                color: var(--text-secondary);
                font-size: 0.95rem;
            }
            
            /* Tabs */
            .tabs {
                display: flex;
                background: rgba(15, 23, 42, 0.4);
                padding: 4px;
                border-radius: 14px;
                margin-bottom: 25px;
                border: 1px solid rgba(255, 255, 255, 0.05);
            }
            
            .tab-btn {
                flex: 1;
                background: none;
                border: none;
                color: var(--text-secondary);
                padding: 12px;
                font-family: inherit;
                font-size: 0.95rem;
                font-weight: 600;
                cursor: pointer;
                border-radius: 10px;
                transition: all 0.25s ease;
            }
            
            .tab-btn.active {
                background: var(--accent-primary);
                color: white;
                box-shadow: 0 4px 12px rgba(99, 102, 241, 0.25);
            }
            
            /* Forms */
            .form-section {
                display: none;
            }
            
            .form-section.active {
                display: block;
                animation: fadeIn 0.4s ease forwards;
            }
            
            @keyframes fadeIn {
                from { opacity: 0; transform: translateY(8px); }
                to { opacity: 1; transform: translateY(0); }
            }
            
            .form-group {
                margin-bottom: 20px;
            }
            
            label {
                display: block;
                font-size: 0.85rem;
                font-weight: 600;
                color: var(--text-secondary);
                margin-bottom: 8px;
                text-transform: uppercase;
                letter-spacing: 0.05em;
            }
            
            input {
                width: 100%;
                background: var(--input-bg);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 12px;
                padding: 14px 16px;
                color: white;
                font-family: inherit;
                font-size: 1rem;
                transition: all 0.2s ease;
            }
            
            input:focus {
                outline: none;
                border-color: var(--accent-primary);
                box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.2);
                background: rgba(15, 23, 42, 0.8);
            }
            
            .btn {
                width: 100%;
                background: linear-gradient(135deg, var(--accent-primary) 0%, var(--accent-primary-hover) 100%);
                color: white;
                border: none;
                border-radius: 12px;
                padding: 15px;
                font-family: inherit;
                font-size: 1rem;
                font-weight: 600;
                cursor: pointer;
                transition: all 0.25s ease;
                box-shadow: 0 4px 14px rgba(99, 102, 241, 0.3);
                margin-top: 5px;
            }
            
            .btn:hover {
                transform: translateY(-1px);
                box-shadow: 0 6px 20px rgba(99, 102, 241, 0.4);
            }
            
            .btn:active {
                transform: translateY(0);
            }
            
            /* Divider */
            .divider {
                display: flex;
                align-items: center;
                margin: 25px 0;
                color: #4b5563;
                font-size: 0.8rem;
                text-transform: uppercase;
                letter-spacing: 0.1em;
            }
            
            .divider::before, .divider::after {
                content: "";
                flex: 1;
                height: 1px;
                background: rgba(255, 255, 255, 0.08);
            }
            
            .divider span {
                padding: 0 15px;
            }
            
            /* OAuth Grid */
            .oauth-grid {
                display: grid;
                grid-template-columns: 1fr;
                gap: 12px;
            }
            
            .oauth-btn {
                display: flex;
                align-items: center;
                justify-content: center;
                gap: 12px;
                background: rgba(255, 255, 255, 0.03);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 12px;
                padding: 12px;
                color: var(--text-primary);
                font-family: inherit;
                font-size: 0.95rem;
                font-weight: 600;
                cursor: pointer;
                transition: all 0.2s ease;
            }
            
            .oauth-btn:hover {
                background: rgba(255, 255, 255, 0.07);
                border-color: rgba(255, 255, 255, 0.15);
                transform: translateY(-1px);
            }
            
            .oauth-icon {
                width: 20px;
                height: 20px;
                display: flex;
                align-items: center;
                justify-content: center;
                font-size: 0.75rem;
                font-weight: bold;
                border-radius: 4px;
                color: white;
            }
            
            .icon-google { background: #ea4335; }
            .icon-github { background: #24292f; border: 1px solid rgba(255, 255, 255, 0.2); }
            .icon-microsoft { background: #00a4ef; }
            
            /* Notification / Alert Toast */
            .toast {
                display: none;
                margin-top: 15px;
                padding: 12px 16px;
                border-radius: 10px;
                font-size: 0.9rem;
                line-height: 1.4;
                text-align: center;
                border: 1px solid transparent;
            }
            
            .toast.error {
                display: block;
                background: rgba(239, 68, 68, 0.1);
                color: #fca5a5;
                border-color: rgba(239, 68, 68, 0.2);
            }
            
            .toast.success {
                display: block;
                background: rgba(16, 185, 129, 0.1);
                color: #a7f3d0;
                border-color: rgba(16, 185, 129, 0.2);
            }
            
            /* Token Details Display */
            .token-card {
                display: none;
                animation: fadeIn 0.5s ease forwards;
            }
            
            .token-card.active {
                display: block;
            }
            
            .token-box {
                background: #090d16;
                padding: 15px;
                border-radius: 12px;
                font-family: monospace;
                font-size: 0.8rem;
                word-break: break-all;
                max-height: 120px;
                overflow-y: auto;
                color: #a7f3d0;
                border: 1px solid rgba(16, 185, 129, 0.2);
                margin: 15px 0;
            }
        </style>
    </head>
    <body>
        <div class="blob blob-1"></div>
        <div class="blob blob-2"></div>
        
        <div class="container">
            <!-- Auth Form Card -->
            <div class="card" id="auth-card">
                <div class="header">
                    <div class="logo">CloudPilot</div>
                    <p class="subtitle">Platform Identity Portal</p>
                </div>
                
                <div class="tabs">
                    <button class="tab-btn active" onclick="switchTab('signin')">Sign In</button>
                    <button class="tab-btn" onclick="switchTab('signup')">Sign Up</button>
                </div>
                
                <!-- Sign In -->
                <div class="form-section active" id="signin-section">
                    <form id="signin-form">
                        <div class="form-group">
                            <label for="signin-email">Email Address</label>
                            <input type="email" id="signin-email" placeholder="you@example.com" required>
                        </div>
                        <div class="form-group">
                            <label for="signin-password">Password</label>
                            <input type="password" id="signin-password" placeholder="••••••••" required>
                        </div>
                        <button type="submit" class="btn">Sign In</button>
                    </form>
                </div>
                
                <!-- Sign Up -->
                <div class="form-section" id="signup-section">
                    <form id="signup-form">
                        <div class="form-group">
                            <label for="signup-username">Username</label>
                            <input type="text" id="signup-username" placeholder="developer101" required>
                        </div>
                        <div class="form-group">
                            <label for="signup-email">Email Address</label>
                            <input type="email" id="signup-email" placeholder="you@example.com" required>
                        </div>
                        <div class="form-group">
                            <label for="signup-password">Password</label>
                            <input type="password" id="signup-password" placeholder="••••••••" required>
                        </div>
                        <button type="submit" class="btn">Create Account</button>
                    </form>
                </div>
                
                <div class="toast" id="auth-toast"></div>
                
                <div class="divider">
                    <span>or connect with</span>
                </div>
                
                <!-- OAuth Grid -->
                <div class="oauth-grid">
                    <button class="oauth-btn" onclick="triggerOAuth('google')">
                        <div class="oauth-icon icon-google">G</div>
                        Continue with Google
                    </button>
                    <button class="oauth-btn" onclick="triggerOAuth('github')">
                        <div class="oauth-icon icon-github">G</div>
                        Continue with GitHub
                    </button>
                    <button class="oauth-btn" onclick="triggerOAuth('microsoft')">
                        <div class="oauth-icon icon-microsoft">M</div>
                        Continue with Microsoft
                    </button>
                </div>
            </div>
            
            <!-- Success Token Card -->
            <div class="card token-card" id="success-card">
                <div class="header">
                    <div class="logo" style="background: linear-gradient(135deg, #a7f3d0 0%, #10b981 100%); -webkit-background-clip: text;">Success!</div>
                    <p class="subtitle" id="success-subtitle">Authentication completed successfully.</p>
                </div>
                
                <label>Secure Access Token</label>
                <div class="token-box" id="token-display"></div>
                
                <button class="btn" style="background: linear-gradient(135deg, #10b981 0%, #059669 100%); box-shadow: 0 4px 14px rgba(16, 185, 129, 0.3);" onclick="window.open('/docs', '_blank')">Open API Documentation</button>
                <button class="btn" style="background: transparent; border: 1px solid rgba(255,255,255,0.15); box-shadow: none; margin-top: 10px;" onclick="logOut()">Sign Out</button>
            </div>
        </div>
        
        <script>
            // Tab Switching Logic
            function switchTab(tab) {
                const tabs = document.querySelectorAll('.tab-btn');
                const sections = document.querySelectorAll('.form-section');
                const toast = document.getElementById('auth-toast');
                toast.style.display = 'none';
                
                if (tab === 'signin') {
                    tabs[0].classList.add('active');
                    tabs[1].classList.remove('active');
                    sections[0].classList.add('active');
                    sections[1].classList.remove('active');
                } else {
                    tabs[0].classList.remove('active');
                    tabs[1].classList.add('active');
                    sections[0].classList.remove('active');
                    sections[1].classList.add('active');
                }
            }
            
            // Trigger API redirects for OAuth
            function triggerOAuth(provider) {
                window.location.href = '/api/v1/auth/' + provider + '/login';
            }
            
            // Show Status Toast
            function showToast(message, type) {
                const toast = document.getElementById('auth-toast');
                toast.textContent = message;
                toast.className = 'toast ' + type;
            }
            
            // Sign In Submit
            document.getElementById('signin-form').addEventListener('submit', async function(e) {
                e.preventDefault();
                const email = document.getElementById('signin-email').value;
                const password = document.getElementById('signin-password').value;
                
                try {
                    const response = await fetch('/api/v1/auth/login', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ email, password })
                    });
                    
                    const data = await response.json();
                    if (!response.ok) {
                        throw new Error(data.detail || 'Sign in failed. Check credentials.');
                    }
                    
                    handleSuccess(data.access_token, email);
                } catch (err) {
                    showToast(err.message, 'error');
                }
            });
            
            // Sign Up Submit
            document.getElementById('signup-form').addEventListener('submit', async function(e) {
                e.preventDefault();
                const username = document.getElementById('signup-username').value;
                const email = document.getElementById('signup-email').value;
                const password = document.getElementById('signup-password').value;
                
                try {
                    const response = await fetch('/api/v1/auth/register', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ username, email, password })
                    });
                    
                    const data = await response.json();
                    if (!response.ok) {
                        throw new Error(data.detail || 'Registration failed.');
                    }
                    
                    showToast('Account registered successfully! Switching to Sign In.', 'success');
                    setTimeout(() => {
                        document.getElementById('signin-email').value = email;
                        switchTab('signin');
                    }, 1500);
                } catch (err) {
                    showToast(err.message, 'error');
                }
            });
            
            // Handle Successful Login
            function handleSuccess(token, userIdentifier) {
                localStorage.setItem('cp_token', token);
                document.getElementById('auth-card').style.display = 'none';
                
                const successCard = document.getElementById('success-card');
                successCard.classList.add('active');
                
                document.getElementById('success-subtitle').textContent = 'Welcome back, ' + userIdentifier;
                document.getElementById('token-display').textContent = token;
            }
            
            function logOut() {
                localStorage.removeItem('cp_token');
                document.getElementById('success-card').classList.remove('active');
                document.getElementById('auth-card').style.display = 'block';
                showToast('Signed out successfully.', 'success');
            }
            
            // Check if already logged in or redirected with a token
            window.addEventListener('load', function() {
                const urlParams = new URLSearchParams(window.location.search);
                const token = urlParams.get('token');
                
                if (token) {
                    // Clear token from URI query params quietly
                    window.history.replaceState({}, document.title, window.location.pathname);
                    handleSuccess(token, 'authenticated user');
                } else {
                    const cachedToken = localStorage.getItem('cp_token');
                    if (cachedToken) {
                        handleSuccess(cachedToken, 'cached session');
                    }
                }
            });
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)



@app.get("/health")
def health_check():
    return {
        "status": "healthy"
    }

app.include_router(
    auth_router,
    prefix="/api/v1/auth",
    tags=["Authentication"]
)

app.include_router(
    workspace_router,
    prefix="/api/v1/workspaces",
    tags=["Workspaces"]
)
