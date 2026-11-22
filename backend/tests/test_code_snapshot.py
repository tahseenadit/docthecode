"""Downloading the code repo for the agent: tarball, size limit, and the API fallback."""
import asyncio
import io
import tarfile

import httpx
import pytest

from app.agent.codebase import ApiCodebase, SnapshotCodebase
from app.config import GitHubHost
from app.github.client import GitHubClient, GitHubError
from app.services.sync import load_codebase

HOST = GitHubHost("github", "GitHub", "https://github.com", "https://api.github.com", "1", "app", "cid", "cs", "pem", "wh")


def archive() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"print('hi')\n"
        info = tarfile.TarInfo("acme-api-abc/app.py")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def client(handler) -> GitHubClient:
    return GitHubClient(HOST, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_tarball_follows_the_redirect_without_leaking_the_token():
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.url.host, req.headers.get("authorization")))
        if req.url.host == "api.github.com":
            assert req.url.path == "/repos/acme/api/tarball/abc"
            return httpx.Response(302, headers={"location": "https://codeload.github.com/acme/api/legacy.tar.gz/abc?token=x"})
        return httpx.Response(200, content=archive())

    data = asyncio.run(client(handler).tarball("ghs_tok", "acme", "api", "abc", 10_000_000))
    assert SnapshotCodebase.from_tarball(data).files == {"app.py": "print('hi')\n"}
    assert seen == [("api.github.com", "Bearer ghs_tok"), ("codeload.github.com", None)]


def test_tarball_size_limit():
    gh = client(lambda req: httpx.Response(200, content=b"x" * 5000))
    with pytest.raises(GitHubError):
        asyncio.run(gh.tarball("t", "acme", "api", "abc", 1000))


def test_load_codebase_falls_back_to_the_api():
    def handler(req: httpx.Request):
        if "/tarball/" in req.url.path:
            return httpx.Response(404, json={"message": "Not Found"})
        if "/git/trees/" in req.url.path:
            return httpx.Response(200, json={"tree": [{"path": "app.py", "type": "blob"}]})
        return httpx.Response(200, text="print('hi')\n")

    async def go():
        code = await load_codebase(client(handler), "t", "acme", "api", "abc")
        return code, await code.read("app.py")

    code, text = asyncio.run(go())
    assert isinstance(code, ApiCodebase) and text == "print('hi')\n"
