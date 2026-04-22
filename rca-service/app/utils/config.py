from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
import os


class Settings(BaseSettings):
    """Configuration de l'application"""
    
    # Service
    APP_NAME: str = "RCA Alert Service"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"

    # Redis
    REDIS_HOST: str = os.getenv("REDIS_HOST", "localhost")
    REDIS_PORT: int = int(os.getenv("REDIS_PORT", "6379"))
    REDIS_DB: int = int(os.getenv("REDIS_DB", "0"))
    
    # Prometheus
    PROMETHEUS_URL: str = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
    
    # Loki
    LOKI_URL: str = os.getenv("LOKI_URL", "http://localhost:3100")
    
    # Jaeger
    JAEGER_URL: str = os.getenv("JAEGER_URL", "http://localhost:16686")
    
    # Ollama (LLM Local Gratuit)
    OLLAMA_URL: str = os.getenv("OLLAMA_URL", os.getenv("OLLAMA_HOST", "http://localhost:11434"))
    OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen2.5:0.5b")  # alternatives: llama3.2:1b, gemma3:270m
    LLM_TIMEOUT: int = int(os.getenv("LLM_TIMEOUT", "180"))
    OLLAMA_NUM_PREDICT: int = int(os.getenv("OLLAMA_NUM_PREDICT", "160"))
    OLLAMA_TEMPERATURE: float = float(os.getenv("OLLAMA_TEMPERATURE", "0.1"))
    OLLAMA_MAX_CONCURRENCY: int = int(os.getenv("OLLAMA_MAX_CONCURRENCY", "1"))
    OLLAMA_RETRY_ATTEMPTS: int = int(os.getenv("OLLAMA_RETRY_ATTEMPTS", "2"))
    OLLAMA_CIRCUIT_FAIL_THRESHOLD: int = int(os.getenv("OLLAMA_CIRCUIT_FAIL_THRESHOLD", "3"))
    OLLAMA_CIRCUIT_COOLDOWN_SECONDS: int = int(os.getenv("OLLAMA_CIRCUIT_COOLDOWN_SECONDS", "120"))
    
    # SMTP
    SMTP_HOST: str = os.getenv("SMTP_HOST", "localhost")
    SMTP_PORT: int = Field(default=587, env="SMTP_PORT")

    @field_validator('SMTP_PORT', mode='before')
    @classmethod
    def validate_smtp_port(cls, v):
        if v == '' or v is None:
            return 587
        return v
    
    SMTP_USER: str = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
    SMTP_FROM: str = os.getenv("SMTP_FROM", "rca@monitoring.local")
    NOTIFICATION_EMAIL: str = os.getenv("NOTIFICATION_EMAIL", "oncall@monitoring.local")
    SMTP_USE_TLS: bool = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
    SMTP_REQUIRE_AUTH: bool = os.getenv("SMTP_REQUIRE_AUTH", "false").lower() == "true"
    
    # Alert Processing
    DEDUP_WINDOW_SECONDS: int = 300  # 5 minutes
    ALERT_PROCESSING_TIMEOUT: int = int(os.getenv("ALERT_PROCESSING_TIMEOUT", "300"))
    SOURCE_TIMEOUT_SECONDS: int = int(os.getenv("SOURCE_TIMEOUT_SECONDS", "8"))
    CONTEXT_LOOKBACK_MINUTES: int = int(os.getenv("CONTEXT_LOOKBACK_MINUTES", "15"))
    DB_PATH: str = os.getenv("DB_PATH", "rca_history.db")

    # Layer 2 (Triage): Drain3 correlation & noise suppression
    DRAIN3_CORRELATION_WINDOW_SECONDS: int = int(os.getenv("DRAIN3_CORRELATION_WINDOW_SECONDS", "180"))
    DRAIN3_EVENT_TTL_SECONDS: int = int(os.getenv("DRAIN3_EVENT_TTL_SECONDS", "900"))
    DRAIN3_MAX_TEMPLATES: int = int(os.getenv("DRAIN3_MAX_TEMPLATES", "5"))
    TRIAGE_HISTORY_LOOKBACK_MINUTES: int = int(os.getenv("TRIAGE_HISTORY_LOOKBACK_MINUTES", "15"))
    TRIAGE_NOISE_PATTERNS: str = os.getenv("TRIAGE_NOISE_PATTERNS", "")
    
    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
    )


settings = Settings()
