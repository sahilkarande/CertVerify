"""Aggregates all versioned API routes."""

from fastapi import APIRouter

from app.api.routes import admin, auth, documents, health, review, student

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, tags=["auth"])
api_router.include_router(review.router, tags=["review"])
api_router.include_router(documents.router, prefix="/review", tags=["documents"])
api_router.include_router(student.router, tags=["student"])
api_router.include_router(admin.router, tags=["admin"])
