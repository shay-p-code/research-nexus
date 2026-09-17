from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./nexus.db"
    public_url: str = "http://localhost:8000"
    api_token: str = Field(min_length=32)
    worker_token: str = Field(min_length=32)
    owner_password: str = Field(min_length=32)
    oauth_client_id: str = "research-nexus-chatgpt"
    oauth_client_secret: str = Field(min_length=32)
    oauth_redirect_uris: list[str] = ["https://chatgpt.com/connector_platform_oauth_redirect"]
    openai_api_key: str = ""
    research_model: str = "gpt-5-mini"
    librarian_model: str = "gpt-5-mini"
    embedding_model: str = "text-embedding-3-small"
    max_tool_calls: int = Field(default=3, ge=1, le=10)
    max_output_tokens: int = Field(default=6000, ge=1000, le=16000)
    lease_seconds: int = Field(default=900, ge=600, le=3600)

    @model_validator(mode="after")
    def validate_settings(self):
        self.public_url = self.public_url.rstrip("/")
        if not self.public_url.startswith("https://") and self.public_url != "http://localhost:8000":
            raise ValueError("PUBLIC_URL must use HTTPS, except http://localhost:8000 for local work")
        secrets = [self.api_token, self.worker_token, self.owner_password, self.oauth_client_secret]
        if len(set(secrets)) != 4 or any(s.startswith("replace-") for s in secrets):
            raise ValueError("Generate four different secrets with scripts/init_env.py")
        return self
