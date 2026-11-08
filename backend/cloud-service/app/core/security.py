from datetime import datetime, timezone, timedelta
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import Session, declarative_base

from app.core.config import JWT_SECRET, JWT_ALGORITHM, FERNET_KEY
from app.core.database import get_db

# Fernet symmetric encryption helpers
def _get_fernet() -> Fernet:
    key = FERNET_KEY
    if isinstance(key, str):
        key = key.encode("utf-8")
    return Fernet(key)


def encrypt_credential(plain_text: str | None) -> str | None:
    """Encrypt sensitive credentials using Fernet symmetric encryption."""
    if not plain_text or not plain_text.strip():
        return None
    plain_bytes = plain_text.strip().encode("utf-8")
    fernet = _get_fernet()
    # If already encrypted (Fernet token test), return as is
    try:
        fernet.decrypt(plain_bytes)
        return plain_text.strip()
    except Exception:
        pass
    encrypted = fernet.encrypt(plain_bytes)
    return encrypted.decode("utf-8")


def decrypt_credential(cipher_text: str | None) -> str | None:
    """Decrypt sensitive credentials. Gracefully handles unencrypted values if any exist."""
    if not cipher_text or not cipher_text.strip():
        return None
    fernet = _get_fernet()
    try:
        decrypted = fernet.decrypt(cipher_text.strip().encode("utf-8"))
        return decrypted.decode("utf-8")
    except (InvalidToken, Exception):
        # Fallback for plain text to prevent breaking existing data
        return cipher_text.strip()


# Minimal User model for JWT verification only — references existing 'users' table
_Base = declarative_base()


class _UserRef(_Base):
    """Read-only reference to the shared users table for JWT identity resolution."""
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String, nullable=False)
    role = Column(String, nullable=False)
    username = Column(String, nullable=False)


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="http://localhost:8000/api/v1/auth/token")


def get_current_user(
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme)
) -> _UserRef:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = db.query(_UserRef).filter(_UserRef.email == email).first()
    if user is None:
        raise credentials_exception
    return user
