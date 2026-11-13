import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, type Pull } from "../api";
import { AgentBadge, Card, DiffView, ErrorBox, GateBadge, Markdown, Spinner, StateBadge, timeAgo, useAsync } from "../components/ui";

export default function PullView({ owner, name }: { owner: string; name: string }) {
  const number = Number(useParams().number);
  const { data: pr, error, reload } = useAsync(() => api.pull(owner, name, number), [owner, name, number]);
  const [tab, setTab] = useState<"conversation" | "files" | "agent">("conversation");
  const [actionError, setActionError] = useState<string | null>(null);

  // Poll while the agent is working so the page updates by itself.
  const busy = pr?.agent_status === "queued" || pr?.agent_status === "running";
  useEffect(() => {
    if (!busy) return;
    const t = setInterval(reload, 4000);
    return () => clearInterval(t);
  }, [busy, reload]);

  const act = (fn: () => Promise<unknown>) => async () => {
    setActionError(null);
    try { await fn(); await reload(); } catch (e) { setActionError((e as Error).message); }
  };

  if (error) return <ErrorBox error={error} />;
  if (!pr) return <Spinner />;

  return (
    <>
      <h2 className="pr-title">{pr.title} <span className="muted">#{pr.number}</span></h2>
      <div className="row">
        <StateBadge state={pr.state} />
        <span className="muted">
          <code>{pr.head_branch}</code> → <code>{pr.base_branch}</code>
          {pr.author && <> · opened by {pr.author}</>} · {timeAgo(pr.created_at)}
        </span>
        <AgentBadge status={pr.agent_status} />
      </div>

      {pr.code_pr && <CodePrBanner pr={pr} />}
      <ErrorBox error={actionError} />

      <nav className="tabs">
        <button className={tab === "conversation" ? "active" : ""} onClick={() => setTab("conversation")}>Conversation</button>
        <button className={tab === "files" ? "active" : ""} onClick={() => setTab("files")}>Files changed <span className="count">{pr.files.length}</span></button>
        {pr.code_pr && <button className={tab === "agent" ? "active" : ""} onClick={() => setTab("agent")}>Agent log</button>}
      </nav>

      {tab === "conversation" && (
        <div className="stack">
          {pr.agent_summary && (
            <Card title="🤖 DocTheCode Agent">
              <Markdown>{pr.agent_summary}</Markdown>
            </Card>
          )}
          {pr.reviews.map((r) => (
            <Card key={r.id} title={<span className="row">
              {r.avatar_url && <img className="avatar" src={r.avatar_url} alt="" />}
              <strong>{r.user}</strong>
              <span className={`badge review-${r.state}`}>{
                { approved: "approved", changes_requested: "requested changes", commented: "commented" }[r.state]}</span>
              {r.stale && r.state === "approved" && <span className="badge muted" title="The docs changed after this approval">stale</span>}
              {r.addressed_by_agent && <span className="badge muted">sent to agent</span>}
              <span className="muted small">{timeAgo(r.created_at)}</span>
            </span>}>
              {r.body ? <Markdown>{r.body}</Markdown> : <span className="muted">No comment.</span>}
            </Card>
          ))}
          {pr.state === "open" && <ReviewForm pr={pr} owner={owner} name={name} onDone={reload} />}
          <div className="row">
            {pr.state === "open" && (
              <button className="btn btn-primary" disabled={!pr.can_merge} title={pr.merge_blocker ?? ""}
                onClick={act(() => api.merge(owner, name, number))}>Merge</button>
            )}
            {pr.merge_blocker && pr.state === "open" && <span className="muted small">{pr.merge_blocker}</span>}
            <div className="spacer" />
            {pr.state === "open" && pr.role !== "read" && (
              <button className="btn btn-danger" onClick={act(async () => {
                if (pr.code_pr && !confirm("Closing the doc PR fails the check on the code PR, blocking its merge. Continue?")) return;
                await api.close(owner, name, number);
              })}>Close pull request</button>
            )}
            {pr.state === "closed" && (pr.can_reopen
              ? <button className="btn" onClick={act(() => api.reopen(owner, name, number))}>Reopen pull request</button>
              : pr.code_pr && pr.code_pr.state !== "open" &&
                <span className="muted small">Can't be reopened: the code pull request is {pr.code_pr.state} on GitHub.</span>)}
          </div>
        </div>
      )}

      {tab === "files" && (
        <>
          {pr.state === "open" && pr.role !== "read" && (
            <p className="muted small">
              Edit these files on <Link to={`/${owner}/${name}?ref=${encodeURIComponent(pr.head_branch)}`}>{pr.head_branch}</Link>.
              Editing after approval makes the approval stale.
            </p>
          )}
          <DiffView diff={pr.diff} />
        </>
      )}

      {tab === "agent" && <AgentPanel pr={pr} owner={owner} name={name} onDone={reload} />}
    </>
  );
}

function CodePrBanner({ pr }: { pr: Pull }) {
  const c = pr.code_pr!;
  return (
    <div className="banner">
      <div>
        Mirrors <a href={c.url} target="_blank" rel="noreferrer"><strong>{c.full_name}#{c.number}</strong></a>
        <span className="muted"> ({c.state}, head {c.head_sha?.slice(0, 7)})</span>
      </div>
      {pr.state === "open" && <GateBadge gate={pr.gate} />}
      {pr.state === "open" && <div className="muted small">{pr.gate.summary}</div>}
    </div>
  );
}

function ReviewForm({ pr, owner, name, onDone }: { pr: Pull; owner: string; name: string; onDone: () => void }) {
  const [body, setBody] = useState("");
  const [state, setState] = useState<"commented" | "approved" | "changes_requested">("commented");
  const [askAgent, setAskAgent] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const busy = pr.agent_status === "queued" || pr.agent_status === "running";

  async function submit() {
    setError(null);
    try {
      await api.review(owner, name, pr.number, { state, body, ask_agent: !!pr.code_pr && state === "changes_requested" && askAgent });
      setBody("");
      setState("commented");
      onDone();
    } catch (e) { setError((e as Error).message); }
  }

  return (
    <Card title="Review">
      <ErrorBox error={error} />
      <textarea rows={4} placeholder="Leave a comment" value={body} onChange={(e) => setBody(e.target.value)} />
      <div className="radios">
        <label><input type="radio" checked={state === "commented"} onChange={() => setState("commented")} /> Comment</label>
        <label title={pr.review_blocker ?? ""}>
          <input type="radio" disabled={!pr.can_review || busy} checked={state === "approved"} onChange={() => setState("approved")} /> Approve
        </label>
        <label title={pr.review_blocker ?? ""}>
          <input type="radio" disabled={!pr.can_review} checked={state === "changes_requested"} onChange={() => setState("changes_requested")} /> Request changes
        </label>
        {pr.code_pr && state === "changes_requested" && (
          <label className="indent"><input type="checkbox" checked={askAgent} onChange={(e) => setAskAgent(e.target.checked)} /> Ask the agent to address this feedback</label>
        )}
      </div>
      {!pr.can_review && pr.review_blocker && <p className="muted small">{pr.review_blocker}</p>}
      {busy && state === "approved" && <p className="muted small">Wait for the agent to finish before approving.</p>}
      <button className="btn btn-primary" onClick={submit} disabled={state !== "approved" && !body.trim()}>Submit review</button>
    </Card>
  );
}

function AgentPanel({ pr, owner, name, onDone }: { pr: Pull; owner: string; name: string; onDone: () => void }) {
  const [instructions, setInstructions] = useState("");
  const [error, setError] = useState<string | null>(null);
  const busy = pr.agent_status === "queued" || pr.agent_status === "running";
  return (
    <div className="stack">
      {pr.state === "open" && pr.role !== "read" && (
        <Card title="Run the agent again">
          <ErrorBox error={error} />
          <textarea rows={3} placeholder="Optional extra instructions, e.g. 'also add an example to the CLI page'"
            value={instructions} onChange={(e) => setInstructions(e.target.value)} />
          <button className="btn" disabled={busy} onClick={async () => {
            try { await api.rerunAgent(owner, name, pr.number, instructions); setInstructions(""); onDone(); }
            catch (e) { setError((e as Error).message); }
          }}>{busy ? "Agent is running…" : "Re-run agent"}</button>
        </Card>
      )}
      <pre className="log">{pr.agent_log.length ? pr.agent_log.join("\n") : "No agent runs yet."}</pre>
    </div>
  );
}
