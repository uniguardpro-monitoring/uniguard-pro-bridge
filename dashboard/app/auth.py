"""Authentication module."""
import time
from collections import defaultdict
from datetime import datetime, timezone

import bcrypt
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from starlette.requests import Request

from . import config
from .database import get_db, get_db_rw

serializer = URLSafeTimedSerializer(config.SECRET_KEY)

# In-memory rate limiter: {ip: [timestamp, ...]}
_login_attempts: dict[str, list[float]] = defaultdict(list)
_last_cleanup = 0.0


def _cleanup_rate_limiter():
    """Remove stale entries from the rate limiter to prevent memory growth."""
    global _last_cleanup
    now = time.monotonic()
    if now - _last_cleanup < 60:
        return
    _last_cleanup = now
    cutoff = now - config.LOGIN_LOCKOUT_SECONDS
    stale = [ip for ip, times in _login_attempts.items() if not times or times[-1] < cutoff]
    for ip in stale:
        del _login_attempts[ip]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def create_session_token(user_id: int, username: str, role: str, dealer_id: int | None = None) -> str:
    return serializer.dumps({
        "user_id": user_id, "username": username,
        "role": role, "dealer_id": dealer_id,
    })


def verify_session_token(token: str) -> dict | None:
    try:
        return serializer.loads(token, max_age=config.SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


def get_current_user(request: Request) -> dict | None:
    token = request.cookies.get("session")
    if not token:
        return None
    return verify_session_token(token)


def is_rate_limited(ip: str) -> bool:
    _cleanup_rate_limiter()
    now = time.monotonic()
    cutoff = now - config.LOGIN_LOCKOUT_SECONDS
    _login_attempts[ip] = [t for t in _login_attempts[ip] if t > cutoff]
    return len(_login_attempts[ip]) >= config.LOGIN_MAX_ATTEMPTS


def record_failed_login(ip: str) -> None:
    _login_attempts[ip].append(time.monotonic())


def clear_failed_logins(ip: str) -> None:
    _login_attempts.pop(ip, None)


def authenticate_user(username: str, password: str) -> dict | None:
    with get_db_rw() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, role, dealer_id FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if not row:
            return None
        if not verify_password(password, row["password_hash"]):
            return None
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (now, row["id"]))
    return {
        "user_id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "dealer_id": row["dealer_id"],
    }


def create_user(username: str, password: str, role: str = "operator", dealer_id: int | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db_rw() as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, role, dealer_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (username, hash_password(password), role, dealer_id, now),
        )


def change_password(user_id: int, current_password: str, new_password: str) -> bool:
    """Change a user's password. Returns True on success, False if current password is wrong."""
    with get_db_rw() as conn:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not row:
            return False
        if not verify_password(current_password, row["password_hash"]):
            return False
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), user_id),
        )
    return True


def get_users() -> list[dict]:
    """Get all dashboard users."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, username, role, dealer_id, created_at, last_login FROM users ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]


def delete_user(user_id: int) -> None:
    """Delete a user by ID."""
    with get_db_rw() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


def ensure_admin_exists() -> bool:
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if count == 0:
        create_user("admin", "changeme", "admin")
        return True
    return False
