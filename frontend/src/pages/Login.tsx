import type { Host } from "../api";

export default function Login({ hosts }: { hosts: Host[] }) {
  const next = encodeURIComponent(location.pathname + location.search);
  const error = new URLSearchParams(location.search).get("login_error");
  return (
    <div className="login">
      <h1>📚 DocTheCode</h1>
      <p className="muted">Documentation that ships with your code. Every pull request gets a docs pull request,
        drafted by an agent and approved by your team before the code can merge.</p>
      {error && <div className="error">GitHub sign-in was cancelled or failed ({error}).</div>}
      <div className="stack">
        {hosts.map((h) => (
          <a key={h.id} className="btn btn-primary btn-lg" href={`/api/auth/login/${h.id}?next=${next}`}>
            Sign in with {h.name}
          </a>
        ))}
      </div>
      <p className="muted small">You'll automatically see the documentation for every connected GitHub repository you have access to.</p>
    </div>
  );
}
