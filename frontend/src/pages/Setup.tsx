import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, type SetupStatus } from "../api";
import { AgentForm, CreateAppForm, HostForm } from "../components/settings";
import { Card, ErrorBox, Spinner, useAsync } from "../components/ui";

/** First-run setup: optional setup token, connect GitHub, choose the AI provider, sign in. */
export default function Setup({ onChanged }: { onChanged?: () => void }) {
  const { data: status, reload } = useAsync<SetupStatus>(() => api.setupStatus(), []);
  const [params] = useSearchParams();
  const created = params.get("created");

  if (!status) return <Spinner />;
  if (!status.needs_setup) {
    return (
      <div className="login">
        <h1>📚 DocTheCode</h1>
        <p>DocTheCode is set up.</p>
        <a className="btn btn-primary btn-lg" href="/">Continue</a>
      </div>
    );
  }

  return (
    <div className="container narrow-wide stack">
      <h1>Set up DocTheCode <span className="muted small">version {status.version}</span></h1>
      <p className="muted small">DocTheCode's address: <code>{status.public_url}</code> (detected from this browser; change it later under Admin).</p>
      {!status.unlocked ? <Unlock onDone={reload} /> : (
        <>
          <GitHubStep status={status} created={created} onChanged={() => { reload(); onChanged?.(); }} />
          {status.hosts.length > 0 && (
            <Card title={<>2. Choose the AI for the documentation agent {status.agent_configured && "✓"}</>}>
              <AgentConfig onSaved={reload} />
            </Card>
          )}
          {status.hosts.length > 0 && (
            <Card title="3. Sign in to finish">
              <p className="muted">Sign in now and you become DocTheCode's first admin. Everyone else just signs in with GitHub;
                they'll automatically see the doc repos connected to GitHub repositories they have access to.</p>
              {!status.agent_configured && <p className="muted small">You can choose the AI provider later under Admin.</p>}
              {status.telemetry_active && <p className="muted small">DocTheCode shares anonymous usage statistics and error reports with its developers.
                Documentation, code and names never leave this server. You can review exactly what is sent, or switch it off, under Admin → Usage data.</p>}
              <div className="row">
                {status.hosts.map((h) => (
                  <a key={h.id} className="btn btn-primary" href={`/api/auth/login/${h.id}?next=/admin`}>Sign in with {h.name}</a>
                ))}
              </div>
            </Card>
          )}
        </>
      )}
    </div>
  );
}

function Unlock({ onDone }: { onDone: () => void }) {
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  return (
    <Card title="Enter the setup token">
      <p className="muted">This DocTheCode server requires a setup token: the <code>DOCTHECODE_SETUP_TOKEN</code> value it was started with.</p>
      <ErrorBox error={error} />
      <form className="row" onSubmit={async (e) => {
        e.preventDefault();
        try { await api.unlock(token); onDone(); } catch (err) { setError((err as Error).message); }
      }}>
        <input className="grow" autoFocus value={token} onChange={(e) => setToken(e.target.value)} placeholder="setup token" />
        <button className="btn btn-primary" disabled={!token.trim()}>Continue</button>
      </form>
    </Card>
  );
}

function GitHubStep({ status, created, onChanged }: { status: SetupStatus; created: string | null; onChanged: () => void }) {
  const [mode, setMode] = useState<"auto" | "manual">("auto");
  const { data: config, reload } = useAsync(() => api.adminConfig(), [status.hosts.length]);
  const [adding, setAdding] = useState(false);
  const [removeError, setRemoveError] = useState<string | null>(null);
  const hosts = config?.hosts ?? [];

  return (
    <Card title={<>1. Connect GitHub {hosts.length > 0 && "✓"}</>}>
      {created && <div className="notice">GitHub App created and its credentials saved. Now install it on the repositories you want to document.</div>}
      <ErrorBox error={removeError} />
      {hosts.map((h) => (
        <div key={h.id} className="link-row stack">
          <div className="row between">
            <span><strong>Connected to {h.name}</strong> <span className="muted small">({h.web_url})</span></span>
            <span className="row">
              <a className="btn" href={`${h.app_url}/installations/new`} target="_blank" rel="noreferrer">Install app on repositories ↗</a>
              <button className="btn btn-danger" onClick={async () => {
                if (!confirm(`Remove the connection to ${h.name}? DocTheCode forgets this GitHub App's credentials. The app itself stays on GitHub; delete it there too if you no longer need it.`)) return;
                setRemoveError(null);
                try { await api.deleteHost(h.id); reload(); onChanged(); } catch (e) { setRemoveError((e as Error).message); }
              }}>Remove</button>
            </span>
          </div>
          <div className="muted small">GitHub App <code>{h.app_slug}</code></div>
        </div>
      ))}
      {hosts.length > 0 && !adding && (
        <button className="btn" onClick={() => setAdding(true)}>Connect GitHub Enterprise Server too</button>
      )}
      {(hosts.length === 0 || adding) && (
        <>
          <p className="muted">DocTheCode talks to GitHub through a GitHub App. Let DocTheCode create one for you, or enter one you already made.</p>
          <div className="segmented">
            <button className={mode === "auto" ? "on" : ""} onClick={() => setMode("auto")}>Create automatically</button>
            <button className={mode === "manual" ? "on" : ""} onClick={() => setMode("manual")}>I already have an app</button>
          </div>
          {mode === "auto"
            ? <CreateAppForm publicUrl={status.public_url} publicUrlIsLocal={status.public_url_is_local} existingIds={hosts.map((h) => h.id)} />
            : (
              <>
                <p className="muted small">In your app's settings set the callback URL to <code>{status.callback_url}</code>. A webhook is optional:
                  without one, use “Check GitHub for changes” in DocTheCode to pick up pull requests.</p>
                <HostForm onSaved={() => { setAdding(false); reload(); onChanged(); }} />
              </>
            )}
        </>
      )}
    </Card>
  );
}

function AgentConfig({ onSaved }: { onSaved: () => void }) {
  const { data, error, reload } = useAsync(() => api.adminConfig(), []);
  if (error) return <ErrorBox error={error} />;
  if (!data) return <Spinner />;
  return <AgentForm current={data.agent} onSaved={() => { reload(); onSaved(); }} />;
}
