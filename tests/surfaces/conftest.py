"""Fixtures for the client surfaces: backends, hooks, CLI, MCP."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mnemosyne.client.backend import LocalBackend, RemoteBackend


@pytest.fixture
def local_backend(container):
    backend = LocalBackend(container)
    yield backend


@pytest.fixture
def remote_backend(client: TestClient):
    """A RemoteBackend talking to an in-process API (local auth mode)."""
    return RemoteBackend("http://testserver", "unused-in-local-mode", client=client)


@pytest.fixture(params=["local", "remote"])
def backend(request, local_backend, remote_backend):
    return local_backend if request.param == "local" else remote_backend
