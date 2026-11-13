import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { AgentBadge, CheckGitHubButton, ErrorBox, GateBadge, Spinner, timeAgo, useAsync, useMe } from "../components/ui";

export default function Dashboard() {
  const me = useMe();
  const repos = useAsync(() => api.repos(), []);
  const inbox = useAsync(() => api.inbox(), []);
  const [syncing, setSyncing] = useState(false);
  const [filter, setFilter] = useState<"all" | "review" | "mine">("all");

  async function refreshAccess() {
    setSyncing(true);
    try { await api.syncAccess(); await Promise.all([repos.reload(), inbox.reload()]); } finally { setSyncing(false); }
  }

  const items = (inbox.data ?? []).filter((p) => filter === "all" || (filter === "review" ? p.needs_review : p.is_mine));

  return (
    <div className="stack">
      <div className="row between">
        <h2>Open documentation pull requests</h2>
        <div className="row">
          <div className="segmented">
            <button className={filter === "all" ? "on" : ""} onClick={() => setFilter("all")}>All</button>
            <button className={filter === "review" ? "on" : ""} onClick={() => setFilter("review")}>Needs review</button>
            <button className={filter === "mine" ? "on" : ""} onClick={() => setFilter("mine")}>My PRs</button>
          </div>
          <button className="btn" onClick={refreshAccess} disabled={syncing}
            title="Re-read which GitHub repositories you can access">{syncing ? "Refreshing…" : "↻ Refresh access"}</button>
          <CheckGitHubButton run={api.checkGitHubAll} onDone={() => setTimeout(inbox.reload, 1500)} />
        </div>
      </div>
      <ErrorBox error={inbox.error} />
      {inbox.loading && <Spinner />}
      {inbox.data && items.length === 0 && (
        <div className="empty"><p className="muted">Nothing here. Doc PRs appear when someone opens a PR on a connected GitHub repository
          (if GitHub can't reach DocTheCode, click “Check GitHub for changes”).</p></div>
      )}
      <ul className="list">
        {items.map((p) => (
          <li key={`${p.repo}#${p.number}`}>
            <div className="row">
              <Link className="strong" to={`/${p.repo}/pulls/${p.number}`}>{p.title}</Link>
              <span className="muted">{p.repo}#{p.number}</span>
              {p.is_mine && <span className="chip">your PR</span>}
            </div>
            <div className="row small muted">
              {p.code_pr && <a href={p.code_pr.url} target="_blank" rel="noreferrer">⇄ {p.code_pr.full_name}#{p.code_pr.number}</a>}
              <span>updated {timeAgo(p.updated_at)}</span>
              <AgentBadge status={p.agent_status} />
              {p.code_pr && <GateBadge gate={p.gate} />}
            </div>
          </li>
        ))}
      </ul>

      <div className="row between">
        <h2>Documentation repositories</h2>
        <Link className="btn btn-primary" to="/new">New repository</Link>
      </div>
      <ErrorBox error={repos.error} />
      {repos.loading && <Spinner />}
      {repos.data?.length === 0 && (
        <div className="empty">
          <p>No doc repositories yet.</p>
          <p className="muted">Repositories connected to GitHub repos you can access ({me.host.name}) show up here automatically.
            Or create one and connect it in its settings.</p>
        </div>
      )}
      <ul className="list">
        {repos.data?.map((r) => (
          <li key={r.id}>
            <Link to={`/${r.full_name}`} className="strong">{r.full_name}</Link>
            {r.description && <p className="muted">{r.description}</p>}
            <div className="row small muted">
              {r.links.length ? r.links.map((l) => <span key={l.id} className="chip">⇄ {l.full_name}</span>)
                : <span>Not connected to GitHub</span>}
              {!!r.open_prs && <span>{r.open_prs} open PR{r.open_prs > 1 ? "s" : ""}</span>}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
