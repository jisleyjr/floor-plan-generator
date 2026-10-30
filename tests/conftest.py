"""Keep service configuration tests independent of the developer environment."""
from __future__ import annotations

import pytest

_SERVICE_ENV = ("DATABASE_URL", "FLOORPLAN_DATA_DIR", "FLOORPLAN_API_KEY")


@pytest.fixture(autouse=True)
def _clear_service_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _SERVICE_ENV:
        monkeypatch.delenv(name, raising=False)
