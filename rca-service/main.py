"""
Application FastAPI principale pour le système de RCA intelligent
"""
from fastapi import FastAPI
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
import logging
import json
import time
from datetime import datetime, timezone

try:
    from logging_loki import LokiHandler
except ImportError:  # pragma: no cover - fallback de dev/test
    LokiHandler = None

from app.utils.config import settings

# Configuration du logging
logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)
APP_VERSION = "1.0.0"
APP_STARTED_AT = datetime.now(timezone.utc)

# Ajouter Loki handler si configuré
if settings.LOKI_URL and LokiHandler is not None:
    loki_handler = LokiHandler(
        url=f"{settings.LOKI_URL}/loki/api/v1/push",
        tags={"service": "rca-service"},
        version="1",
    )
    logging.getLogger().addHandler(loki_handler)

# Création de l'app FastAPI
app = FastAPI(
    title=settings.APP_NAME,
    description="Système intelligent d'analyse de cause racine (RCA) pour les alertes",
    version=APP_VERSION,
    debug=settings.DEBUG
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def log_requests(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    latency_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        json.dumps(
            {
                "event": "http_request",
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "latency_ms": latency_ms,
            }
        )
    )
    return response


# Health check endpoint
@app.get("/health")
async def health_check():
    """Vérifier que le service est actif"""
    uptime_seconds = int((datetime.now(timezone.utc) - APP_STARTED_AT).total_seconds())
    return {
        "status": "healthy",
        "service": settings.APP_NAME,
        "uptime_seconds": uptime_seconds,
        "version": APP_VERSION,
    }

# Startup/Shutdown events
@app.on_event("startup")
async def startup_event():
    """Initialiser les services au démarrage"""
    from app.services.deduplication import dedup_service
    from app.services.history_store import history_store
    from app.services.drain3_store import drain3_store
    await dedup_service.connect()
    await drain3_store.connect()
    history_store.initialize()
    logger.info("Services initialized")

@app.on_event("shutdown")
async def shutdown_event():
    """Nettoyer les ressources à l'arrêt"""
    from app.services.deduplication import dedup_service
    from app.services.drain3_store import drain3_store
    await dedup_service.disconnect()
    await drain3_store.disconnect()
    logger.info("Services cleaned up")

# Routers
from app.routers.alerts import router as alerts_router
app.include_router(alerts_router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.DEBUG
    )
