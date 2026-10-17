from contextlib import asynccontextmanager
import logging
from pathlib import Path
import sqlite3

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from board import __version__
from board.server import db
from board.server.routers import assets, evidence, export, findings, hints, intents, jobs, projects, settings

STATIC_DIR = Path(__file__).parent / "static"
LOG = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.configure(db.DEFAULT_DB)
    yield


app = FastAPI(
    title="Strikebox",
    description="Fact-graph based collaborative exploration protocol",
    version=__version__,
    lifespan=lifespan,
)

@app.exception_handler(sqlite3.OperationalError)
async def sqlite_operational_error(_request, exc: sqlite3.OperationalError):
    if not db.is_contention_error(exc):
        raise exc
    LOG.warning("database contention code=%s; request rolled back", exc.sqlite_errorcode)
    return JSONResponse(
        status_code=503,
        content={"detail": "Board database is busy; retry shortly.", "code": "database_busy"},
        headers={"Retry-After": "1"},
    )


app.include_router(settings.router)
app.include_router(projects.router)
app.include_router(hints.router)
app.include_router(intents.router)
app.include_router(jobs.router)
app.include_router(evidence.router)
app.include_router(findings.router)
app.include_router(assets.router)
app.include_router(export.router)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
