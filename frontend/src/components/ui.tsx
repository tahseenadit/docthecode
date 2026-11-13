import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import { Link } from "react-router-dom";
import remarkGfm from "remark-gfm";
import type { CheckResult, Gate, Me, PullSummary } from "../api";

export const MeContext = createContext<Me | null>(null);
export const useMe = () => useContext(MeContext)!;

/** Load data, expose reload(). Re-runs whenever deps change. */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const load = useCallback(fn, deps);
  const reload = useCallback(async () => {
    try {
      setData(await load());
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [load]);
  useEffect(() => {
    setLoading(true);
    reload();
  }, [reload]);
  return { data, error, loading, reload, setData };
}

/** Renders Markdown. `onPageLink` turns relative links to other .md pages into in-app navigation. */
export function Markdown({ children, onPageLink }: { children: string; onPageLink?: (href: string) => string | null }) {
  return (
    <div className="markdown">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={onPageLink ? {
        a: ({ href, children: text }) => {
          const internal = href ? onPageLink(href) : null;
          return internal
            ? <Link to={internal}>{text}</Link>
            : <a href={href} target="_blank" rel="noreferrer">{text}</a>;
        },
      } : undefined}>{children}</ReactMarkdown>
    </div>
  );
}

export function StateBadge({ state }: { state: PullSummary["state"] }) {
  const label = { open: "Open", merged: "Merged", closed: "Closed" }[state];
  return <span className={`badge state-${state}`}>{label}</span>;
}

export function GateBadge({ gate }: { gate: Gate }) {
  const kind =
    gate.status !== "completed" ? "pending" : gate.conclusion === "success" || gate.conclusion === "neutral" ? "pass" : "fail";
  const icon = { pending: "●", pass: "✓", fail: "✕" }[kind];
  return (
    <span className={`gate gate-${kind}`} title={gate.summary}>
      {icon} {gate.title}
    </span>
  );
}

export function AgentBadge({ status }: { status: PullSummary["agent_status"] }) {
  if (status === "idle") return null;
  const text = {
    queued: "Agent queued", running: "Agent writing…", done: "Agent drafted", failed: "Agent failed", no_changes: "No changes needed",
  }[status];
  return <span className={`badge agent-${status}`}>{text}</span>;
}

export function ErrorBox({ error }: { error: string | null }) {
  return error ? <div className="error">{error}</div> : null;
}

export function Spinner() {
  return <div className="muted pad">Loading…</div>;
}

export function Card({ title, actions, children }: { title?: ReactNode; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="card">
      {(title || actions) && (
        <header className="card-head">
          <h3>{title}</h3>
          <div className="row">{actions}</div>
        </header>
      )}
      <div className="card-body">{children}</div>
    </section>
  );
}

export function timeAgo(iso: string | number) {
  const t = typeof iso === "number" ? iso * 1000 : Date.parse(iso);
  const s = Math.max(1, Math.round((Date.now() - t) / 1000));
  const units: [number, string][] = [[86400 * 30, "mo"], [86400, "d"], [3600, "h"], [60, "m"]];
  for (const [n, u] of units) if (s >= n) return `${Math.floor(s / n)}${u} ago`;
  return `${s}s ago`;
}

/** Unified diff, split per file, with add/remove line colouring. */
export function DiffView({ diff }: { diff: string }) {
  if (!diff.trim()) return <p className="muted pad">No changes yet.</p>;
  const files = diff.split(/^(?=diff --git )/m).filter(Boolean);
  return (
    <div className="diff">
      {files.map((f, i) => {
        const lines = f.split("\n");
        const name = (lines[0].match(/ b\/(.+)$/) ?? [])[1] ?? lines[0];
        const body = lines.slice(lines.findIndex((l) => l.startsWith("@@")));
        return (
          <details key={i} open className="diff-file">
            <summary>{name}</summary>
            <pre>
              {body.map((l, j) => (
                <div key={j} className={l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : l.startsWith("@@") ? "hunk" : ""}>
                  {l || " "}
                </div>
              ))}
            </pre>
          </details>
        );
      })}
    </div>
  );
}

const CHANGE_LABELS: Record<string, [string, string]> = {
  opened: ["new pull request", "new pull requests"], synchronize: ["pull request with new commits", "pull requests with new commits"],
  closed: ["merged or closed", "merged or closed"], reopened: ["reopened", "reopened"], edited: ["renamed", "renamed"],
};

export function describeCheck(r: CheckResult): string {
  if (r.repos === 0) return "No connected GitHub repositories to check.";
  const parts = Object.entries(r.changes).map(([k, n]) => `${n} ${(CHANGE_LABELS[k] ?? [k, k])[n === 1 ? 0 : 1]}`);
  return parts.length ? `Found ${parts.join(", ")}. Doc PRs are being updated.` : "Everything is up to date.";
}

/** "Check GitHub for changes": for setups where GitHub can't send webhooks to DocTheCode. */
export function CheckGitHubButton({ run, onDone }: { run: () => Promise<CheckResult>; onDone?: () => void }) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ text: string; error?: boolean } | null>(null);
  return (
    <span className="row">
      <button className="btn" disabled={busy} title="Ask GitHub for new, updated, merged and closed pull requests" onClick={async () => {
        setBusy(true); setMsg(null);
        try {
          const r = await run();
          setMsg(r.errors.length ? { text: `${describeCheck(r)} Problems: ${r.errors.join("; ")}`, error: true } : { text: describeCheck(r) });
          onDone?.();
        } catch (e) { setMsg({ text: (e as Error).message, error: true }); } finally { setBusy(false); }
      }}>{busy ? "Checking GitHub…" : "↻ Check GitHub for changes"}</button>
      {msg && <span className={`small ${msg.error ? "gate-fail" : "muted"}`}>{msg.text}</span>}
    </span>
  );
}
