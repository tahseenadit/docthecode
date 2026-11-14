# DocTheCode

**Documentation that ships with your code.**

DocTheCode hosts documentation spaces and connects each one to a GitHub repository. When someone opens a pull request on the code repo, DocTheCode opens a matching **doc PR**, an AI agent drafts the documentation changes, and a required GitHub check keeps the code PR **unmergeable until the doc PR is approved**. When the code PR merges, the doc PR merges with it.

**Version 0.4.1** · [Apache 2.0 license](LICENSE)

- **Self-hosted, your data stays yours.** DocTheCode is one Docker image with PostgreSQL built in. Each organization builds it, hosts it wherever it likes, and finishes setup in the browser. Docs, users, credentials and history stay on that host.
- **Sign in with GitHub**: personal accounts, GitHub Enterprise Cloud orgs on github.com, and any number of GitHub Enterprise Server instances. Everyone automatically sees the doc spaces and open doc PRs for the GitHub repositories they can access.
- **Organized like a wiki, not like the code.** A doc space is a tree of titled pages and sections (think Confluence), independent of the code repo's folder layout.
- **Any AI provider.** The documentation agent runs on Anthropic, OpenAI, Google Gemini, GitHub Copilot (billed to your organization's Copilot plan, no API key), or any OpenAI-compatible endpoint such as Azure AI Foundry, OpenRouter or a self-hosted Ollama.
- **Configured in the browser.** GitHub App credentials (created in one click), the AI provider and admins are set in the Setup / Admin pages; secrets are stored encrypted.

## Quick start

```bash
docker run -d --name docthecode --restart unless-stopped \
  -p 8080:8080 -v docthecode-data:/data ghcr.io/tahseenadit/docthecode:latest
```

Open `http://localhost:8080` and follow the setup page. For a real deployment, put HTTPS in front of it; see [Self-host DocTheCode](#self-host-docthecode).

## How it works

```mermaid
sequenceDiagram
    participant Dev as Developer
    participant GH as GitHub (code repo)
    participant DH as DocTheCode
    participant AI as Doc agent (AI provider)
    participant Rev as Reviewer

    Dev->>GH: Open PR #35;42
    GH->>DH: webhook pull_request.opened
    DH->>DH: Create doc branch + doc PR #35;7
    DH->>GH: Check run "DocTheCode / documentation approved" = in progress
    DH->>GH: Comment linking doc PR #35;7
    DH->>AI: Code diff + existing docs
    AI->>DH: Edits via tools (read/write doc files, read code)
    DH->>DH: Commit to doc branch
    Rev->>DH: Approve doc PR #35;7 (or request changes → agent redrafts)
    DH->>GH: Check run = success ✓  (code PR can merge)
    Dev->>GH: Push more commits
    GH->>DH: webhook synchronize → agent updates docs, approval goes stale, check pending again
    Dev->>GH: Merge PR #35;42
    GH->>DH: webhook closed(merged) → doc PR #35;7 merged into docs main
```

**The merge gate.** DocTheCode posts a GitHub *check run* named `DocTheCode / documentation approved` on the code PR's head commit. Making it a required status check in branch protection is what blocks the merge. DocTheCode can set that up for you when you connect a repo (if you're an admin there), or an admin adds it by hand. The check is:

| Doc PR state | Check on code PR |
|---|---|
| Agent drafting | in progress (blocks) |
| Waiting for review | in progress (blocks) |
| Approved on the current doc commit | ✅ success |
| Changes requested (by anyone's latest review) | ❌ failure |
| Docs changed after approval | in progress (approval is stale) |
| Agent found nothing to document | in progress, or neutral (passes) if the repo opts in |
| Doc PR closed | ❌ failure |

Approvals are tied to the doc commit they were given on, so any new doc commit (from the agent after a push, or a human edit) automatically requires a fresh approval. Each reviewer's latest review wins.

**Members are inherited from GitHub.** Connecting a doc space to a GitHub repo imports everyone who has access to that repo (direct collaborators, team members, org members via the base permission, outside collaborators) with the matching role: GitHub admin → admin; maintain/write → write; triage/read → read. Nobody needs an invite: people have access from their very first sign-in. The list follows GitHub through `member` webhooks, an hourly background sync, and a *Re-sync members* button. People without access to the code can still be added by hand. DocTheCode admins can see every space.

**Who can review.** Anyone with write access (inherited or added). The code PR's author can approve their own docs by default; turn that off per connected repo if you want an independent reviewer. The number of required approvals and custom agent instructions are also per connected repo.

## Where data lives

| What | Where |
|---|---|
| Users, members inherited from GitHub, doc PRs, reviews, settings | PostgreSQL: built into the image (in `/data/postgres`), or the organization's own Postgres via `DATABASE_URL` |
| Doc contents and full history | Bare git repositories in `/data/repos` |
| GitHub App secrets, private keys, AI provider keys, users' GitHub tokens | The database, encrypted with the server's secret key |
| Secret key | Generated into `/data/.secret_key` on first start, or `DOCTHECODE_SECRET_KEY` from a secret manager |
| Anonymous usage statistics and error reports | Sent to the DocTheCode maintainers' collector unless an admin switches it off (see *Usage data*) |

**Why PostgreSQL + git.** Documentation is a set of Markdown files that change together and need history, diffs, review and merges: that is what git is for, so doc contents live in git repositories. PostgreSQL holds the structured data (users, members, PRs, reviews, settings) and handles concurrent users and background jobs without fuss. The image runs its own PostgreSQL inside the container, reachable only through a socket inside it, so there's nothing extra to install or connect; set `DATABASE_URL` to use an existing Postgres instead.

**Backups:** everything is in `/data`: the database, the repos and the key. Back up that volume, or mount a folder or bucket at `/backup` and DocTheCode keeps a consistent snapshot there automatically (`docthecode-backup.tar.gz` with a `pg_dump` of the database, plus the previous snapshot).

## Doc spaces are wikis

A doc space is a tree of pages, organized for readers rather than mirroring the code:

```
index.md                    the space's home page
getting-started.md          a page
guides/                     a section: a page with child pages
  index.md                  the section's own page
  deploying.md
reference/cli.md
images/architecture.png     an attachment
```

- A page's title is its first `# Heading` (or `title:` front matter); `order: 1` front matter pins its position among siblings.
- **+ Child page** on a plain page turns it into a section automatically.
- **Move** relocates a page or a whole section (attachments included); **Rename** changes only the title, so links keep working. Relative links between pages navigate inside DocTheCode.
- The agent sees the page tree (not just file names) and is instructed to organize by audience and topic (Getting started, Guides, Concepts, Reference, Operations), update existing pages before adding new ones, link new pages from their section page, and never recreate the code's folder structure. Per-repo agent instructions can describe a space's own conventions.

## AI providers

The documentation agent is the same everywhere: it gets the PR's diff and the space's page tree, and edits pages through DocTheCode's tools (read page, write page, read code file, finish…). Only the model behind it changes. Admins pick it under **Admin → Documentation agent**:

| Provider | What you need | Notes |
|---|---|---|
| **Anthropic** | API key | Default model `claude-sonnet-5-5` |
| **OpenAI** | API key | Pick a model with *Load models* |
| **Google Gemini** | Gemini API key | Uses Gemini's OpenAI-compatible endpoint |
| **GitHub Copilot** | No key | Uses DocTheCode's GitHub App; billed to the organization that owns the code repo. Give the app the organization permission **Copilot Requests** (read-only), and allow Copilot requests from GitHub Apps in the organization's Copilot policies. Model `auto` lets Copilot choose. |
| **Other (OpenAI-compatible)** | Base URL (+ key if required) | Azure AI Foundry, OpenRouter, Ollama, vLLM, LiteLLM and other gateways |

Model lists come from the provider (*Load models*), so new models appear without updating DocTheCode. The chosen model must support tool (function) calling; *Test* checks this with a one-tool round trip. Each provider's key is stored separately and encrypted, so switching back and forth doesn't lose them.

## Usage data

DocTheCode sends anonymous usage statistics and error reports to a collector run by the DocTheCode maintainers (`https://docthecode-telemetry.onrender.com`), so we can see which features are used, where onboarding stalls, how the agent performs and what breaks. Nothing that identifies your organization, people, code or documentation is sent. Forks can point it at their own collector with `DOCTHECODE_TELEMETRY_URL` and `DOCTHECODE_TELEMETRY_KEY`, or change the defaults in `backend/app/config.py`.

| Sent | Never sent |
|---|---|
| Random server ID, DocTheCode version, OS, database kind | Documentation or code contents, diffs, file or page names |
| Rounded counts (users, doc spaces, connected repos, open doc PRs, members imported per sync and their permission mix) | Names of repos, orgs or users; emails; GitHub logins; member lists |
| Feature events with small numeric/enum details (doc PR opened, review approved, agent run took 42 s / 6 turns on provider X, …) | Tokens, keys, URLs, review comments, agent prompts or outputs |
| Per-user IDs hashed with the server's own secret key (counts users, never identifies them) | Request paths or bodies |
| API route templates (`/api/repos/{owner}/{name}`), status codes, timings; browser screen templates | |
| Errors: exception type, a scrubbed message (tokens, emails, URLs, paths, long strings removed), code locations | Local variables or request data |

Admins can switch it off under **Admin → Usage data**, which also shows the exact JSON of the last batch sent. `DOCTHECODE_TELEMETRY=off` disables it for the whole server. The code is in `backend/app/services/telemetry.py` and `frontend/src/telemetry.ts`.

## Self-host DocTheCode

DocTheCode ships as one Docker image containing the web app, the API and PostgreSQL. The database is created on first start, the secret key is generated, and the public address is learned from the browser. **Nothing needs configuring before it starts**: run the image anywhere that runs containers, open its address, and finish setup in the browser.

### 1. Get the image

**Option A: use the prebuilt image.** Every release is published to GitHub Container Registry:

```bash
docker pull ghcr.io/tahseenadit/docthecode:latest      # or a version, e.g. :0.4.1
```

**Option B: build it yourself.** Install [Docker Desktop](https://docs.docker.com/desktop/) (macOS, Windows) or [Docker Engine](https://docs.docker.com/engine/install/) (Linux); [Podman](https://podman-desktop.io) works too. Node, Python and PostgreSQL are fetched inside the build.

```bash
git clone https://github.com/tahseenadit/docthecode.git
cd docthecode
docker build --platform linux/amd64 -t docthecode:latest .
```

A fork builds its own image too: the included workflow (`.github/workflows/build-image.yml`) runs the tests and publishes `ghcr.io/<account>/<repo>` on every push to `main` and every version tag.

### 2. Host it

Push the image to a registry your host can pull from, unless you use the prebuilt image or build on the host itself:

```bash
docker tag docthecode:latest REGISTRY/docthecode:latest   # e.g. docker.io/you, ghcr.io/you, your cloud's registry
docker push REGISTRY/docthecode:latest
```

Then run it anywhere that runs containers: a VM with Docker, Kubernetes, or a cloud container service. Whatever you choose, it needs:

| Requirement | Why |
|---|---|
| **Persistent storage at `/data`** (a volume or disk, 1 GB is plenty to start) | The database, doc repos and secret key live there |
| **Exactly one instance, always running** | One database; webhooks and background jobs must always be handled |
| An **HTTPS address** forwarding to container port **8080** (or `$PORT` if the host sets one) | People open DocTheCode there; if GitHub can reach it too, webhooks work (otherwise see *When GitHub can't reach DocTheCode*) |
| **Outgoing internet access** to GitHub and your AI provider | DocTheCode calls GitHub's API and the AI |
| **~1 GB of memory** | DocTheCode and PostgreSQL together |

On a machine with Docker, that's:

```bash
docker run -d --name docthecode --restart unless-stopped -p 8080:8080 -v docthecode-data:/data REGISTRY/docthecode:latest
```

and put HTTPS in front of it (your load balancer, ingress, or a reverse proxy such as Caddy or nginx). `docker-compose.yml` does the same with `docker compose up -d`.

**Hosts without persistent disks.** Some container services wipe the container's disk on every restart. There, mount a bucket or network folder at `/backup` instead. DocTheCode then writes a consistent snapshot there within ~30 seconds of every change and on shutdown, and restores it automatically when a new container starts. Never point `/data` itself at a bucket mount: databases need real disks. If DocTheCode finds neither a volume at `/data` nor a folder at `/backup`, it logs a warning at start-up.

### When GitHub can't reach DocTheCode

GitHub normally tells DocTheCode about pull requests through webhooks, which only work if GitHub can reach DocTheCode's address. On a private service or behind a firewall, untick *GitHub can reach this address* when creating the GitHub App in setup. Then click **Check GitHub for changes** (on the dashboard, or on a doc space's *Pull requests* tab) whenever you want DocTheCode to catch up. DocTheCode asks GitHub, over an outgoing connection, which pull requests were opened, updated, merged or closed, and reacts exactly as it would to webhooks: it opens doc PRs, runs the agent on new commits, and merges or closes doc PRs. Nothing runs on a timer.

Everything that goes from DocTheCode *to* GitHub works either way: the documentation check on pull requests, comments, and approvals turning the check green.

### 3. Open it and finish setup in the browser

1. Open DocTheCode's address. Until setup is finished, it shows the setup page to whoever opens it first.
2. **Connect GitHub** → *Create automatically*. DocTheCode fills in its own callback and webhook URLs; confirm on GitHub, then **install the app** on the repositories you want documented.
3. **Choose the AI provider** for the documentation agent, paste its API key (not needed for GitHub Copilot), click *Load models* and pick one, then *Test*.
4. **Sign in with GitHub**. You become the admin.

That's it. Keep the container running. Teammates open the same address and sign in with GitHub; they automatically see the doc spaces for repositories they can access. Create a doc space, connect it to a repo in its *Settings*, and the next pull request on that repo gets a doc PR.

On GitHub Free, branch protection (what blocks the merge until docs are approved) only works on public repositories.

### Updating

Pull or build the new image and replace the container, keeping the same `/data` volume (or `/backup` mount). New database tables are created automatically. If a future image moves to a newer PostgreSQL major version, DocTheCode refuses to start with the old data and says so, rather than risk it.

### Optional settings

None are needed. They only override the defaults:

| Variable | Default | Use it to |
|---|---|---|
| `DOCTHECODE_PUBLIC_URL` | learned from the browser, editable in Admin | pin the address |
| `DOCTHECODE_SECRET_KEY` | generated into `/data/.secret_key` | keep the encryption key in a secret manager, apart from the data |
| `DATABASE_URL` | built-in PostgreSQL | use an existing PostgreSQL (`postgresql://user:pass@host/db`) |
| `DOCTHECODE_DATA_DIR`, `DOCTHECODE_BACKUP_DIR` | `/data`, `/backup` | move the folders |
| `DOCTHECODE_SETUP_TOKEN` | none: the first visitor runs setup | require this token to run setup; use it if DocTheCode is reachable from the internet before you finish setting it up |
| `DOCTHECODE_TELEMETRY` | `on` | `off` disables anonymous usage data for the server |

## Development

### Run it locally

Needs Python 3.11+ (on macOS without admin rights, use [uv](https://docs.astral.sh/uv/)), Node 20+ and git. No configuration is needed; data goes to `backend/data/`. For convenience this development mode uses SQLite unless you set `DATABASE_URL`; the Docker image always uses PostgreSQL.

```bash
cd backend
uv venv --python 3.12 && source .venv/bin/activate        # Windows: .venv\Scripts\activate
uv pip install -r requirements.txt                         # or: pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# second terminal
cd frontend && npm install && npm run dev                   # open http://localhost:5173
```

Setup works as described above. Untick *GitHub can reach this address* when creating the GitHub App, and use *Check GitHub for changes* after opening or updating a pull request: the full PR flow works locally too.

If you created your GitHub App by hand, make sure it has the **Member** event and the organization permission **Members: read** in the app's settings. Apps created with *Create automatically* already have them.

### Project layout

```
backend/                     DocTheCode server: FastAPI + SQLAlchemy + PostgreSQL (Python 3.11+)
  app/gitstore.py            Bare git repos (plumbing only, CAS ref updates, merge-tree merges, byte-safe moves)
  app/pages.py               Page tree rules: titles, sections, ordering, child pages, moves
  app/agent/doc_agent.py     Provider-neutral tool-use loop that edits the doc space
  app/agent/providers.py     Anthropic, OpenAI, Gemini and OpenAI-compatible adapters (plain HTTPS)
  app/agent/copilot_agent.py GitHub Copilot via the Copilot SDK, same tools
  app/services/settings.py   Encrypted credentials, GitHub hosts, first-run setup
  app/services/access.py     Which doc spaces each user sees, from their GitHub access
  app/services/gate.py       Merge-gate rules (what the GitHub check says)
  app/services/sync.py       Webhooks, doc PR lifecycle, agent runs, check runs
  app/services/telemetry.py  Anonymous usage data client
  app/routers/               setup/admin, auth, repos/inbox, pages, pulls/reviews, webhooks, telemetry
frontend/                    React + Vite + TypeScript (Setup, Admin, Dashboard, Pages, PRs)
Dockerfile                   The whole product in one image (web app, API, PostgreSQL 17)
docker/entrypoint.sh         Starts the built-in PostgreSQL, then DocTheCode; stops both cleanly
.github/workflows/           Tests and publishes the image to GitHub Container Registry
telemetry-collector/         Optional service that receives anonymous usage data (for maintainers)
  collector/app.py           Ingest API (write-only key, validation, rate limit) + /v1/summary
  sql/analytics.sql          Ready-made queries: adoption, onboarding funnel, agent quality, errors, latency
```

### Tests

```bash
cd backend && pytest                      # includes starting the built-in PostgreSQL and backups (needs PostgreSQL + root; skipped otherwise)
cd telemetry-collector && pip install -r requirements.txt && pytest
```

### Usage-data collector (for maintainers)

The collector is a small separate service that receives anonymous usage data from DocTheCode servers and stores it in a Postgres database run by whoever ships the build.

1. Create a Postgres database (e.g. [Neon](https://neon.tech)).
2. Deploy `telemetry-collector/` anywhere that runs a Dockerfile (`telemetry-collector/render.yaml` is a ready blueprint for Render) with `DATABASE_URL`, `COLLECTOR_INGEST_KEYS` and `COLLECTOR_ADMIN_TOKEN`. Tables are created on first start.
3. Put the collector's URL and the ingest key in `backend/app/config.py` (`DEFAULT_TELEMETRY_URL`, `DEFAULT_TELEMETRY_KEY`) so the builds you ship report to it. (`DOCTHECODE_TELEMETRY_URL` / `DOCTHECODE_TELEMETRY_KEY` override per server, handy for testing.)
4. Analyse with `telemetry-collector/sql/analytics.sql` in the Neon SQL editor, or connect Metabase / Grafana. Quick check: `curl -H "Authorization: Bearer <admin token>" https://<collector>/v1/summary`.

The ingest key is write-only and ships inside DocTheCode, so treat it as public: the collector validates and size-limits every batch and rate-limits each server. Rotate it by adding a new key to `COLLECTOR_INGEST_KEYS` before removing the old one. Say in your terms or privacy notice that DocTheCode sends this data and how to switch it off; the retention query at the end of `analytics.sql` keeps 13 months.

## Limitations and roadmap

- **One instance per organization.** Background jobs run inside the server process and the database lives in the container's volume, so run exactly one DocTheCode container. On hosts that use `/backup` snapshots instead of a volume, a crash can lose up to ~30 seconds of changes.
- **Database migrations**: new tables are created automatically, but changes to existing tables are not migrated yet. Read the release notes before upgrading.
- **Attachments**: they move and are versioned correctly, but the UI can't upload or preview images yet.
- **Planned**: search across pages, and git over HTTP to clone and push docs locally.
- **Rotating `DOCTHECODE_SECRET_KEY`** needs a small re-encryption script.
- **Disconnecting a repo** leaves in-flight check runs on GitHub as they are; remove the required check from branch protection when you disconnect.

## Contributing

Issues and pull requests are welcome at [github.com/tahseenadit/docthecode](https://github.com/tahseenadit/docthecode). For larger changes, open an issue first to discuss the approach. Run the tests before sending a pull request.

## Security

Please report vulnerabilities privately through [GitHub security advisories](https://github.com/tahseenadit/docthecode/security/advisories/new) rather than public issues.

## License

DocTheCode is licensed under the [Apache License 2.0](LICENSE).
