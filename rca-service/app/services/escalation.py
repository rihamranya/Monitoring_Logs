"""
Service d'escalade par email via SMTP
"""
import asyncio
import smtplib
import logging
from collections import Counter
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

from app.models.alert import AlertContext, RCADecision
from app.utils.config import settings

logger = logging.getLogger(__name__)


def _metrics_value_preview(context: AlertContext) -> str:
    """Safely render a short metrics preview for email."""
    if not context.metrics or not context.metrics.values:
        return "N/A"
    first = context.metrics.values[0]
    if isinstance(first, dict):
        value = first.get("value")
        if value is not None:
            return str(value)
        timestamps_values = first.get("timestamps_values")
        if isinstance(timestamps_values, list) and timestamps_values:
            last_item = timestamps_values[-1]
            if isinstance(last_item, list) and len(last_item) > 1:
                return str(last_item[1])
    return "N/A"


def _top_log_issues(context: AlertContext, top_n: int = 3) -> list[tuple[str, int]]:
    """Build a short error summary instead of dumping raw logs."""
    if not context.logs or not context.logs.logs:
        return []
    counter: Counter[str] = Counter()
    for entry in context.logs.logs:
        line = str(entry.get("line", "")).strip()
        if not line:
            continue
        # Keep first meaningful fragment for grouping recurring messages.
        normalized = " ".join(line.split())
        key = normalized[:140]
        counter[key] += 1
    return counter.most_common(top_n)


def _slowest_span_summary(context: AlertContext) -> str:
    """Return the most expensive span as compact trace insight."""
    if not context.traces or not context.traces.spans:
        return "N/A"
    spans = context.traces.spans
    slowest = max(spans, key=lambda s: int(s.get("duration_us") or 0))
    name = slowest.get("span_name") or "unknown-span"
    duration_us = int(slowest.get("duration_us") or 0)
    duration_ms = duration_us / 1000.0
    return f"{name} ({duration_ms:.2f} ms)"


def _quick_links(context: AlertContext) -> dict[str, str]:
    tags = context.alert.tags or {}
    links: dict[str, str] = {}
    if tags.get("grafana_source_url"):
        links["View Alert"] = tags["grafana_source_url"]
    if tags.get("grafana_dashboard_url"):
        links["View Dashboard"] = tags["grafana_dashboard_url"]
    if tags.get("grafana_panel_url"):
        links["View Panel"] = tags["grafana_panel_url"]
    if tags.get("grafana_silence_url"):
        links["Create Silence"] = tags["grafana_silence_url"]
    return links


