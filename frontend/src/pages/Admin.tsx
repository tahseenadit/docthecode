import { useState } from "react";
import { api, type AdminConfig, type HostAdmin } from "../api";
import { AgentForm, CreateAppForm, HostForm, StorageNotice } from "../components/settings";
import { Card, ErrorBox, Spinner, timeAgo, useAsync, useMe } from "../components/ui";

export default function Admin() {
  const me = useMe();
  const { data, error, reload } = useAsync(() => api.adminConfig(), []);
  const { data: status } = useAsync(() => api.setupStatus(), []);
  if (!me.is_admin) return <ErrorBox error="Only DocTheCode admins can open this page." />;
  if (error) return <ErrorBox error={error} />;
  if (!data || !status) return <Spinner />;

  return (
    <div className="stack">
      <h2>DocTheCode settings</h2>
      <p className="muted small">Secrets are stored encrypted in the database with the server's <code>DOCTHECODE_SECRET_KEY</code> and are never sent back to the browser.</p>

      <Card title="This server">
        <StorageNotice storage={data.storage} />
        <PublicUrl current={data.public_url} locked={data.public_url_locked} onSaved={reload} />
      </Card>

      <Card title="GitHub connections">
        <p className="muted small">OAuth callback URL for every app: <code>{data.callback_url}</code></p>
        <GitHubEvents events={data.github_events} hosts={data.hosts.map((h) => h.id)} />
        {data.hosts.map((h) => <HostRow key={h.id} host={h} onChange={reload} />)}
        <AddHost publicUrl={status.public_url} local={status.public_url_is_local} existing={data.hosts.map((h) => h.id)} onAdded={reload} />
      </Card>

      <Card title="Documentation agent">
        <AgentForm current={data.agent} onSaved={reload} />
      </Card>

      <UsageData />

      <Admins admins={data.admins} me={me.handle} onChange={reload} />
    </div>
  );
}

function HostRow({ host, onChange }: { host: HostAdmin; onChange: () => void }) {
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <div className="link-row stack">
      <div className="row between">
        <div className="row"><strong>{host.name}</strong><span className="chip">{host.web_url}</span></div>
        <div className="row">
          <a className="btn" href={`${host.app_url}/installations/new`} target="_blank" rel="noreferrer">Install on repos ↗</a>
          <button className="btn" onClick={() => setEditing(!editing)}>{editing ? "Close" : "Edit"}</button>
          <button className="btn btn-danger" onClick={async () => {
            if (!confirm(`Remove ${host.name}? Users of this host will no longer be able to sign in.`)) return;
            try { await api.deleteHost(host.id); onChange(); } catch (e) { setError((e as Error).message); }
          }}>Remove</button>
        </div>
      </div>
      <ErrorBox error={error} />
      <div className="muted small">
        App <code>{host.app_slug}</code> (ID {host.app_id}) · client secret {host.client_secret ?? "missing"} ·
        webhook secret {host.webhook_secret ?? "missing"} · private key {host.private_key ?? "missing"}
      </div>
      <div className="muted small">Webhook URL: <code>{host.webhook_url}</code></div>
      {editing && <HostForm existing={host} onSaved={onChange} />}
    </div>
  );
}

function AddHost({ publicUrl, local, existing, onAdded }: { publicUrl: string; local: boolean; existing: string[]; onAdded: () => void }) {
  const [mode, setMode] = useState<null | "auto" | "manual">(null);
  if (!mode) {
    return (
      <div className="row">
        <button className="btn" onClick={() => setMode("auto")}>Create a new GitHub App</button>
        <button className="btn" onClick={() => setMode("manual")}>Add an existing GitHub App</button>
      </div>
    );
  }
  return (
    <div className="connect pad stack">
      <button className="btn-link" onClick={() => setMode(null)}>← Cancel</button>
      {mode === "auto"
        ? <CreateAppForm publicUrl={publicUrl} publicUrlIsLocal={local} existingIds={existing} />
        : <HostForm onSaved={() => { setMode(null); onAdded(); }} />}
    </div>
  );
}

