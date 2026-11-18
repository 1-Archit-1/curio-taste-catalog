"""Public Stremio/Nuvio addon routes (manifest, catalogs). Unauthenticated; CORS `*`.

Routes arrive with the addon protocol work (#3).
"""

from fastapi import APIRouter

router = APIRouter(tags=["addon"])
