export type Host = { id: string; name: string; web_url: string };
export type Me = {
  id: string; login: string; handle: string; name: string | null; avatar_url: string | null; is_admin: boolean; host: Host;
};

export type StorageStatus = {
  data_dir: string; data_persistent: boolean; external_database: boolean; backup_enabled: boolean;
  backup_dir: string | null; last_backup_at: number | null; last_backup_error: string | null;
  snapshot: { exists: boolean; size?: number; modified?: number; age_s?: number } | null;
  restored: { from: string; at: number; seconds: number } | null; safe: boolean;
};

export type SetupStatus = {
  needs_setup: boolean; unlocked: boolean; hosts: Host[]; agent_configured: boolean;
  version: string; public_url: string; public_url_is_local: boolean; callback_url: string; token_required: boolean; telemetry_active: boolean;
  storage: StorageStatus;
};

export type HostAdmin = Host & {
  api_url: string; app_id: string; app_slug: string; client_id: string; app_url: string;
  client_secret: string | null; webhook_secret: string | null; private_key: string | null; webhook_url: string; updated_at: string;
};

export type HostInput = {
  id: string; name: string; web_url: string; api_url?: string | null; app_id: string; app_slug: string; client_id: string;
  client_secret?: string | null; private_key?: string | null; webhook_secret?: string | null;
};

export type ProviderInfo = {
  id: string; label: string; needs_key: boolean; default_model: string | null; base_url: string;
  key_hint: string; key_url: string | null;
};

export type AgentSettings = {
  provider: string | null; model: string | null; effective_model: string | null; base_url: string | null;
  api_key: string | null; problem: string | null; stored_keys: Record<string, boolean>; providers: ProviderInfo[];
};

export type AgentInput = { provider: string; api_key?: string; model?: string; base_url?: string };

export type AdminConfig = {
  hosts: HostAdmin[]; agent: AgentSettings;
  admins: { handle: string; avatar_url: string | null }[]; callback_url: string;
  public_url: string; public_url_locked: boolean; storage: StorageStatus;
  github_events: {
    last_webhook_at: Record<string, number | null>;
    links: { full_name: string; checked_at: number | null; check_error: string | null }[];
  };
};

export type ManifestInput = {
  host_id: string; host_name: string; web_url: string; organization: string; app_name: string;
  webhook_url: string | null; webhooks: boolean; public: boolean;
};

export type Link = {
  id: string; host_id: string; host_name: string; full_name: string; url: string; enabled: boolean;
  required_approvals: number; pass_when_no_changes: boolean; allow_author_approval: boolean; agent_instructions: string;
  members_count: number; members_synced_at: number | null; members_sync_error: string | null;
  github_checked_at: number | null; github_check_error: string | null;
};

export type CheckResult = { repos: number; changes: Record<string, number>; errors: string[] };

export type Member = { handle: string; avatar_url: string | null; role: string; source: string };
export type InheritedMember = Member & { github_permission: string; signed_in: boolean };

export type Repo = {
  id: string; owner: string; name: string; full_name: string; description: string; default_branch: string;
  role: "admin" | "write" | "read" | null; links: Link[]; branches?: Record<string, string>; open_prs?: number;
};

export type Gate = { status: "queued" | "in_progress" | "completed"; conclusion: string | null; title: string; summary: string };

export type PullSummary = {
  number: number; title: string; state: "open" | "merged" | "closed"; head_branch: string; base_branch: string;
  author: string | null; created_at: string; updated_at: string;
  agent_status: "idle" | "queued" | "running" | "done" | "failed" | "no_changes";
  code_pr: { full_name: string; number: number; url: string; state: string; head_sha: string } | null;
  gate: Gate;
};

export type Review = {
  id: number; user: string; avatar_url: string | null; state: "approved" | "changes_requested" | "commented";
  body: string; doc_head_sha: string; stale: boolean; addressed_by_agent: boolean; created_at: string;
};

export type Commit = { sha: string; author: string; email: string; timestamp: number; message: string };

