import os
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware

from app.core.database import init_db
from app.api.cloud_accounts import router as cloud_accounts_router

init_db()

app = FastAPI(
    title="CloudPilot Cloud Service",
    version="1.0.0",
    description="Cloud Account Management microservice for CloudPilot DevOps Platform"
)

# Configure CORS Middleware with explicit origins for secure credential handling
cors_origins_env = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:3000,http://localhost:8000,http://localhost:8001,http://127.0.0.1:3000,http://127.0.0.1:8000,http://127.0.0.1:8001"
)
allowed_origins = [origin.strip() for origin in cors_origins_env.split(",") if origin.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "service": "cloud-service",
        "version": "1.0.0"
    }


app.include_router(
    cloud_accounts_router,
    prefix="/api/v1/cloud/accounts",
    tags=["Cloud Accounts"]
)
