"""Aggregates all versioned API routes."""

from fastapi import APIRouter

from app.api.routes import auth, health, review

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, tags=["auth"])
api_router.include_router(review.router, tags=["review"])
