import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from config import get_settings
from api.routers import health, subscriptions, frontend, admin, dashboard
from api.middleware import log_request_middleware
from api.dependencies import get_templates

logger = logging.getLogger(__name__)

settings = get_settings()


class RevalidatingStaticFiles(StaticFiles):
    '''
    Serve static assets with `Cache-Control: no-cache`.

    StaticFiles sends only ETag and Last-Modified. With no Cache-Control a browser
    falls back to heuristic freshness — roughly 10% of the file's age — so a
    stylesheet that had sat unchanged for months gets cached for days and an edit
    never reaches anyone still holding the old copy. `no-cache` means "store it,
    but revalidate before use", so the ETag still turns repeat requests into 304s.
    '''

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers['Cache-Control'] = 'no-cache'
        return response

@asynccontextmanager
async def lifespan(_: FastAPI):
    # Print all registered routes
    for route in app.routes:
        if hasattr(route, 'path'):
            logger.info(f'Route: {route.path} | Methods: {getattr(route, 'methods', 'N/A')}')  # pyright: ignore[reportAttributeAccessIssue]
    yield

def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(404)
    async def not_found_handler(request: Request, exc: StarletteHTTPException) -> HTMLResponse:
        return get_templates().TemplateResponse(
            '404.html',
            {'request': request, 'title': '404 — NotiFER'},
            status_code=404
        )

    app.middleware('http')(log_request_middleware)

    app.include_router(health.router)
    app.include_router(subscriptions.router)
    app.include_router(frontend.router)
    app.include_router(admin.router)
    app.include_router(dashboard.router)

    app.mount('/static', RevalidatingStaticFiles(directory='static'), name='static')

    return app

app = create_app()
