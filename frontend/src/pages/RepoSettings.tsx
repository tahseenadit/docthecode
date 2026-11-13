import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, type Link as RepoLink, type Repo } from "../api";
import { Card, ErrorBox, Spinner, timeAgo, useAsync } from "../components/ui";

export default function RepoSettings({ owner, name }: { owner: string; name: string }) {
  const { data: repo, error, reload } = useAsync(() => api.repo(owner, name), [owner, name]);
  if (error) return <ErrorBox error={error} />;
  if (!repo) return <Spinner />;
  const admin = repo.role === "admin";
  return (
    <div className="stack">
      <Card title="Connected GitHub repositories">
        <p className="muted small">
          When a pull request is opened on a connected repository, DocTheCode opens a matching doc PR here, the agent drafts
          the documentation changes, and the GitHub check <code>DocTheCode / documentation approved</code> stays pending until
          the doc PR is approved.
        </p>
        {repo.links.map((l) => <LinkRow key={l.id} repo={repo} link={l} admin={admin} onChange={reload} />)}
        {admin && <ConnectRepo repo={repo} onDone={reload} />}
      </Card>
      <Members key={repo.links.map((l) => `${l.id}:${l.members_synced_at}`).join()} owner={owner} name={name} admin={admin}
        hasLinks={repo.links.length > 0} />
      {admin && <DangerZone repo={repo} />}
    </div>
  );
}

function LinkRow({ repo, link, admin, onChange }: { repo: Repo; link: RepoLink; admin: boolean; onChange: () => void }) {
  const [draft, setDraft] = useState(link);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const save = async (patch: Partial<RepoLink>) => {
    try { await api.updateLink(repo.owner, repo.name, link.id, patch); onChange(); } catch (e) { setError((e as Error).message); }
  };
  return (
    <div className="link-row">
      <div className="row between">
        <div className="row">
          <a href={link.url} target="_blank" rel="noreferrer" className="strong">{link.full_name}</a>
          <span className="chip">{link.host_name}</span>
          {!link.enabled && <span className="badge muted">paused</span>}
        </div>
        {admin && <div className="row">
          <button className="btn" onClick={async () => {
            setError(null);
            try { await api.resyncMembers(repo.owner, repo.name, link.id); onChange(); }
            catch (e) { setError((e as Error).message); }
          }}>Re-sync members</button>
          <button className="btn" onClick={() => setOpen(!open)}>{open ? "Hide" : "Configure"}</button>
          <button className="btn btn-danger" onClick={async () => {
            if (confirm(`Disconnect ${link.full_name}? Remove the required check from its branch protection too.`)) {
              await api.unlink(repo.owner, repo.name, link.id); onChange();
            }
          }}>Disconnect</button>
        </div>}
      </div>
      <div className="muted small">
        {link.members_count} member{link.members_count === 1 ? "" : "s"} inherited
        {link.members_synced_at ? ` · synced ${timeAgo(link.members_synced_at)}` : " · not synced yet"}
        {link.members_sync_error && <span className="gate-fail"> · last sync failed: {link.members_sync_error}</span>}
      </div>
      {!open && <ErrorBox error={error} />}
      {open && (
        <div className="stack pad">
          <ErrorBox error={error} />
          <label className="check"><input type="checkbox" checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} /> Enabled</label>
          <label>Required approvals
            <input type="number" min={1} max={10} value={draft.required_approvals}
              onChange={(e) => setDraft({ ...draft, required_approvals: Number(e.target.value) })} />
          </label>
          <label className="check"><input type="checkbox" checked={draft.pass_when_no_changes}
            onChange={(e) => setDraft({ ...draft, pass_when_no_changes: e.target.checked })} /> Pass the check automatically when the agent finds nothing to document</label>
          <label className="check"><input type="checkbox" checked={draft.allow_author_approval}
            onChange={(e) => setDraft({ ...draft, allow_author_approval: e.target.checked })} /> The code PR author can approve their own docs</label>
          <label>Agent instructions
            <textarea rows={5} placeholder="e.g. Public API docs live in docs/api/. Use second person. Every CLI flag needs an example."
              value={draft.agent_instructions} onChange={(e) => setDraft({ ...draft, agent_instructions: e.target.value })} />
          </label>
          <button className="btn btn-primary" onClick={() => save({
            enabled: draft.enabled, required_approvals: draft.required_approvals, pass_when_no_changes: draft.pass_when_no_changes,
            allow_author_approval: draft.allow_author_approval, agent_instructions: draft.agent_instructions,
          })}>Save</button>
        </div>
      )}
    </div>
  );
}

