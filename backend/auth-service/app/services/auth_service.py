from sqlalchemy.orm import Session

from app.models.user import User
from app.schemas.user import UserRegister
from app.core.security import hash_password
from app.core.security import verify_password


def get_user_by_email(
    db: Session,
    email: str
):
    return (
        db.query(User)
        .filter(User.email == email)
        .first()
    )


def get_user_by_username(
    db: Session,
    username: str
):
    return (
        db.query(User)
        .filter(User.username == username)
        .first()
    )


def create_user(
    db: Session,
    user: UserRegister
):
    if get_user_by_email(db, user.email):
        return None, "Email already exists"

    if get_user_by_username(db, user.username):
        return None, "Username already exists"

    db_user = User(
        username=user.username,
        email=user.email,
        password_hash=hash_password(
            user.password
        ),
        provider="local"
    )

    db.add(db_user)

    db.commit()

    db.refresh(db_user)

    return db_user, None


def authenticate_user(
    db: Session,
    email: str,
    password: str
):
    user = get_user_by_email(
        db,
        email
    )

    if not user or not user.password_hash:
        return None

    if not verify_password(
        password,
        user.password_hash
    ):
        return None

    return user


def get_or_create_social_user(
    db: Session,
    provider: str,
    provider_id: str,
    email: str,
    username: str,
    avatar_url: str | None = None
):
    user = get_user_by_email(
        db,
        email
    )

    if user:
        if user.provider == "local":
            user.provider_id = provider_id
            user.avatar_url = avatar_url or user.avatar_url
        db.commit()
        db.refresh(user)
        return user

    unique_username = username
    suffix = 1

    while get_user_by_username(db, unique_username):
        suffix += 1
        unique_username = f"{username}{suffix}"

    user = User(
        username=unique_username,
        email=email,
        password_hash=None,
        provider=provider,
        provider_id=provider_id,
        avatar_url=avatar_url
    )

    db.add(user)
    db.commit()
    db.refresh(user)

    return user
