from __future__ import annotations

import threading

from .client import GitHubClient

_clients: dict[str, tuple[str, GitHubClient]] = {}
_guard = threading.Lock()


def github_client(host_id: str) -> GitHubClient:
    """Client for a GitHub host configured in the database.

    Rebuilt automatically when an admin changes that host's credentials. Raises KeyError
    for an unknown host.
    """
    from ..services.settings import host_version, load_host

    version = host_version(host_id)
    if version is None:
        raise KeyError(f"Unknown GitHub host '{host_id}'")
    with _guard:
        cached = _clients.get(host_id)
        if cached and cached[0] == version:
            return cached[1]
    client = GitHubClient(load_host(host_id))
    with _guard:
        _clients[host_id] = (version, client)
    return client
