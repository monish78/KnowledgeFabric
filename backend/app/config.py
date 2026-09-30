from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    postgres_host: str = "localhost"
    postgres_port: int = 5433
    postgres_db: str = "graphbase"
    postgres_user: str = "postgres"
    postgres_password: str = ""

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""
    neo4j_mode: str = "single"  # single | multi

    chroma_host: str = "localhost"
    chroma_port: int = 8001

    llm_provider: str = "ollama"  # ollama | azure
    ollama_base_url: str = "http://localhost:11434"
    ollama_chat_model: str = "qwen2.5:7b-instruct"
    ollama_embed_model: str = "nomic-embed-text"
    ollama_num_ctx: int = 8192
    llm_timeout_seconds: int = 600

    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_api_version: str = "2024-10-21"
    azure_openai_chat_deployment: str = "gpt-4.1"
    azure_openai_embed_deployment: str = "text-embedding-3-large"

    auth_provider: str = "local"  # local | keycloak
    # Encrypts the Keycloak tokens stored in sessions. JWT_SECRET is accepted as the old name.
    secret_key: str = Field("", validation_alias=AliasChoices("SECRET_KEY", "JWT_SECRET"))
    session_idle_minutes: int = 60
    session_max_hours: int = 12
    session_cookie_name: str = "graphbase_session"
    session_cookie_secure: bool = False  # set true when the app is served over https
    app_url: str = ""  # browser-facing URL of the app, e.g. http://graphbase.example:5173
    keycloak_url: str = ""  # browser-facing; token issuer is {keycloak_url}/realms/{realm}
    keycloak_internal_url: str = ""  # where the backend fetches signing keys; defaults to keycloak_url
    keycloak_realm: str = ""
    keycloak_client_id: str = ""
    keycloak_client_secret: str = ""

    upload_dir: str = "/uploads"

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
