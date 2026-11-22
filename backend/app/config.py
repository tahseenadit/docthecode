"""Server settings. None are required.

DocTheCode is configured in the browser (Setup / Admin pages): GitHub App credentials, the
Anthropic key, admins and the public URL are stored in the database, secrets encrypted.
The environment variables below only exist to override the zero-config defaults.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


VERSION = "0.5.0"

# Where DocTheCode servers send anonymous usage data: the maintainers' collector (telemetry-collector/).
# The ingest key can only submit usage data, never read it. Forks can point these at their own
# collector, override them with DOCTHECODE_TELEMETRY_URL / _KEY, or set the URL to "" to disable.
DEFAULT_TELEMETRY_URL = "https://docthecode-telemetry.onrender.com"
DEFAULT_TELEMETRY_KEY = "OB5oJkBB8OWLgbH/3/2FoFqYq/mAPGsgXk/4ZXEGHfg="  # write-only ingest key; public by design


@dataclass(frozen=True)
class GitHubHost:
    """A GitHub App registration for one GitHub host, with decrypted secrets (in memory only)."""
    id: str                      # short slug, e.g. "github" or "acme"
    name: str                    # label shown on the login button
    web_url: str                 # https://github.com or https://ghe.acme.com
    api_url: str                 # https://api.github.com or https://ghe.acme.com/api/v3
    app_id: str
    app_slug: str
    client_id: str
    client_secret: str
    private_key: str             # PEM contents
    webhook_secret: str


def default_api_url(web_url: str) -> str:
    web_url = web_url.rstrip("/")
    return "https://api.github.com" if web_url == "https://github.com" else f"{web_url}/api/v3"


def normalize_database_url(url: str) -> str:
    """Accept the URLs that Neon, Supabase, Render etc. hand out and pick the psycopg 3 driver."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


@dataclass(frozen=True)
class Settings:
    data_dir: str = "./data"             # everything DocTheCode stores; /data in the Docker image
    public_url_override: str = ""        # empty: detected from the browser and editable in Admin
    database_url: str = ""
    secret_key: str = ""
    git_root: str = ""                   # doc repos (bare git)
    repo_storage: str = "disk"           # "disk" or "database" (see services/repo_storage.py)
    agent_max_turns: int = 40
    agent_bootstrap_max_turns: int = 150   # runs that write a whole kind of documentation (or the whole space)
    check_name: str = "DocTheCode / documentation approved"
    # Anonymous product telemetry (see services/telemetry.py). Admins can switch it off in the UI;
    # DOCTHECODE_TELEMETRY=off disables it for the whole server.
    telemetry_url: str = ""
    telemetry_key: str = ""
    telemetry_enabled: bool = True
    setup_token: str = ""                # optional fixed first-run token instead of one printed in the log

    @property
    def uses_bundled_sqlite(self) -> bool:
        return self.database_url == f"sqlite:///{self.data_dir}/docthecode.db"


@lru_cache
def get_settings() -> Settings:
    """Every setting is optional. With none at all, DocTheCode stores everything in ./data
    (/data in the Docker image), generates its secret key on first start, and learns its
    public address from the first browser that opens it."""
    from .storage_backup import ensure_secret_key

    env = os.environ
    data_dir = env.get("DOCTHECODE_DATA_DIR", Settings.data_dir).rstrip("/") or "/"
    secret = env.get("DOCTHECODE_SECRET_KEY") or env.get("DOCTHECODE_SESSION_SECRET", "")
    if secret and len(secret) < 32:
        raise SystemExit("DOCTHECODE_SECRET_KEY must be at least 32 characters (or leave it unset to auto-generate one).")
    platform_url = env.get("RENDER_EXTERNAL_URL", "")
    return Settings(
        data_dir=data_dir,
        public_url_override=(env.get("DOCTHECODE_PUBLIC_URL") or platform_url).rstrip("/"),
        database_url=normalize_database_url(env.get("DATABASE_URL") or f"sqlite:///{data_dir}/docthecode.db"),
        # Without DOCTHECODE_SECRET_KEY the key lives in the data directory (and its backups). Set the
        # variable from a secret manager if you want the key kept apart from the data it protects.
        secret_key=secret or ensure_secret_key(data_dir),
        git_root=env.get("DOCTHECODE_GIT_ROOT") or f"{data_dir}/repos",
        repo_storage=env.get("DOCTHECODE_REPO_STORAGE", Settings.repo_storage).lower(),
        agent_max_turns=int(env.get("DOCTHECODE_AGENT_MAX_TURNS", Settings.agent_max_turns)),
        agent_bootstrap_max_turns=int(env.get("DOCTHECODE_AGENT_BOOTSTRAP_MAX_TURNS",
                                              Settings.agent_bootstrap_max_turns)),
        check_name=env.get("DOCTHECODE_CHECK_NAME", Settings.check_name),
        telemetry_url=env.get("DOCTHECODE_TELEMETRY_URL", DEFAULT_TELEMETRY_URL).rstrip("/"),
        telemetry_key=env.get("DOCTHECODE_TELEMETRY_KEY", DEFAULT_TELEMETRY_KEY),
        telemetry_enabled=env.get("DOCTHECODE_TELEMETRY", "on").lower() not in ("off", "0", "false", "no"),
        setup_token=env.get("DOCTHECODE_SETUP_TOKEN", "").strip(),
    )
