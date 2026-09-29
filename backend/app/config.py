from functools import lru_cache

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

    auth_provider: str = "local"  # local | keycloak
    jwt_secret: str = ""
    jwt_expire_minutes: int = 480
    keycloak_url: str = ""
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
