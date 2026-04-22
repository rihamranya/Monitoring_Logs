from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, Literal
from datetime import datetime


class AlertPayload(BaseModel):
    """Modèle pour les alertes entrantes (ex: Grafana, Prometheus)"""
    alert_id: str = Field(..., description="ID unique de l'alerte")
    alert_name: str = Field(..., description="Nom de l'alerte")
    severity: str = Field(..., description="Sévérité: critical, warning, info")
    hostname: Optional[str] = Field(None, description="Host affecté")
    description: str = Field(..., description="Description de l'alerte")
    timestamp: Optional[datetime] = Field(default_factory=datetime.utcnow)
    source: Optional[str] = Field(None, description="Source de l'alerte: prometheus, grafana, etc")
    tags: Dict[str, str] = Field(default_factory=dict)
    
    class Config:
        json_schema_extra = {
            "example": {
                "alert_id": "abc123",
                "alert_name": "HighCPUUsage",
                "severity": "critical",
                "hostname": "prod-server-01",
                "description": "CPU usage exceeded 90%",
                "source": "prometheus",
                "tags": {"service": "backend", "env": "prod"}
            }
        }


class GrafanaAlertItem(BaseModel):
    status: str
    labels: Dict[str, str] = Field(default_factory=dict)
    annotations: Dict[str, str] = Field(default_factory=dict)
    startsAt: Optional[datetime] = None
    endsAt: Optional[datetime] = None
    fingerprint: Optional[str] = None


class GrafanaWebhook(BaseModel):
    receiver: Optional[str] = None
    status: str
    title: Optional[str] = None
    message: Optional[str] = None
    groupKey: Optional[str] = None
    alerts: list[GrafanaAlertItem] = Field(default_factory=list, min_length=1)


class Drain3Webhook(BaseModel):
    cluster_id: str
    template: str
    source: Optional[str] = "drain3"
    timestamp: Optional[datetime] = Field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class MetricsContext(BaseModel):
    """Contexte des métriques Prometheus"""
    query: str
    values: list[Dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class LogsContext(BaseModel):
    """Contexte des logs Loki"""
    logs: list[Dict[str, Any]] = Field(default_factory=list)
    total_entries: int = 0
    notes: list[str] = Field(default_factory=list)


class TracesContext(BaseModel):
    """Contexte des traces Jaeger"""
    trace_id: Optional[str] = None
    spans: list[Dict[str, Any]] = Field(default_factory=list)
    error_rate: Optional[float] = None
    notes: list[str] = Field(default_factory=list)


class AlertContext(BaseModel):
    """Contexte global collecté pour l'alerte"""
    alert: AlertPayload
    metrics: Optional[MetricsContext] = None
    logs: Optional[LogsContext] = None
    traces: Optional[TracesContext] = None
    drain3_templates: list[str] = Field(default_factory=list)
    source_latencies_ms: Dict[str, int] = Field(default_factory=dict)
    context_notes: list[str] = Field(default_factory=list)
    collected_at: datetime = Field(default_factory=datetime.utcnow)


class LLMDecision(BaseModel):
    decision: Literal["ESCALATE", "DISMISS"]
    severity: Literal["critical", "high", "medium", "low"]
    reason: str
    rca: str
    anomaly_summary: str
    suggested_actions: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class RCADecision(LLMDecision):
    """Alias métier conservé pour compatibilité pipeline."""
