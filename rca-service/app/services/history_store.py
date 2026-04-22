"""
Stockage SQLite de l'historique RCA.
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.models.alert import AlertPayload, RCADecision
from app.utils.config import settings


class RCAHistoryStore:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def initialize(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rca_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    alert_id TEXT NOT NULL,
                    alert_name TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    affected_service TEXT,
                    triage_decision TEXT NOT NULL,
                    reason TEXT,
                    rca TEXT,
                    anomaly_summary TEXT,
                    suggested_actions TEXT,
                    evidence TEXT,
                    confidence REAL,
                    raw_llm_response TEXT,
                    pipeline_stage TEXT,
                    timed_out INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.commit()

    def store_decision(
        self,
        alert: AlertPayload,
        decision: RCADecision,
        raw_llm_response: str = "",
        pipeline_stage: str = "completed",
        timed_out: bool = False,
    ) -> None:
        with sqlite3.connect(self.db_path) as conn:
            affected_service = alert.tags.get("service") or alert.tags.get("service_name") or ""
            conn.execute(
                """
                INSERT INTO rca_history (
                    created_at, alert_id, alert_name, severity, affected_service,
                    triage_decision, reason, rca, anomaly_summary, suggested_actions,
                    evidence, confidence, raw_llm_response, pipeline_stage, timed_out
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    alert.alert_id,
                    alert.alert_name,
                    alert.severity,
                    affected_service,
                    decision.decision,
                    decision.reason,
                    decision.rca,
                    decision.anomaly_summary,
                    json.dumps(decision.suggested_actions),
                    json.dumps(decision.evidence),
                    decision.confidence,
                    raw_llm_response,
                    pipeline_stage,
                    1 if timed_out else 0,
                ),
            )
            conn.commit()

    def store_timeout_passthrough(self, alert: AlertPayload, pipeline_stage: str) -> None:
        timeout_decision = RCADecision(
            decision="ESCALATE",
            severity=alert.severity if alert.severity in {"critical", "high", "medium", "low"} else "high",
            reason="Pipeline timeout",
            rca="RCA non disponible: timeout global pipeline",
            anomaly_summary="timeout_passthrough",
            suggested_actions=["Manual investigation required"],
            evidence=[f"pipeline_stage={pipeline_stage}"],
            confidence=0.0,
        )
        self.store_decision(
            alert=alert,
            decision=timeout_decision,
            raw_llm_response="",
            pipeline_stage=pipeline_stage,
            timed_out=True,
        )

    def get_last_decisions(self, limit: int = 50) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT created_at, alert_id, alert_name, severity, affected_service,
                       triage_decision, reason, rca, anomaly_summary, suggested_actions,
                       evidence, confidence, pipeline_stage, timed_out
                FROM rca_history
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_recent_decision_for_alert(
        self,
        *,
        alert_name: str,
        affected_service: str,
        lookback_minutes: int,
    ) -> dict[str, Any] | None:
        """
        Used by triage to suppress noisy repeats without calling the LLM.
        """
        since = datetime.now(timezone.utc).timestamp() - (lookback_minutes * 60)
        since_iso = datetime.fromtimestamp(since, tz=timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT created_at, triage_decision, reason, confidence, pipeline_stage
                FROM rca_history
                WHERE alert_name = ?
                  AND affected_service = ?
                  AND created_at >= ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (alert_name, affected_service, since_iso),
            ).fetchone()
            return dict(row) if row else None


history_store = RCAHistoryStore(settings.DB_PATH)
