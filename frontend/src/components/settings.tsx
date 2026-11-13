import { useState } from "react";
import { api, type AgentInput, type AgentSettings, type HostAdmin, type HostInput, type ManifestInput, type StorageStatus } from "../api";
import { ErrorBox } from "./ui";

/** A form field with a short explanation: whether you choose the value or copy it from somewhere. */
function Field({ label, source, hint, children, className }: {
  label: React.ReactNode; source: "choose" | "copy" | "optional"; hint: React.ReactNode; children: React.ReactNode; className?: string;
}) {
  const tag = { choose: "you choose", copy: "copy from GitHub", optional: "optional" }[source];
  return (
    <label className={className}>
      <span className="row">{label} <span className={`chip tag-${source}`}>{tag}</span></span>
      {children}
      <span className="muted small hint">{hint}</span>
    </label>
  );
}

function InfoPanel({ title, children, defaultOpen = true }: { title: string; children: React.ReactNode; defaultOpen?: boolean }) {
  return (
    <details className="info-panel" open={defaultOpen}>
      <summary>ⓘ {title}</summary>
      <div className="small">{children}</div>
    </details>
  );
}

/** "https://github.acme.com" → { id: "acme", name: "Acme GitHub" } */
function suggestFromUrl(url: string): { id: string; name: string } | null {
  try {
    const host = new URL(url).hostname.toLowerCase();
    const parts = host.split(".").filter((p) => !["github", "ghe", "git", "www", "code", "scm"].includes(p));
    const core = (parts.length > 1 ? parts.slice(0, -1) : parts).join("-").replace(/[^a-z0-9-]/g, "").slice(0, 40);
    if (!core) return null;
    const word = core.split("-")[core.split("-").length - 1];
    return { id: core, name: `${word.charAt(0).toUpperCase()}${word.slice(1)} GitHub` };
  } catch {
    return null;
  }
}

const RANDOM_SUFFIX = Math.random().toString(36).slice(2, 6);

