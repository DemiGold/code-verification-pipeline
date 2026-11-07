"""Application settings, loaded from .env on import.

Required fields (database_url, validator_token, github_webhook_secret) have
no defaults on purpose — a misconfigured deploy should crash at startup rather
than boot with empty strings and fail on the first request.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # extra="ignore" lets .env carry future-phase keys (stripe, openai, sentry)
    # without forcing them into this class just to be valid.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    app_base_url: str = "http://localhost:8000"

    # Unused at runtime right now — kept so .env doesn't reshape when the
    # frontend picks up Supabase auth.
    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str

    # Direct Postgres URL, not the Supabase pooler. We handle pooling in
    # app/core/database.py.
    database_url: str

    # Celery broker + result backend. Same URL for both.
    redis_url: str

    github_token: str

    # Bot's own pushes get rejected by the webhook handler, otherwise the
    # audit-report commit would retrigger the pipeline.
    bot_github_username: str = ""

    # No defaults here on purpose. An empty validator_token would let anyone
    # mark submissions as VERIFIED. An empty webhook_secret would accept
    # unsigned payloads from anywhere.
    validator_token: str
    github_webhook_secret: str

    resend_api_key: str
    resend_from_email: str

    # Uncomment when the respective phases land.
    # stripe_secret_key: str
    # stripe_webhook_secret: str
    # openai_api_key: str
    # sentry_dsn: str


settings = Settings()