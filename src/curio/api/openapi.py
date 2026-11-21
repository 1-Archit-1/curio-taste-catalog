"""Export the control-plane OpenAPI schema (only `/api/*` routes) for TypeScript codegen.

Addon routes follow the Stremio protocol and are not consumed by the control-plane UI, so they
are excluded here along with `/healthz`.

Usage:
    uv run python -m curio.api.openapi > openapi.json
"""

from __future__ import annotations

import json
import sys
from typing import Any

from curio.api.app import create_app
from curio.settings import Settings

_DUMMY_SETTINGS = Settings(
    _env_file=None,
    database_url="postgresql://unused@127.0.0.1:1/unused",
)


def export_control_plane_openapi() -> dict[str, Any]:
    """Return the OpenAPI dict filtered to `/api/*` control-plane paths."""
    full_schema = create_app(_DUMMY_SETTINGS).openapi()
    api_paths = {
        path: ops for path, ops in full_schema.get("paths", {}).items() if path.startswith("/api/")
    }
    return {
        **full_schema,
        "paths": api_paths,
    }


def main() -> None:
    json.dump(export_control_plane_openapi(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