class EscalationService:
    """Gère l'escalade des alertes par email"""
    
    def __init__(self):
        self.smtp_host = settings.SMTP_HOST
        self.smtp_port = settings.SMTP_PORT
        self.smtp_user = settings.SMTP_USER
        self.smtp_password = settings.SMTP_PASSWORD
        self.smtp_from = settings.SMTP_FROM
        self.smtp_use_tls = settings.SMTP_USE_TLS
        self.smtp_require_auth = settings.SMTP_REQUIRE_AUTH
    
    async def escalate(
        self,
        context: AlertContext,
        rca_decision: RCADecision,
        recipients: list[str]
    ) -> bool:
        """
        SCRUM-79: Escalader l'alerte par email avec contexte RCA
        """
        logger.info(f"Escalating alert {context.alert.alert_id} to {len(recipients)} recipients")
        
        try:
            subject = self._build_subject(context, rca_decision)
            body = self._build_email_body(context, rca_decision)
            
            # Exécuter l'envoi dans un executor pour ne pas bloquer
            loop = asyncio.get_event_loop()
            success = await loop.run_in_executor(
                None,
                self._send_email,
                recipients,
                subject,
                body
            )
            
            if success:
                logger.info(f"Alert escalated successfully to {recipients}")
            else:
                logger.error("Failed to escalate alert")
            
            return success
        
        except Exception as e:
            logger.error(f"Error escalating alert: {e}")
            return False
    
    def _build_subject(self, context: AlertContext, decision: RCADecision) -> str:
        """Construire le sujet de l'email"""
        severity = decision.severity.upper()
        affected_service = context.alert.tags.get("service", context.alert.hostname or "unknown-service")
        return f"[ALERT] {severity}: {context.alert.alert_name} — {affected_service}"
    
    def _build_email_body(self, context: AlertContext, decision: RCADecision) -> str:
        """Construire le corps de l'email en HTML"""
        alert = context.alert
        service_name = alert.tags.get("service") or alert.tags.get("service_name") or alert.hostname or "unknown-service"
        environment = alert.tags.get("deployment_environment") or alert.tags.get("env") or "N/A"
        top_issues = _top_log_issues(context)
        slow_span = _slowest_span_summary(context)
        quick_links = _quick_links(context)
        links_html = "".join(
            f'<li><a href="{url}" target="_blank" rel="noopener noreferrer">{label}</a></li>'
            for label, url in quick_links.items()
        ) or "<li>Aucun lien disponible</li>"
        
        html_body = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <style>
        :root {{
            --bg: #ffffff;
            --card: #f7f9ff;
            --muted: #5b6b8b;
            --text: #0b1b3a;
            --border: rgba(11, 27, 58, .12);
            --danger: #d64550;
            --warn: #c27c0e;
            --info: #1f6feb;
            --ok: #0f766e;
        }}
        body {{
            margin: 0;
            padding: 0;
            background: var(--bg);
            color: var(--text);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
            line-height: 1.45;
        }}
        a {{ color: #0b5bd3; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        .container {{ max-width: 860px; margin: 0 auto; padding: 24px 16px; }}
        .hero {{
            background: radial-gradient(1200px 600px at 10% -10%, rgba(31,111,235,.22), transparent 55%),
                        radial-gradient(900px 500px at 90% 0%, rgba(214,69,80,.16), transparent 58%),
                        linear-gradient(180deg, rgba(11,27,58,.04), rgba(11,27,58,.02));
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 18px 18px 16px;
        }}
        .hero-top {{ display: flex; gap: 12px; align-items: center; justify-content: space-between; flex-wrap: wrap; }}
        .title {{ font-size: 22px; margin: 0; }}
        .subtitle {{ margin: 6px 0 0; color: var(--muted); font-size: 13px; }}
        .pill {{
            display: inline-block;
            padding: 6px 10px;
            border-radius: 999px;
            border: 1px solid var(--border);
            font-weight: 700;
            letter-spacing: .2px;
            font-size: 12px;
            background: rgba(11,27,58,.03);
        }}
        .pill-danger {{ border-color: rgba(214,69,80,.35); color: #7a1f28; background: rgba(214,69,80,.08); }}
        .pill-warn {{ border-color: rgba(194,124,14,.30); color: #6a3f07; background: rgba(194,124,14,.10); }}
        .pill-info {{ border-color: rgba(31,111,235,.30); color: #0b2e7a; background: rgba(31,111,235,.08); }}
        .grid {{
            display: grid;
            grid-template-columns: 1fr;
            gap: 14px;
            margin-top: 14px;
        }}
        .card {{
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 14px;
            padding: 14px 14px 10px;
        }}
        .card h2 {{ font-size: 16px; margin: 0 0 10px; }}
        .kvs {{ width: 100%; border-collapse: collapse; }}
        .kvs td {{ padding: 8px 0; border-bottom: 1px solid rgba(11,27,58,.08); vertical-align: top; }}
        .kvs tr:last-child td {{ border-bottom: none; }}
        .k {{ width: 180px; color: var(--muted); font-weight: 700; }}
        .v {{ color: var(--text); word-break: break-word; }}
        .badge {{
            display: inline-flex;
            gap: 8px;
            align-items: center;
            padding: 10px 12px;
            border-radius: 12px;
            border: 1px solid var(--border);
            font-weight: 800;
        }}
        .badge-escalate {{ background: rgba(214,69,80,.10); border-color: rgba(214,69,80,.28); }}
        .badge-dismiss {{ background: rgba(15,118,110,.09); border-color: rgba(15,118,110,.25); }}
        .badge small {{ display: block; font-weight: 700; color: var(--muted); margin-top: 2px; }}
        .mono {{
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
            font-size: 12px;
            white-space: pre-wrap;
            word-break: break-word;
            background: rgba(11,27,58,.03);
            border: 1px dashed rgba(11,27,58,.18);
            padding: 10px;
            border-radius: 10px;
            margin: 8px 0 0;
        }}
        ul {{ margin: 8px 0 0; padding-left: 18px; }}
        .footer {{ text-align: center; margin-top: 18px; color: var(--muted); font-size: 12px; }}
        @media (min-width: 900px) {{
            .grid {{
                grid-template-columns: 1.15fr .85fr;
                align-items: start;
            }}
            .span2 {{ grid-column: span 2; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="hero">
            <div class="hero-top">
                <div>
                    <h1 class="title">🚨 Alerte détectée et analysée</h1>
                    <p class="subtitle">Un système d'analyse RCA intelligent a traité cette alerte</p>
                </div>
                <div>
                    <span class="pill pill-danger">STATUS: {alert.severity.upper()}</span>
                </div>
            </div>
        </div>
        
        <div class="grid">
            <div class="card">
                <h2>📋 Informations de l'alerte</h2>
                <table class="kvs">
                    <tr><td class="k">Nom</td><td class="v">{alert.alert_name}</td></tr>
                    <tr><td class="k">Sévérité</td><td class="v">{alert.severity.upper()}</td></tr>
                    <tr><td class="k">Hostname</td><td class="v">{alert.hostname or 'N/A'}</td></tr>
                    <tr><td class="k">Service</td><td class="v">{service_name}</td></tr>
                    <tr><td class="k">Environment</td><td class="v">{environment}</td></tr>
                    <tr><td class="k">Source</td><td class="v">{alert.source or 'Unknown'}</td></tr>
                    <tr><td class="k">Timestamp</td><td class="v">{alert.timestamp}</td></tr>
                </table>
                <div class="mono">{alert.description}</div>
            </div>
        
            <div class="card">
                <h2>🤖 Analyse RCA</h2>
                <div class="badge {'badge-escalate' if decision.decision == 'ESCALATE' else 'badge-dismiss'}">
                    <div>
                        Décision: {decision.decision}
                        <small>Confiance: {decision.confidence * 100:.1f}%</small>
                    </div>
                </div>
                <div class="mono">Reason: {decision.reason}

RCA: {decision.rca}

Anomaly: {decision.anomaly_summary}

Suggested actions: {", ".join(decision.suggested_actions)}

Evidence: {", ".join(decision.evidence)}</div>
            </div>
        </div>
        
        <div class="card span2">
            <h2>📊 Contexte Collecté</h2>
            
            {f'''
            <h3>Métriques Prometheus</h3>
            <table class="kvs">
                <tr><td class="k">Query</td><td class="v">{context.metrics.query}</td></tr>
                <tr><td class="k">Valeur</td><td class="v">{_metrics_value_preview(context)}</td></tr>
            </table>
            ''' if context.metrics else '<p>Aucune métrique collectée</p>'}
            
            {f'''
            <h3>Logs Loki</h3>
            <p><span class="pill pill-info">Total: {context.logs.total_entries} entrées</span> <span class="pill pill-info">{len(context.logs.logs) if context.logs.logs else 0} logs récupérés</span></p>
            <p><b>Top erreurs</b></p>
            <ul>
                {"".join(f"<li>{msg} ({count}x)</li>" for msg, count in top_issues) if top_issues else "<li>Aucune erreur significative détectée</li>"}
            </ul>
            ''' if context.logs else '<p>Aucun log collecté</p>'}
            
            {f'''
            <h3>Traces Jaeger</h3>
            <table class="kvs">
                <tr><td class="k">Trace ID</td><td class="v">{context.traces.trace_id}</td></tr>
                <tr><td class="k">Spans</td><td class="v">{len(context.traces.spans)}</td></tr>
                <tr><td class="k">Taux d'erreur</td><td class="v">{context.traces.error_rate * 100:.1f}%</td></tr>
                <tr><td class="k">Slow span</td><td class="v">{slow_span}</td></tr>
            </table>
            ''' if context.traces else '<p>Aucune trace collectée</p>'}
        </div>

        <div class="card">
            <h2>🔗 Liens rapides</h2>
            <ul>
                {links_html}
            </ul>
        </div>
        
        <div class="footer">
            <p>Message automatique généré par le système RCA Monitoring</p>
        </div>
    </div>
</body>
</html>
"""
        return html_body

    async def send_timeout_passthrough(self, alert: AlertContext, recipients: list[str]) -> bool:
        timeout_decision = RCADecision(
            decision="ESCALATE",
            severity="high",
            reason="LLM pipeline timeout",
            rca="Raw alert passthrough due to timeout",
            anomaly_summary="timeout_passthrough",
            suggested_actions=["Manual triage immediately"],
            evidence=[f"alert_name={alert.alert.alert_name}"],
            confidence=0.0,
        )
        subject = f"[ALERT] TIMEOUT: {alert.alert.alert_name}"
        body = self._build_email_body(alert, timeout_decision)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._send_email, recipients, subject, body)
    
    def _send_email(self, recipients: list[str], subject: str, body: str) -> bool:
        """
        Envoyer l'email via SMTP (exécuté dans un executor)
        """
        try:
            if not recipients:
                logger.warning("No recipients configured for escalation email")
                return False

            # Créer le message
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = self.smtp_from
            msg["To"] = ", ".join(recipients)
            
            # Attacher le body HTML
            msg.attach(MIMEText(body, "html"))
            
            # Envoyer via SMTP
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                if self.smtp_use_tls:
                    server.starttls()
                if self.smtp_require_auth or (self.smtp_user and self.smtp_password):
                    server.login(self.smtp_user, self.smtp_password)
                server.sendmail(self.smtp_from, recipients, msg.as_string())
            
            logger.info(f"Email sent successfully to {recipients}")
            return True
        
        except Exception as e:
            logger.error(f"Error sending email: {e}")
            return False


# Instance globale
escalation_service = EscalationService()