export type Pull = PullSummary & {
  body: string; head_sha: string | null; files: { status: string; path: string }[]; diff: string; commits: Commit[];
  agent_summary: string; agent_log: string[]; role: Repo["role"];
  can_review: boolean; review_blocker: string | null; can_merge: boolean; merge_blocker: string | null; can_reopen: boolean; reviews: Review[];
};

export type GhRepo = {
  id: number; full_name: string; private: boolean; default_branch: string; installation_id: number;
  can_admin: boolean; linked_doc_repo_id: string | null;
};

export type PageNode = {
  kind: "page" | "section" | "file"; title: string; path: string | null; dir: string | null; children: PageNode[];
};
export type PageTree = { ref: string; commit: string; role: Repo["role"]; home: string | null; children: PageNode[] };

export type TelemetryStatus = {
  enabled: boolean; server_disabled: boolean; collector_configured: boolean; instance_id: string;
  what_is_sent: string[]; never_sent: string[]; last_sent_at: number | null; last_error: string | null;
  last_batch: unknown;
};

export type InboxItem = PullSummary & { repo: string; is_mine: boolean; needs_review: boolean };

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    credentials: "same-origin",
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j);
    } catch { /* not json */ }
    throw new ApiError(res.status, msg);
  }
  return res.status === 204 ? (undefined as T) : res.json();
}

const enc = encodeURIComponent;
const r = (owner: string, name: string) => `/repos/${enc(owner)}/${enc(name)}`;

