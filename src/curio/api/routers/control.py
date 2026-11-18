"""Authenticated control-plane routes (settings, presets, runs). No CORS.

Routes and auth arrive with users/auth (#2).
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["control"])
