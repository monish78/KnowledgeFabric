"""Authentication: local (Postgres users + HS256 JWT) or Keycloak (RS256 JWT via JWKS).

AUTH_PROVIDER picks the provider. Routes depend on `current_user`, which is the
same for both, so nothing else in the app knows which provider is active.
"""
import datetime as dt
from dataclasses import dataclass
from functools import lru_cache

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings, get_settings
from app.db import get_conn

LOCAL_ISSUER = "graphbase-local"
# Checked against when the user doesn't exist, so response time doesn't reveal valid user IDs.
_DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt()).decode()


@dataclass
class CurrentUser:
    user_id: str
    display_name: str
    email: str | None


def _unauthorized(detail: str = "Invalid or expired token") -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"})


# ------------------------------------------------------------------ passwords
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), (password_hash or _DUMMY_HASH).encode()) and password_hash is not None
    except ValueError:
        return False


# ------------------------------------------------------------------ local provider
def local_login(user_id: str, password: str) -> tuple[str, CurrentUser]:
    s = get_settings()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT user_id, display_name, email, password_hash, is_active FROM users WHERE user_id = %s",
            (user_id,),
        ).fetchone()
        if not verify_password(password, row["password_hash"] if row else None) or not row["is_active"]:
            raise _unauthorized("Incorrect user ID or password")
        conn.execute("UPDATE users SET last_login_at = now() WHERE user_id = %s", (user_id,))
    now = dt.datetime.now(dt.timezone.utc)
    token = jwt.encode(
        {"sub": row["user_id"], "name": row["display_name"], "iss": LOCAL_ISSUER, "iat": now,
         "exp": now + dt.timedelta(minutes=s.jwt_expire_minutes)},
        s.jwt_secret, algorithm="HS256",
    )
    return token, CurrentUser(row["user_id"], row["display_name"], row["email"])


def _authenticate_local(token: str, s: Settings) -> CurrentUser:
    try:
        claims = jwt.decode(token, s.jwt_secret, algorithms=["HS256"], issuer=LOCAL_ISSUER,
                            options={"require": ["sub", "exp", "iss"]})
    except jwt.PyJWTError:
        raise _unauthorized()
    with get_conn() as conn:
        row = conn.execute("SELECT user_id, display_name, email, is_active FROM users WHERE user_id = %s",
                           (claims["sub"],)).fetchone()
    if not row or not row["is_active"]:
        raise _unauthorized("User is disabled or no longer exists")
    return CurrentUser(row["user_id"], row["display_name"], row["email"])


# ------------------------------------------------------------------ keycloak provider
@lru_cache
def _jwks_client(certs_url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(certs_url, cache_keys=True, lifespan=3600)


def keycloak_issuer(s: Settings) -> str:
    return f"{s.keycloak_url.rstrip('/')}/realms/{s.keycloak_realm}"


def _authenticate_keycloak(token: str, s: Settings) -> CurrentUser:
    base = (s.keycloak_internal_url or s.keycloak_url).rstrip("/")
    try:
        key = _jwks_client(f"{base}/realms/{s.keycloak_realm}/protocol/openid-connect/certs").get_signing_key_from_jwt(token)
        claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=s.keycloak_client_id,
                            issuer=keycloak_issuer(s), options={"require": ["sub", "exp", "iss", "aud"]})
    except jwt.PyJWKClientConnectionError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Keycloak is unreachable")
    except jwt.PyJWTError:
        raise _unauthorized()
    user_id = claims.get("preferred_username")
    if not user_id:
        raise _unauthorized("Token has no preferred_username")
    name = claims.get("name") or user_id
    # First login creates the users row; later logins refresh name/email and last_login_at.
    with get_conn() as conn:
        row = conn.execute(
            """INSERT INTO users (user_id, display_name, email, auth_source, last_login_at, modified_by)
               VALUES (%(u)s, %(n)s, %(e)s, 'keycloak', now(), 'system')
               ON CONFLICT (user_id) DO UPDATE
                 SET display_name = EXCLUDED.display_name, email = EXCLUDED.email,
                     auth_source = 'keycloak', last_login_at = now(), modified_by = 'system'
               RETURNING user_id, display_name, email, is_active""",
            {"u": user_id, "n": name, "e": claims.get("email")},
        ).fetchone()
    if not row["is_active"]:
        raise _unauthorized("User is disabled")
    return CurrentUser(row["user_id"], row["display_name"], row["email"])


# ------------------------------------------------------------------ dependency
_bearer = HTTPBearer(auto_error=False)


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> CurrentUser:
    if creds is None:
        raise _unauthorized("Not signed in")
    s = get_settings()
    if s.auth_provider == "keycloak":
        return _authenticate_keycloak(creds.credentials, s)
    return _authenticate_local(creds.credentials, s)