export const api = {
  hosts: () => request<Host[]>("GET", "/auth/hosts"),
  setupStatus: () => request<SetupStatus>("GET", "/setup/status"),
  unlock: (token: string) => request("POST", "/setup/unlock", { token }),
  startManifest: (b: ManifestInput) => request<{ action: string; manifest: string }>("POST", "/setup/github-app/manifest", b),
  savePublicUrl: (url: string) => request<{ public_url: string }>("PUT", "/admin/public-url", { url }),
  adminConfig: () => request<AdminConfig>("GET", "/admin/config"),
  saveHost: (b: HostInput) => request<HostAdmin>("PUT", `/admin/hosts/${encodeURIComponent(b.id)}`, b),
  deleteHost: (id: string) => request<void>("DELETE", `/admin/hosts/${encodeURIComponent(id)}`),
  saveAgent: (b: AgentInput) => request<AgentSettings>("PUT", "/admin/agent", b),
  agentModels: (b: AgentInput) => request<{ models: string[] }>("POST", "/admin/agent/models", b),
  testAgent: (b: AgentInput) => request<{ ok: boolean; model: string }>("POST", "/admin/agent/test", b),
  addAdmin: (handle: string) => request("POST", "/admin/admins", { handle }),
  removeAdmin: (handle: string) => request<void>("DELETE", `/admin/admins/${encodeURIComponent(handle)}`),
  pages: (o: string, n: string, ref?: string) => request<PageTree>("GET", `${r(o, n)}/pages${ref ? `?ref=${enc(ref)}` : ""}`),
  newPage: (o: string, n: string, b: { branch: string; parent: string; title: string; body?: string }) =>
    request<{ path: string }>("POST", `${r(o, n)}/pages`, b),
  movePage: (o: string, n: string, b: { branch: string; path: string; new_parent: string }) =>
    request<{ path: string }>("POST", `${r(o, n)}/pages/move`, b),
  renamePage: (o: string, n: string, b: { branch: string; path: string; title: string }) =>
    request<{ path: string }>("POST", `${r(o, n)}/pages/rename`, b),
  deletePage: (o: string, n: string, b: { branch: string; path: string }) =>
    request<{ deleted: string[] }>("POST", `${r(o, n)}/pages/delete`, b),
  telemetryStatus: () => request<TelemetryStatus>("GET", "/admin/telemetry"),
  setTelemetry: (enabled: boolean) => request<TelemetryStatus>("PUT", "/admin/telemetry", { enabled }),
  checkGitHubAll: () => request<CheckResult>("POST", "/check-github"),
  checkGitHub: (o: string, n: string) => request<CheckResult>("POST", `${r(o, n)}/check-github`),
  inbox: () => request<InboxItem[]>("GET", "/inbox"),
  syncAccess: () => request<{ github_repos: number }>("POST", "/me/sync"),
  me: () => request<Me>("GET", "/auth/me"),
  logout: () => fetch("/api/auth/logout", { method: "POST" }),

  repos: () => request<Repo[]>("GET", "/repos"),
  createRepo: (b: { name: string; description: string; default_branch: string }) => request<Repo>("POST", "/repos", b),
  repo: (o: string, n: string) => request<Repo>("GET", r(o, n)),
  deleteRepo: (o: string, n: string) => request<void>("DELETE", r(o, n)),
  tree: (o: string, n: string, ref?: string) =>
    request<{ ref: string; files: string[] }>("GET", `${r(o, n)}/tree${ref ? `?ref=${enc(ref)}` : ""}`),
  file: (o: string, n: string, path: string, ref?: string) =>
    request<{ path: string; ref: string; commit: string; content: string }>(
      "GET", `${r(o, n)}/file?path=${enc(path)}${ref ? `&ref=${enc(ref)}` : ""}`),
  writeFile: (o: string, n: string, b: { branch: string; path: string; content: string | null; message: string; expected_sha?: string }) =>
    request<{ commit: string }>("PUT", `${r(o, n)}/file`, b),
  commits: (o: string, n: string, ref?: string) => request<Commit[]>("GET", `${r(o, n)}/commits${ref ? `?ref=${enc(ref)}` : ""}`),

  members: (o: string, n: string) => request<{ members: Member[]; inherited: InheritedMember[] }>("GET", `${r(o, n)}/members`),
  resyncMembers: (o: string, n: string, linkId: string) =>
    request<{ members: number; added: number; changed: number; removed: number }>("POST", `${r(o, n)}/links/${linkId}/sync-members`),
  addMember: (o: string, n: string, login: string, role: string) => request("POST", `${r(o, n)}/members`, { login, role }),
  removeMember: (o: string, n: string, handle: string) => request("DELETE", `${r(o, n)}/members/${enc(handle)}`),

  githubRepos: () => request<{ install_url: string; repos: GhRepo[] }>("GET", "/github/repos"),
  link: (o: string, n: string, b: { gh_repo_id: number; installation_id: number; full_name: string; require_check: boolean; agent_instructions: string }) =>
    request<Link & { warning: string | null }>("POST", `${r(o, n)}/links`, b),
  updateLink: (o: string, n: string, id: string, b: Partial<Link>) => request<Link>("PATCH", `${r(o, n)}/links/${id}`, b),
  unlink: (o: string, n: string, id: string) => request<void>("DELETE", `${r(o, n)}/links/${id}`),

  pulls: (o: string, n: string, state = "open") => request<PullSummary[]>("GET", `${r(o, n)}/pulls?state=${state}`),
  createPull: (o: string, n: string, b: { title: string; body: string; branch: string }) => request<PullSummary>("POST", `${r(o, n)}/pulls`, b),
  pull: (o: string, n: string, num: number) => request<Pull>("GET", `${r(o, n)}/pulls/${num}`),
  review: (o: string, n: string, num: number, b: { state: string; body: string; ask_agent: boolean }) =>
    request("POST", `${r(o, n)}/pulls/${num}/reviews`, b),
  rerunAgent: (o: string, n: string, num: number, instructions: string) =>
    request("POST", `${r(o, n)}/pulls/${num}/agent`, { instructions }),
  merge: (o: string, n: string, num: number) => request("POST", `${r(o, n)}/pulls/${num}/merge`),
  close: (o: string, n: string, num: number) => request("POST", `${r(o, n)}/pulls/${num}/close`),
  reopen: (o: string, n: string, num: number) => request("POST", `${r(o, n)}/pulls/${num}/reopen`),
};
