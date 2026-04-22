"""
Service LLM pour l'analyse RCA intelligente des alertes
Utilise Ollama (LLM local gratuit) pour l'analyse
"""
import asyncio
import logging
import json
import time
import httpx
import re

from app.models.alert import AlertContext, RCADecision
from app.utils.config import settings

logger = logging.getLogger(__name__)


class LLMRCAAnalyzer:
    """Utilise Ollama (LLM local gratuit) pour analyser les alertes et prendre des décisions"""
    
    def __init__(self):
        self.ollama_host = settings.OLLAMA_URL
        self.model = settings.OLLAMA_MODEL
        self.timeout = settings.LLM_TIMEOUT
        self.num_predict = settings.OLLAMA_NUM_PREDICT
        self.temperature = settings.OLLAMA_TEMPERATURE
        self.max_concurrency = max(1, settings.OLLAMA_MAX_CONCURRENCY)
        self.retry_attempts = max(1, settings.OLLAMA_RETRY_ATTEMPTS)
        self.circuit_fail_threshold = max(1, settings.OLLAMA_CIRCUIT_FAIL_THRESHOLD)
        self.circuit_cooldown_seconds = max(1, settings.OLLAMA_CIRCUIT_COOLDOWN_SECONDS)
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._state_lock = asyncio.Lock()
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        logger.info(
            "LLM analyzer configured with Ollama host=%s model=%s",
            self.ollama_host,
            self.model,
        )

    async def _is_circuit_open(self) -> bool:
        async with self._state_lock:
            return time.monotonic() < self._circuit_open_until

    async def _record_success(self) -> None:
        async with self._state_lock:
            self._consecutive_failures = 0
            self._circuit_open_until = 0.0

    async def _record_failure(self) -> None:
        async with self._state_lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.circuit_fail_threshold:
                self._circuit_open_until = time.monotonic() + float(self.circuit_cooldown_seconds)
                logger.warning(
                    "LLM circuit opened for %ss after %s consecutive failures",
                    self.circuit_cooldown_seconds,
                    self._consecutive_failures,
                )

    def _compact_context(self, context: AlertContext) -> dict:
        """Build a compact context payload to keep prompts short and stable."""
        metrics_summary = {}
        if context.metrics:
            values = context.metrics.values or []
            metrics_summary = {
                "query": context.metrics.query,
                "points_count": len(values),
                "notes": (context.metrics.notes or [])[:3],
                "latest_point": values[-1] if values else None,
            }

        logs_summary = {}
        if context.logs:
            logs = context.logs.logs or []
            logs_summary = {
                "total_entries": context.logs.total_entries,
                "sample_logs": logs[:3],
                "notes": (context.logs.notes or [])[:3],
            }

        traces_summary = {}
        if context.traces:
            traces_summary = {
                "trace_id": context.traces.trace_id,
                "spans_count": len(context.traces.spans or []),
                "error_rate": context.traces.error_rate,
                "notes": (context.traces.notes or [])[:3],
            }

        return {
            "metrics": metrics_summary,
            "logs": logs_summary,
            "traces": traces_summary,
            "drain3_templates": (context.drain3_templates or [])[:5],
            "context_notes": (context.context_notes or [])[:3],
            "source_latencies_ms": context.source_latencies_ms or {},
        }
    
    def _build_prompt(self, context: AlertContext, strict: bool = False) -> str:
        """
        Construire le prompt pour Ollama
        """
        alert = context.alert
        raw_description = alert.description or ""
        # Keep the alert description compact to avoid very long prompts and LLM latency spikes.
        compact_description = re.sub(r"\s+", " ", raw_description).strip()
        if len(compact_description) > 600:
            compact_description = f"{compact_description[:600]}...(truncated)"
        compact_context = self._compact_context(context)
        context_json = json.dumps(compact_context, ensure_ascii=True, separators=(",", ":"))
        
        prompt = f"""You are an SRE triage assistant.
Analyze this alert and decide ESCALATE or DISMISS based on metrics/logs/traces.

ALERT:
- name: {alert.alert_name}
- severity: {alert.severity}
- host: {alert.hostname}
- source: {alert.source}
- description: {compact_description}

CONTEXT_JSON:
{context_json}

Return JSON only with this exact schema:
{{
  "decision": "ESCALATE|DISMISS",
  "severity": "critical|high|medium|low",
  "reason": "short reason",
  "rca": "root cause analysis",
  "anomaly_summary": "concise anomaly summary",
  "suggested_actions": ["action1", "action2"],
  "evidence": ["fact1", "fact2"],
  "confidence": 0.0
}}
{ "STRICT: valid JSON only, no markdown, no extra keys." if strict else "" }
"""
        
        return prompt

    def _fallback_evidence(self, context: AlertContext) -> list[str]:
        evidence: list[str] = []
        if context.logs and context.logs.notes:
            evidence.extend([note for note in context.logs.notes[:3] if note])
        if context.traces and context.traces.error_rate is not None:
            evidence.append(f"trace_error_rate={context.traces.error_rate:.2f}")
        if context.metrics and context.metrics.query:
            evidence.append(f"metric_query={context.metrics.query}")
        if not evidence:
            evidence.append("context_collected_with_no_explicit_signals")
        return evidence[:4]

    def _normalize_decision(self, decision: RCADecision, context: AlertContext) -> RCADecision:
        min_confidence = 0.35
        decision.confidence = max(min_confidence, min(1.0, float(decision.confidence)))
        if not decision.evidence:
            decision.evidence = self._fallback_evidence(context)
        if not decision.suggested_actions:
            decision.suggested_actions = ["Investigate logs and metrics for this service"]
        return decision
    
    async def analyze(self, context: AlertContext) -> tuple[RCADecision, str, int]:
        """
        Analyser l'alerte avec Ollama
        """
        logger.info(f"Starting Ollama analysis for alert: {context.alert.alert_name}")
        start = time.perf_counter()
        
        try:
            if await self._is_circuit_open():
                duration_ms = int((time.perf_counter() - start) * 1000)
                logger.warning("Skipping LLM call: circuit breaker is open")
                return RCADecision(
                    decision="ESCALATE",
                    severity="high",
                    reason="Ollama circuit open",
                    rca="LLM temporarily bypassed after repeated failures",
                    anomaly_summary="llm_circuit_open",
                    suggested_actions=["Manual investigation required"],
                    evidence=["ollama_circuit_open"],
                    confidence=0.5,
                ), "", duration_ms

            prompt = self._build_prompt(context, strict=False)
            response = await self._call_ollama(prompt)
            logger.info("raw_llm_response=%s", response)
            try:
                decision = self._parse_response(response)
            except Exception:
                retry_prompt = self._build_prompt(context, strict=True)
                response = await self._call_ollama(
                    retry_prompt,
                    num_predict_override=min(self.num_predict * 2, 512),
                )
                logger.info("raw_llm_response_retry=%s", response)
                decision = self._parse_response(response)
            decision = self._normalize_decision(decision, context)

            await self._record_success()
            duration_ms = int((time.perf_counter() - start) * 1000)
            logger.info("llm_investigation_duration_ms=%s", duration_ms)
            logger.info(f"Ollama decision: {decision.decision} (confidence: {decision.confidence})")
            return decision, response, duration_ms
        
        except (asyncio.TimeoutError, httpx.ReadTimeout):
            await self._record_failure()
            logger.error("Ollama analysis timeout - falling back to ESCALATE")
            duration_ms = int((time.perf_counter() - start) * 1000)
            return RCADecision(
                decision="ESCALATE",
                severity="high",
                reason="Ollama timeout",
                rca="Unknown issue due to LLM timeout",
                anomaly_summary="timeout",
                suggested_actions=["Manual investigation required"],
                evidence=["ollama_timeout"],
                confidence=0.5,
            ), "", duration_ms
        
        except Exception as e:
            await self._record_failure()
            logger.error("Error in Ollama analysis: %r", e)
            duration_ms = int((time.perf_counter() - start) * 1000)
            return RCADecision(
                decision="ESCALATE",
                severity="high",
                reason="Ollama error",
                rca="Analysis failed",
                anomaly_summary="error",
                suggested_actions=["Manual investigation required"],
                evidence=[f"ollama_error={str(e)}"],
                confidence=0.5,
            ), "", duration_ms
    
    async def _call_ollama(self, prompt: str, num_predict_override: int | None = None) -> str:
        """
        Appeler Ollama via HTTP API
        """
        last_error = None
        target_num_predict = num_predict_override or self.num_predict
        for attempt in range(1, self.retry_attempts + 1):
            try:
                async with self._semaphore:
                    timeout = httpx.Timeout(connect=10.0, read=float(self.timeout), write=30.0, pool=30.0)
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        response = await client.post(
                            f"{self.ollama_host}/api/chat",
                            json={
                                "model": self.model,
                            "format": "json",
                                "messages": [
                                    {"role": "system", "content": "You are a strict JSON SRE assistant."},
                                    {"role": "user", "content": prompt},
                                ],
                                "stream": False,
                                "options": {
                                    "temperature": self.temperature,
                                    "num_predict": target_num_predict,
                                },
                            }
                        )

                    if response.status_code != 200:
                        raise RuntimeError(
                            f"Ollama HTTP {response.status_code}: {response.text[:500]}"
                        )

                    data = response.json()
                    content = data.get("message", {}).get("content", "")
                    if data.get("done_reason") == "length":
                        logger.warning(
                            "Ollama truncated output (done_reason=length, num_predict=%s), trying parse/retry path",
                            target_num_predict,
                        )
                    return content
            except (httpx.ReadTimeout, httpx.ConnectError) as e:
                last_error = e
                if attempt < self.retry_attempts:
                    backoff_s = float(attempt)
                    logger.warning(
                        "Ollama timeout/connectivity issue (attempt %s/%s), retry in %.1fs",
                        attempt,
                        self.retry_attempts,
                        backoff_s,
                    )
                    await asyncio.sleep(backoff_s)
                    continue
                logger.error("Ollama API error after retries: %r", e)
                raise
            except Exception as e:
                logger.error("Ollama API error: %r", e)
                raise

        if last_error is not None:
            raise last_error
        raise RuntimeError("Unexpected Ollama call failure")
    
    def _parse_response(self, response: str) -> RCADecision:
        """
        Parser la réponse JSON de Ollama
        """
        try:
            # Nettoyer la réponse (supprimer markdown si présent)
            response = response.strip()
            if response.startswith("```json"):
                response = response[7:]
            if response.startswith("```"):
                response = response[3:]
            if response.endswith("```"):
                response = response[:-3]
            
            # Extraire le JSON valide
            response = response.strip()
            start_idx = response.find('{')
            end_idx = response.rfind('}') + 1
            
            if start_idx >= 0 and end_idx > start_idx:
                json_str = response[start_idx:end_idx]
                data = json.loads(json_str)
            else:
                raise ValueError("No JSON found in response")
            
            decision = RCADecision.model_validate(data)
            return decision
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            logger.error(f"Error parsing Ollama response: {e}")
            raise


# Instance globale
llm_analyzer = LLMRCAAnalyzer()
