from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, APIRouter, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.app.api import accounts, ai, auth, channel, auto_tasks, drafts, files, keyword_groups, login_sessions, model_configs, notes, notifications, publish, tags, tasks
from backend.app.api import model_routing, direct_messages, dm_chat, comment_monitoring, business, sms_relay
from backend.app.api import account_maintenance
from backend.app.api import proactive_interactions
from backend.app.services.proactive_interactions import run_worker as run_proactive_worker
from backend.app.services.dm_worker import run as run_dm_worker
from backend.app.services.xhs_network import monitor as monitor_xhs_network
from backend.app.services.publish_recovery import run as run_publish_recovery
from backend.app.api.platforms import registry
from backend.app.api.platforms.xhs import analytics, crawl, creator, monitoring, pc
from backend.app.core.config import get_settings
from backend.app.core.database import init_db
from backend.app.core.deps import get_current_user
from backend.app.services.scheduler_service import run_due_auto_tasks, shutdown_due_publish_scheduler, start_due_publish_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    settings = get_settings()
    scheduler = None
    if settings.scheduler_enabled:
        scheduler = start_due_publish_scheduler(settings.scheduler_interval_seconds)
    app.state.scheduler = scheduler
    network_monitor = asyncio.create_task(monitor_xhs_network())
    dm_worker = asyncio.create_task(run_dm_worker())
    business_worker = asyncio.create_task(business.run())
    publish_recovery = asyncio.create_task(run_publish_recovery())
    proactive_worker = asyncio.create_task(run_proactive_worker())
    try:
        yield
    finally:
        dm_worker.cancel()
        business_worker.cancel()
        network_monitor.cancel()
        publish_recovery.cancel()
        proactive_worker.cancel()
        await asyncio.gather(dm_worker, network_monitor, business_worker, publish_recovery, proactive_worker, return_exceptions=True)
        shutdown_due_publish_scheduler(scheduler)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.api_title, lifespan=lifespan)

    origins = [origin.strip() for origin in settings.backend_cors_origins.split(",") if origin.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware('http')
    async def private_api_cache(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'private, no-store'
            response.headers['Vary'] = 'Authorization, Cookie'
        return response

    @app.get("/api/health", tags=["health"])
    def health() -> dict:
        return {"status": "ok", "service": "spider-xhs"}

    app.include_router(business.router, prefix="/api")
    app.include_router(registry.router, prefix="/api")
    from backend.app.core.ops_identity import endpoint as identity_endpoint
    if identity_endpoint():
        from backend.app.api.identity_proxy import router as identity_router
        app.include_router(identity_router, prefix='/api')
    else:
        # Standalone development compatibility; production always uses Ops Hub.
        app.include_router(auth.router, prefix="/api")
    app.include_router(accounts.router, prefix="/api")
    app.include_router(login_sessions.router, prefix="/api")
    app.include_router(sms_relay.router, prefix="/api")
    app.include_router(account_maintenance.router, prefix="/api")
    private = APIRouter(dependencies=[Depends(get_current_user)])
    for router in (notes.router, files.router, drafts.router, ai.router, channel.router,
                   model_routing.router, direct_messages.router, dm_chat.router,
                   comment_monitoring.router, proactive_interactions.router, tasks.router,
                   model_configs.router, tags.router, notifications.router, keyword_groups.router,
                   publish.router, analytics.router, pc.router, creator.router, crawl.router,
                   monitoring.router, auto_tasks.router):
        private.include_router(router)
    app.include_router(private, prefix='/api')

    # Serve pre-built frontend in production / Docker
    if settings.frontend_serve_static:
        frontend_dist = Path(settings.frontend_build_dir)
        if frontend_dist.is_dir():
            from starlette.responses import FileResponse

            # Serve index.html for SPA client-side routing (non-API, non-file paths)
            @app.middleware("http")
            async def _spa_fallback(request, call_next):
                response = await call_next(request)
                path = request.url.path
                if (
                    response.status_code == 404
                    and not path.startswith("/api")
                    and "." not in path.split("/")[-1]
                ):
                    return FileResponse(str(frontend_dist / "index.html"))
                return response

            app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")

    return app


app = create_app()
