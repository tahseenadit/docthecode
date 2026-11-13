import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { AgentBadge, CheckGitHubButton, ErrorBox, GateBadge, Spinner, StateBadge, timeAgo, useAsync } from "../components/ui";

export default function RepoPulls({ owner, name }: { owner: string; name: string }) {
  const [state, setState] = useState("open");
  const { data: pulls, error, loading, reload } = useAsync(() => api.pulls(owner, name, state), [owner, name, state]);
  const [showNew, setShowNew] = useState(false);
  return (
    <>
      <div className="row between">
        <div className="segmented cap">
          {["open", "merged", "closed", "all"].map((s) => (
            <button key={s} className={s === state ? "on" : ""} onClick={() => setState(s)}>{s}</button>
          ))}
        </div>
        <div className="row">
          <CheckGitHubButton run={() => api.checkGitHub(owner, name)} onDone={() => setTimeout(reload, 1500)} />
          <button className="btn" onClick={() => setShowNew(!showNew)}>New pull request</button>
        </div>
      </div>
      {showNew && <NewPull owner={owner} name={name} />}
      <ErrorBox error={error} />
      {loading && <Spinner />}
      {pulls?.length === 0 && (
        <div className="empty">
          <p>No {state === "all" ? "" : state} pull requests.</p>
          <p className="muted">Doc PRs are opened automatically when a PR is opened on a connected GitHub repository.</p>
        </div>
      )}
      <ul className="list">
        {pulls?.map((p) => (
          <li key={p.number}>
            <div className="row">
              <StateBadge state={p.state} />
              <Link className="strong" to={`${p.number}`}>{p.title}</Link>
              <span className="muted">#{p.number}</span>
            </div>
            <div className="row small muted">
              {p.code_pr && <a href={p.code_pr.url} target="_blank" rel="noreferrer">⇄ {p.code_pr.full_name}#{p.code_pr.number}</a>}
              <span>{p.author ? `by ${p.author}` : ""} · updated {timeAgo(p.updated_at)}</span>
              <AgentBadge status={p.agent_status} />
              {p.state === "open" && p.code_pr && <GateBadge gate={p.gate} />}
            </div>
          </li>
        ))}
      </ul>
    </>
  );
}

function NewPull({ owner, name }: { owner: string; name: string }) {
  const nav = useNavigate();
  const [title, setTitle] = useState("");
  const [branch, setBranch] = useState("docs/");
  const [error, setError] = useState<string | null>(null);
  return (
    <form className="card stack pad" onSubmit={async (e) => {
      e.preventDefault();
      try {
        const pr = await api.createPull(owner, name, { title, body: "", branch });
        nav(`${pr.number}`);
      } catch (err) { setError((err as Error).message); }
    }}>
      <ErrorBox error={error} />
      <input required placeholder="Title" value={title} onChange={(e) => setTitle(e.target.value)} />
      <input required placeholder="Branch (created from the default branch if new)" value={branch} onChange={(e) => setBranch(e.target.value)} />
      <p className="muted small">Edit files on this branch from the Docs tab, then ask for review here.</p>
      <button className="btn btn-primary">Create pull request</button>
    </form>
  );
}
