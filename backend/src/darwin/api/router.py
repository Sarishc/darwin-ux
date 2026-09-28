"""Version 1 of the HTTP API. Every v1 router is included here, under /api/v1."""

from fastapi import APIRouter

from darwin.api import experiments, health, telemetry

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(health.router)
api_v1_router.include_router(telemetry.router)
api_v1_router.include_router(experiments.router)
