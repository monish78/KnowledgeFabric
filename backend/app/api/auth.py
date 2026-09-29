from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import CurrentUser, current_user, keycloak_issuer, local_login
from app.config import get_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=72)  # bcrypt limit


@router.get("/config")
def auth_config():
    """Tells the frontend how to sign in: our own form, or a redirect to Keycloak."""
    s = get_settings()
    if s.auth_provider == "keycloak":
        return {"provider": "keycloak", "url": s.keycloak_url, "realm": s.keycloak_realm,
                "client_id": s.keycloak_client_id, "issuer": keycloak_issuer(s)}
    return {"provider": "local"}


@router.post("/login")
def login(body: LoginRequest):
    s = get_settings()
    if s.auth_provider != "local":
        raise HTTPException(400, "Password login is disabled; sign in through Keycloak")
    token, user = local_login(body.user_id.strip(), body.password)
    return {"access_token": token, "token_type": "bearer", "expires_in": s.jwt_expire_minutes * 60,
            "user": user.__dict__}


@router.get("/me")
def me(user: CurrentUser = Depends(current_user)):
    return user.__dict__
