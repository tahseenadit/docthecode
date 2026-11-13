import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { ErrorBox, useMe } from "../components/ui";

export default function NewRepo() {
  const me = useMe();
  const nav = useNavigate();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [branch, setBranch] = useState("main");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const repo = await api.createRepo({ name, description, default_branch: branch });
      nav(`/${repo.full_name}/settings`);
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  return (
    <form className="narrow stack" onSubmit={submit}>
      <h2>Create a documentation repository</h2>
      <ErrorBox error={error} />
      <label>Name
        <div className="row"><span className="muted">{me.handle} /</span>
          <input required value={name} onChange={(e) => setName(e.target.value)} placeholder="my-service-docs" pattern="[A-Za-z0-9][A-Za-z0-9._\-]*" />
        </div>
      </label>
      <label>Description<input value={description} onChange={(e) => setDescription(e.target.value)} /></label>
      <label>Default branch<input value={branch} onChange={(e) => setBranch(e.target.value)} /></label>
      <p className="muted small">Next you'll connect it to a GitHub repository.</p>
      <button className="btn btn-primary" disabled={busy || !name}>Create repository</button>
    </form>
  );
}
