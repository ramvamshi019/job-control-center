"""
main.py
-------
FastAPI application entry point.

Run from the backend/ folder:
    uvicorn app.main:app --reload

Then open http://127.0.0.1:8000/docs for interactive API docs.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.database import init_db
from app.routes import applications, companies, export, jobs, resume
from app.utils.logging import get_logger

log = get_logger("main")

app = FastAPI(
    title="Job Control Center",
    description="A targeted job-search operating system for an F-1/OPT candidate.",
    version="1.0.0",
)

# CORS: allow the local Streamlit dashboard to call this API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # local-only MVP; tighten for any real deployment
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    log.info("Database ready. API up.")


@app.get("/")
def root():
    return {"app": "Job Control Center", "status": "ok", "docs": "/docs"}


@app.get("/health")
def health():
    return {"status": "healthy"}


# Register routers.
app.include_router(jobs.router)
app.include_router(companies.router)
app.include_router(applications.router)
app.include_router(export.router)
app.include_router(resume.router)
