from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "AI Question-Answering API"
    debug: bool = False
    database_url: str = "postgresql+psycopg2://qa_user:qa_pass@localhost:5432/qa_db"

    jwt_secret: str  # no default — must be set in .env, this is a real secret
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    redis_url: str = "redis://localhost:6379/0"
    rate_limit_per_minute: int = 5
    cache_ttl_seconds: int = 300

    llm_provider: str = "mock"  # "mock" | "gemini"
    gemini_api_key: str = ""
    llm_primary_model: str = "gemini-1.5-flash"
    llm_timeout_seconds: int = 10
    llm_max_retries: int = 3

settings = Settings()