function Admins({ admins, me, onChange }: { admins: { handle: string; avatar_url: string | null }[]; me: string; onChange: () => void }) {
  const [handle, setHandle] = useState("");
  const [error, setError] = useState<string | null>(null);
  return (
    <Card title="Admins">
      <p className="muted small">Admins manage these settings. People must sign in once before they can be made admin.</p>
      <ErrorBox error={error} />
      <ul className="list compact">
        {admins.map((a) => (
          <li key={a.handle} className="row between">
            <span className="row">{a.avatar_url && <img className="avatar" src={a.avatar_url} alt="" />}{a.handle}</span>
            {a.handle !== me && <button className="btn-link" onClick={() => api.removeAdmin(a.handle).then(onChange)}>Remove</button>}
          </li>
        ))}
      </ul>
      <form className="row" onSubmit={async (e) => {
        e.preventDefault();
        try { await api.addAdmin(handle); setHandle(""); setError(null); onChange(); } catch (err) { setError((err as Error).message); }
      }}>
        <input className="grow" placeholder="GitHub login (login@host for enterprise users)" value={handle} onChange={(e) => setHandle(e.target.value)} />
        <button className="btn" disabled={!handle.trim()}>Make admin</button>
      </form>
    </Card>
  );
}

function UsageData() {
  const { data, error, setData } = useAsync(() => api.telemetryStatus(), []);
  const [saving, setSaving] = useState(false);
  if (error) return <ErrorBox error={error} />;
  if (!data) return null;
  const effective = data.enabled && !data.server_disabled && data.collector_configured;
  return (
    <Card title="Usage data">
      <p className="muted">DocTheCode can send anonymous usage statistics and error reports to its developers so they can
        see which features are used and fix problems. Your documentation, code and names never leave this server.</p>
      <label className="check">
        <input type="checkbox" checked={data.enabled} disabled={saving || data.server_disabled}
          onChange={async (e) => { setSaving(true); try { setData(await api.setTelemetry(e.target.checked)); } finally { setSaving(false); } }} />
        Share anonymous usage data
      </label>
      {data.server_disabled && <p className="muted small">Turned off for this whole server (<code>DOCTHECODE_TELEMETRY=off</code>).</p>}
      {!data.collector_configured && <p className="muted small">This build has no usage-data destination configured, so nothing is sent.</p>}
      <div className="split-2">
        <div><strong>Sent</strong><ul className="small">{data.what_is_sent.map((t) => <li key={t}>{t}</li>)}</ul></div>
        <div><strong>Never sent</strong><ul className="small">{data.never_sent.map((t) => <li key={t}>{t}</li>)}</ul></div>
      </div>
      <p className="muted small">
        Server ID <code>{data.instance_id}</code>
        {effective && (data.last_sent_at ? <> · last sent {new Date(data.last_sent_at * 1000).toLocaleString()}</> : <> · nothing sent yet</>)}
        {data.last_error && <> · last attempt failed: {data.last_error}</>}
      </p>
      {data.last_batch != null && (
        <details><summary>Show exactly what was sent last time</summary>
          <pre className="log">{JSON.stringify(data.last_batch, null, 2)}</pre>
        </details>
      )}
    </Card>
  );
}

function PublicUrl({ current, locked, onSaved }: { current: string; locked: boolean; onSaved: () => void }) {
  const [url, setUrl] = useState(current);
  const [error, setError] = useState<string | null>(null);
  if (locked) return <p className="muted small">Address: <code>{current}</code> (set by <code>DOCTHECODE_PUBLIC_URL</code> on the server).</p>;
  return (
    <form className="stack" onSubmit={async (e) => {
      e.preventDefault();
      setError(null);
      try { await api.savePublicUrl(url.trim().replace(/\/$/, "")); onSaved(); } catch (err) { setError((err as Error).message); }
    }}>
      <label>DocTheCode's address
        <div className="row">
          <input className="grow" value={url} onChange={(e) => setUrl(e.target.value)} pattern="https?://[^/\s]+" />
          <button className="btn" disabled={url.trim() === current}>Save</button>
        </div>
      </label>
      <p className="muted small">Detected automatically during setup. Change it after moving DocTheCode, e.g. to a custom domain,
        and then update each GitHub App's callback and webhook URLs to match (shown under GitHub connections).</p>
      <ErrorBox error={error} />
    </form>
  );
}

function GitHubEvents({ events, hosts }: { events: AdminConfig["github_events"]; hosts: string[] }) {
  const lastHook = Math.max(0, ...hosts.map((h) => events.last_webhook_at[h] ?? 0));
  return (
    <div className="muted small">
      <strong>How DocTheCode hears about pull requests:</strong>{" "}
      {lastHook
        ? <>through GitHub webhooks (last one {timeAgo(lastHook)}).</>
        : <>no webhooks have arrived since this server started. If GitHub can't reach DocTheCode, people use
          <em> Check GitHub for changes</em> on the dashboard or a doc space's pull requests.</>}
      {events.links.filter((l) => l.check_error).map((l) => (
        <div key={l.full_name} className="gate-fail">Checking {l.full_name} failed: {l.check_error}</div>
      ))}
    </div>
  );
}