function ConnectRepo({ repo, onDone }: { repo: Repo; onDone: () => void }) {
  const [show, setShow] = useState(false);
  const { data, error, loading, reload } = useAsync(() => (show ? api.githubRepos() : Promise.resolve(null)), [show]);
  const [filter, setFilter] = useState("");
  const [requireCheck, setRequireCheck] = useState(true);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  if (!show) return <button className="btn btn-primary" onClick={() => setShow(true)}>Connect a GitHub repository</button>;
  const repos = (data?.repos ?? []).filter((r) => r.full_name.toLowerCase().includes(filter.toLowerCase()));
  return (
    <div className="stack pad connect">
      <ErrorBox error={error ?? err} />
      {msg && <div className="notice">{msg}</div>}
      <div className="row">
        <input className="grow" placeholder="Filter repositories" value={filter} onChange={(e) => setFilter(e.target.value)} />
        {data && <a className="btn" href={data.install_url} target="_blank" rel="noreferrer">Install app on more repos ↗</a>}
        <button className="btn" onClick={reload}>Refresh</button>
      </div>
      <label className="check"><input type="checkbox" checked={requireCheck} onChange={(e) => setRequireCheck(e.target.checked)} />
        Make the DocTheCode check required on the default branch (needs admin on the GitHub repo)</label>
      {loading && <Spinner />}
      {data && repos.length === 0 && <p className="muted">No repositories found. Install the DocTheCode GitHub App on the repository first.</p>}
      <ul className="list compact">
        {repos.map((r) => (
          <li key={r.id} className="row between">
            <span>{r.private ? "🔒" : "📦"} {r.full_name}</span>
            {r.linked_doc_repo_id ? (
              <span className="muted small">{r.linked_doc_repo_id === repo.id ? "connected" : "connected to another doc repo"}</span>
            ) : (
              <button className="btn" onClick={async () => {
                setErr(null);
                try {
                  const res = await api.link(repo.owner, repo.name, {
                    gh_repo_id: r.id, installation_id: r.installation_id, full_name: r.full_name,
                    require_check: requireCheck && r.can_admin, agent_instructions: "",
                  });
                  setMsg(res.warning ?? `Connected ${r.full_name}.` + (!r.can_admin && requireCheck
                    ? " You are not an admin there, so ask an admin to require the DocTheCode check in branch protection." : ""));
                  onDone(); reload();
                } catch (e) { setErr((e as Error).message); }
              }}>Connect</button>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

function Members({ owner, name, admin, hasLinks }: { owner: string; name: string; admin: boolean; hasLinks: boolean }) {
  const { data, reload } = useAsync(() => api.members(owner, name), [owner, name]);
  const [login, setLogin] = useState("");
  const [role, setRole] = useState("write");
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const inherited = (data?.inherited ?? []).filter((m) => m.handle.toLowerCase().includes(filter.toLowerCase()));
  return (
    <Card title="Members">
      <ErrorBox error={error} />
      {hasLinks && (
        <>
          <p className="muted small">Everyone with access to a connected GitHub repository is a member automatically, with the
            matching role (GitHub admin → admin; maintain/write → write; triage/read → read). No invites needed: they have access
            the first time they sign in. The list follows GitHub as people join, leave or change permission.</p>
          <div className="row between">
            <strong>From GitHub ({data?.inherited.length ?? 0})</strong>
            {(data?.inherited.length ?? 0) > 8 && <input placeholder="Filter" value={filter} onChange={(e) => setFilter(e.target.value)} />}
          </div>
          <ul className="list compact scroll">
            {inherited.map((m) => (
              <li key={m.handle} className="row between">
                <span className="row">{m.avatar_url && <img className="avatar" src={m.avatar_url} alt="" />}{m.handle}
                  {!m.signed_in && <span className="muted small">hasn't signed in yet</span>}</span>
                <span className="row"><span className="muted small" title={`via ${m.source}`}>GitHub {m.github_permission}</span>
                  <span className="chip">{m.role}</span></span>
              </li>
            ))}
            {data && data.inherited.length === 0 && <li className="muted small">No one imported yet. Use “Re-sync members” on the connected repository above.</li>}
          </ul>
        </>
      )}
      <strong>Added in DocTheCode</strong>
      <p className="muted small">For people who should see these docs without access to the code repository.</p>
      <ul className="list compact">
        {data?.members.map((m) => (
          <li key={m.handle} className="row between">
            <span className="row">{m.avatar_url && <img className="avatar" src={m.avatar_url} alt="" />}{m.handle}</span>
            <span className="row"><span className="chip">{m.role}</span>
              {admin && m.role !== "owner" && <button className="btn-link" onClick={() => api.removeMember(owner, name, m.handle).then(reload)}>Remove</button>}
            </span>
          </li>
        ))}
      </ul>
      {admin && (
        <div className="row">
          <input className="grow" placeholder="GitHub login (login@host for enterprise users)" value={login} onChange={(e) => setLogin(e.target.value)} />
          <select value={role} onChange={(e) => setRole(e.target.value)}>
            <option>read</option><option>write</option><option>admin</option>
          </select>
          <button className="btn" disabled={!login} onClick={async () => {
            try { await api.addMember(owner, name, login, role); setLogin(""); setError(null); reload(); }
            catch (e) { setError((e as Error).message); }
          }}>Add</button>
        </div>
      )}
    </Card>
  );
}

function DangerZone({ repo }: { repo: Repo }) {
  const nav = useNavigate();
  return (
    <Card title="Danger zone">
      <button className="btn btn-danger" onClick={async () => {
        if (prompt(`Type ${repo.name} to delete this repository and all its history.`) === repo.name) {
          await api.deleteRepo(repo.owner, repo.name);
          nav("/");
        }
      }}>Delete repository</button>
    </Card>
  );
}