/** One click: GitHub creates the App from our manifest and hands the credentials back to DocTheCode. */
export function CreateAppForm({ publicUrlIsLocal, publicUrl, existingIds }: { publicUrlIsLocal: boolean; publicUrl: string; existingIds: string[] }) {
  const [kind, setKind] = useState<"github" | "enterprise">("github");
  const [enterpriseUrl, setEnterpriseUrl] = useState("https://");
  const [hostId, setHostId] = useState("");
  const [hostName, setHostName] = useState("");
  const [touched, setTouched] = useState({ id: false, name: false, app: false });
  const [org, setOrg] = useState("");
  const [appName, setAppName] = useState(`DocTheCode ${RANDOM_SUFFIX}`);
  const [hook, setHook] = useState("");
  const [webhooks, setWebhooks] = useState(!publicUrlIsLocal);
  const [isPublic, setIsPublic] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const id = kind === "github" ? "github" : hostId;
  const githubTaken = kind === "github" && existingIds.includes("github");
  const defaultHook = `${publicUrl}/api/webhooks/github/${id || "<id>"}`;
  const ghBase = kind === "github" ? "https://github.com" : (enterpriseUrl.replace(/\/$/, "") || "https://github.your-company.com");

  function onEnterpriseUrl(v: string) {
    setEnterpriseUrl(v);
    const s = suggestFromUrl(v);
    if (s && !touched.id) setHostId(s.id);
    if (s && !touched.name) setHostName(s.name);
  }

  function onOrg(v: string) {
    setOrg(v);
    if (!touched.app) setAppName((v.trim() ? `DocTheCode for ${v.trim()}` : `DocTheCode ${RANDOM_SUFFIX}`).slice(0, 34));
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    const body: ManifestInput = {
      host_id: id, host_name: kind === "github" ? "GitHub" : hostName || hostId,
      web_url: kind === "github" ? "https://github.com" : enterpriseUrl, organization: org.trim(),
      app_name: appName, webhook_url: webhooks ? hook.trim() || null : null, webhooks, public: isPublic,
    };
    try {
      const { action, manifest } = await api.startManifest(body);
      // GitHub's manifest flow needs a real form POST, not fetch.
      const form = document.createElement("form");
      form.method = "post";
      form.action = action;
      const input = document.createElement("input");
      input.type = "hidden";
      input.name = "manifest";
      input.value = manifest;
      form.appendChild(input);
      document.body.appendChild(form);
      form.submit();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <form className="stack" onSubmit={submit}>
      <ErrorBox error={error} />
      <InfoPanel title="What happens here, and what to enter">
        <p>DocTheCode asks GitHub to create a <em>GitHub App</em> for it: the identity DocTheCode uses to read pull requests, post the
          documentation check and let people sign in. You fill in a few names below, confirm on GitHub, and GitHub sends the app's
          keys straight back to DocTheCode. There is nothing to copy or paste.</p>
        <ul>
          <li><strong>GitHub.com</strong> if your code is on github.com, including companies on GitHub Enterprise Cloud.</li>
          <li><strong>GitHub Enterprise Server</strong> if your company runs its own GitHub at its own address (e.g. <code>github.your-company.com</code>).</li>
        </ul>
        <p>Fields marked <span className="chip tag-choose">you choose</span> are names you make up; nothing needs to match anything else.</p>
      </InfoPanel>

      <div className="segmented">
        <button type="button" className={kind === "github" ? "on" : ""} onClick={() => setKind("github")}>GitHub.com</button>
        <button type="button" className={kind === "enterprise" ? "on" : ""} onClick={() => setKind("enterprise")}>GitHub Enterprise Server</button>
      </div>
      {githubTaken ? (
        <div className="notice">
          DocTheCode is already connected to GitHub.com (listed above). There can be one GitHub.com connection: to create a new
          app instead, remove the existing connection first with its <em>Remove</em> button.
        </div>
      ) : <>

      {kind === "enterprise" && (
        <>
          <Field label="Enterprise URL" source="copy" hint={<>The address you open your company's GitHub at in the browser, e.g. <code>https://github.your-company.com</code>. Copy it from your browser's address bar.</>}>
            <input required value={enterpriseUrl} onChange={(e) => onEnterpriseUrl(e.target.value)} placeholder="https://github.your-company.com" />
          </Field>
          <div className="row align-start">
            <Field className="grow" label="Short id" source="choose" hint={<>A short nickname for this GitHub inside DocTheCode, used in DocTheCode's own addresses. Lowercase letters, digits and dashes. Filled in from the URL; can't be changed later.</>}>
              <input required pattern="[a-z0-9][a-z0-9-]*" value={hostId} placeholder="acme"
                onChange={(e) => { setHostId(e.target.value); setTouched({ ...touched, id: true }); }} />
            </Field>
            <Field className="grow" label="Name on the sign-in button" source="choose" hint={<>What people see on DocTheCode's login page: “Sign in with <em>{hostName || "Acme GitHub"}</em>”.</>}>
              <input value={hostName} placeholder="Acme GitHub"
                onChange={(e) => { setHostName(e.target.value); setTouched({ ...touched, name: true }); }} />
            </Field>
          </div>
        </>
      )}

      <div className="row align-start">
        <Field className="grow" label="Create under organization" source="optional" hint={<>
          Your GitHub organization's name, as in <code>{ghBase}/<strong>your-org</strong></code>. Recommended for teams: the app then
          belongs to the organization, not to you. You need to be an owner (or GitHub App manager) of that organization.
          Leave empty to create it under your personal account.</>}>
          <input value={org} onChange={(e) => onOrg(e.target.value)} placeholder="leave empty for your personal account" />
        </Field>
        <Field className="grow" label="App name" source="choose" hint={<>
          The name GitHub shows on pull requests next to DocTheCode's check and comments. It must be unique on {kind === "github" ? "all of GitHub.com" : "your GitHub server"}, so
          a plain “DocTheCode” is probably taken: keep the suggestion or pick your own (max 34 characters).</>}>
          <input required maxLength={34} value={appName}
            onChange={(e) => { setAppName(e.target.value); setTouched({ ...touched, app: true }); }} />
        </Field>
      </div>

      <label className="check"><input type="checkbox" checked={webhooks} onChange={(e) => setWebhooks(e.target.checked)} />
        GitHub can reach this address over the internet (send webhooks)</label>
      {webhooks ? (
        <Field label="Webhook URL" source="optional" hint={<>Where GitHub sends pull request events. Leave empty to use {defaultHook}.</>}>
          <input value={hook} onChange={(e) => setHook(e.target.value)} placeholder={defaultHook} />
        </Field>
      ) : (
        <p className="muted small">Untick this when DocTheCode runs privately (GitHub can't reach it). The app is then created without a webhook, and people click
          <em> Check GitHub for changes</em> (dashboard, or a doc space's pull requests) to pick up new, updated, merged and closed pull requests.</p>
      )}
      <label className="check"><input type="checkbox" checked={isPublic} onChange={(e) => setIsPublic(e.target.checked)} />
        Allow other accounts and organizations to install this app</label>
      <p className="muted small hint">Leave unticked unless code you want documented lives in organizations other than the one that owns the app.</p>
      <button className="btn btn-primary">Create the GitHub App on {kind === "github" ? "GitHub.com" : "your server"} →</button>
      <p className="muted small">You'll confirm on GitHub. GitHub then sends the app's ID, secrets and private key straight back to DocTheCode, which stores them encrypted.</p>
      </>}
    </form>
  );
}

/** Enter (or edit) the credentials of an existing GitHub App by hand. Empty secret fields keep the stored value. */
export function HostForm({ existing, onSaved }: { existing?: HostAdmin; onSaved: () => void }) {
  const [f, setF] = useState<HostInput>(existing ? {
    id: existing.id, name: existing.name, web_url: existing.web_url, api_url: existing.api_url, app_id: existing.app_id,
    app_slug: existing.app_slug, client_id: existing.client_id,
  } : { id: "github", name: "GitHub", web_url: "https://github.com", app_id: "", app_slug: "", client_id: "" });
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const set = (k: keyof HostInput) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => setF({ ...f, [k]: e.target.value });
  const keep = existing ? "leave empty to keep the stored value" : "";
  const settings = <>your app's settings page on GitHub (<em>Settings → Developer settings → GitHub Apps → your app → Edit</em>)</>;

  async function readPem(file: File | undefined) {
    if (file) setF({ ...f, private_key: await file.text() });
  }

  return (
    <form className="stack" onSubmit={async (e) => {
      e.preventDefault();
      setError(null);
      try { await api.saveHost(f); setSaved(true); onSaved(); } catch (err) { setError((err as Error).message); }
    }}>
      <ErrorBox error={error} />
      {saved && <div className="notice">Saved.</div>}
      {!existing && (
        <InfoPanel title="Where to find these values">
          <p>Everything marked <span className="chip tag-copy">copy from GitHub</span> is on {settings}, on the <em>General</em> tab:</p>
          <ul>
            <li><strong>App ID</strong> and <strong>Client ID</strong>: in the <em>About</em> box at the top.</li>
            <li><strong>App slug</strong>: the last part of the app's public link, <code>github.com/apps/<strong>this-part</strong></code>.</li>
            <li><strong>Client secret</strong>: <em>Client secrets → Generate a new client secret</em>. GitHub shows it only once.</li>
            <li><strong>Webhook secret</strong>: the value in the <em>Webhook → Secret</em> field. If it's empty, type any long random text there, save, and use the same text here.</li>
            <li><strong>Private key</strong>: <em>Private keys → Generate a private key</em> downloads a <code>.pem</code> file; pick that file below.</li>
          </ul>
        </InfoPanel>
      )}
      <div className="row align-start">
        <Field className="grow" label="Id" source="choose" hint="A short nickname for this GitHub inside DocTheCode. Keep “github” for GitHub.com.">
          <input required disabled={!!existing} value={f.id} onChange={set("id")} pattern="[a-z0-9][a-z0-9-]*" />
        </Field>
        <Field className="grow" label="Name" source="choose" hint="Shown on the sign-in button.">
          <input required value={f.name} onChange={set("name")} />
        </Field>
      </div>
      <div className="row align-start">
        <Field className="grow" label="GitHub URL" source="copy" hint="https://github.com, or your GitHub Enterprise Server's address.">
          <input required value={f.web_url} onChange={set("web_url")} />
        </Field>
        <Field className="grow" label="API URL" source="optional" hint="Leave empty: worked out from the GitHub URL.">
          <input value={f.api_url ?? ""} onChange={set("api_url")} placeholder="derived from the GitHub URL" />
        </Field>
      </div>
      <div className="row align-start">
        <Field className="grow" label="App ID" source="copy" hint="A number, in the About box.">
          <input required value={f.app_id} onChange={set("app_id")} />
        </Field>
        <Field className="grow" label="App slug" source="copy" hint="From github.com/apps/<slug>.">
          <input required value={f.app_slug} onChange={set("app_slug")} />
        </Field>
        <Field className="grow" label="Client ID" source="copy" hint="Starts with Iv…, in the About box.">
          <input required value={f.client_id} onChange={set("client_id")} />
        </Field>
      </div>
      <Field label={<>Client secret {existing?.client_secret && <span className="muted small">stored: {existing.client_secret}</span>}</>} source="copy"
        hint="Client secrets → Generate a new client secret.">
        <input type="password" autoComplete="off" required={!existing} value={f.client_secret ?? ""} onChange={set("client_secret")} placeholder={keep} />
      </Field>
      <Field label={<>Webhook secret {existing?.webhook_secret && <span className="muted small">stored: {existing.webhook_secret}</span>}</>} source="copy"
        hint="The Webhook → Secret field. If empty there, type any long random text in both places.">
        <input type="password" autoComplete="off" required={!existing} value={f.webhook_secret ?? ""} onChange={set("webhook_secret")} placeholder={keep} />
      </Field>
      <Field label={<>Private key (.pem) {existing?.private_key && <span className="muted small">stored</span>}</>} source="copy"
        hint="Private keys → Generate a private key, then pick the downloaded file.">
        <input type="file" accept=".pem" onChange={(e) => readPem(e.target.files?.[0])} />
        <textarea rows={3} value={f.private_key ?? ""} onChange={set("private_key")}
          placeholder={existing ? keep : "-----BEGIN RSA PRIVATE KEY----- … (or pick the file above)"} />
      </Field>
      <button className="btn btn-primary">{existing ? "Save changes" : "Save GitHub App"}</button>
    </form>
  );
}

/** Choose the AI provider for the documentation agent: Anthropic, OpenAI, Gemini, GitHub Copilot or any
 *  OpenAI-compatible endpoint. Models are loaded from the provider rather than hard-coded. */
export function AgentForm({ current, onSaved }: { current: AgentSettings; onSaved: () => void }) {
  const [provider, setProvider] = useState(current.provider ?? "");
  const [key, setKey] = useState("");
  const [model, setModel] = useState(current.model ?? "");
  const [baseUrl, setBaseUrl] = useState(current.base_url ?? "");
  const [models, setModels] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<null | "models" | "test" | "save">(null);
  const info = current.providers.find((p) => p.id === provider);
  const hasStoredKey = !!current.stored_keys[provider];
  const input = (): AgentInput => ({ provider, api_key: key || undefined, model: model || undefined, base_url: baseUrl || undefined });

  const run = async (kind: "models" | "test" | "save", fn: () => Promise<void>) => {
    setBusy(kind); setError(null); setNotice(null);
    try { await fn(); } catch (e) { setError((e as Error).message); } finally { setBusy(null); }
  };

  return (
    <div className="stack">
      <div className="provider-grid">
        {current.providers.map((p) => (
          <button type="button" key={p.id} className={`provider ${provider === p.id ? "on" : ""}`}
            onClick={() => { setProvider(p.id); setModels([]); setModel(p.id === current.provider ? current.model ?? "" : ""); setKey("");
              setBaseUrl(p.id === current.provider ? current.base_url ?? "" : ""); setError(null); setNotice(null); }}>
            {p.label}
          </button>
        ))}
      </div>
      {!info && <p className="muted small">Choose which AI service drafts your documentation.</p>}
      {info && (
        <form className="stack" onSubmit={(e) => { e.preventDefault(); run("save", async () => {
          await api.saveAgent(input()); setKey(""); setNotice("Saved."); onSaved(); }); }}>
          {info.id === "copilot" ? (
            <div className="notice small">
              <strong>GitHub Copilot needs no API key.</strong> DocTheCode uses its GitHub App, and usage is billed to the
              organization's Copilot plan. Before testing:
              <ol>
                <li>In the GitHub App's settings (<em>Permissions → Organization permissions</em>), set <strong>Copilot Requests</strong> to <em>Read-only</em>, then accept the new permission on each organization's installation.</li>
                <li>In the organization's Copilot policies, allow Copilot requests from GitHub App installations and the model you choose.</li>
              </ol>
            </div>
          ) : (
            <label>API key {hasStoredKey && <span className="muted small">a key is stored{info.id === current.provider && current.api_key ? ` (${current.api_key})` : ""}</span>}
              <input type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)}
                placeholder={hasStoredKey ? "leave empty to keep the stored key" : info.key_hint} />
              {info.key_url && <span className="muted small">Get one at <a href={info.key_url} target="_blank" rel="noreferrer">{new URL(info.key_url).host}</a>. Stored encrypted.</span>}
            </label>
          )}
          {(info.id === "openai_compatible" || baseUrl) && (
            <label>API base URL
              <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} required={info.id === "openai_compatible"}
                placeholder={info.base_url || "https://your-endpoint/v1  (Azure AI Foundry, OpenRouter, Ollama, vLLM…)"} />
            </label>
          )}
          <label>Model
            <div className="row">
              <input className="grow" list="agent-models" value={model} onChange={(e) => setModel(e.target.value)}
                placeholder={info.default_model ? `default: ${info.default_model}` : "load the list or type a model id"}
                required={!info.default_model} />
              <datalist id="agent-models">{models.map((m) => <option key={m} value={m} />)}</datalist>
              {info.id !== "copilot" && (
                <button type="button" className="btn" disabled={busy !== null || (info.needs_key && info.id !== "openai_compatible" && !key && !hasStoredKey)}
                  onClick={() => run("models", async () => {
                    const r = await api.agentModels(input()); setModels(r.models);
                    setNotice(r.models.length ? `${r.models.length} models available: pick one from the list.` : "The provider returned no models.");
                  })}>{busy === "models" ? "Loading…" : "Load models"}</button>
              )}
            </div>
            <span className="muted small">The model must support tool (function) calling. Use <em>Test</em> to check.</span>
          </label>
          {info.id !== "openai_compatible" && !baseUrl && (
            <button type="button" className="btn-link small" onClick={() => setBaseUrl(info.base_url)}>Use a different API endpoint (proxy, gateway)…</button>
          )}
          <ErrorBox error={error} />
          {notice && <div className="notice">{notice}</div>}
          <div className="row">
            <button type="button" className="btn" disabled={busy !== null} onClick={() => run("test", async () => {
              const r = await api.testAgent(input()); setNotice(`Works: ${r.model} answered and called a tool.`);
            })}>{busy === "test" ? "Testing…" : "Test"}</button>
            <button className="btn btn-primary" disabled={busy !== null}>{busy === "save" ? "Saving…" : "Save"}</button>
          </div>
          {current.problem && current.provider && <p className="muted small">Current setting: {current.problem}</p>}
        </form>
      )}
    </div>
  );
}

/** Where DocTheCode keeps its data (shown in Admin when snapshots to a backup folder are active). */
export function StorageNotice({ storage }: { storage: StorageStatus }) {
  if (!storage.backup_enabled) return null;
  return (
    <p className="muted small">
      Data is snapshotted to <code>{storage.backup_dir}</code> after every change and restored automatically when the container starts.
      {storage.last_backup_at && <> Last snapshot {new Date(storage.last_backup_at * 1000).toLocaleString()}.</>}
      {storage.restored && <> This container was restored from <code>{storage.restored.from}</code>.</>}
      {storage.last_backup_error && <span className="gate-fail"> Last snapshot failed: {storage.last_backup_error}</span>}
    </p>
  );
}
