"""Live-LLM evals (#11). Everything under evals/ is marked `eval` and so excluded by default.

Run with `uv run pytest -m eval evals/`. Evals need real network and API keys.
"""

from pathlib import Path

import pytest

_HERE = Path(__file__).parent


def pytest_collection_modifyitems(items):
    for item in items:
        if _HERE in item.path.parents:
            item.add_marker(pytest.mark.eval)
            item.add_marker(pytest.mark.enable_socket)
