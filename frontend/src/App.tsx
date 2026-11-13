import { useCallback, useEffect, useState } from "react";
import { Link, NavLink, Route, Routes, useLocation, useParams } from "react-router-dom";
import { api, type Me, type SetupStatus } from "./api";
import { MeContext, Spinner, useMe } from "./components/ui";
import Admin from "./pages/Admin";
import Dashboard from "./pages/Dashboard";
import Login from "./pages/Login";
import NewRepo from "./pages/NewRepo";
import PullView from "./pages/PullView";
import RepoCode from "./pages/RepoCode";
import RepoPulls from "./pages/RepoPulls";
import RepoSettings from "./pages/RepoSettings";
import Setup from "./pages/Setup";
import { trackPageView } from "./telemetry";

export default function App() {
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  const [status, setStatus] = useState<SetupStatus | null>(null);

  const load = useCallback(async () => {
    const [s, m] = await Promise.all([api.setupStatus(), api.me().catch(() => null)]);
    setStatus(s);
    setMe(m);
  }, []);
  useEffect(() => { load(); }, [load]);
  const { pathname } = useLocation();
  useEffect(() => { trackPageView(pathname); }, [pathname]);

  if (me === undefined || !status) return <Spinner />;
  // Until the first admin has signed in, everyone sees the setup wizard (it is locked by the setup token).
  if (status.needs_setup && !me?.is_admin) return <Setup onChanged={load} />;
  if (me === null) return <Login hosts={status.hosts} />;
  return (
    <MeContext.Provider value={me}>
      <TopBar />
      <main className="container">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/new" element={<NewRepo />} />
          <Route path="/admin" element={<Admin />} />
          <Route path="/setup" element={<Setup />} />
          <Route path="/:owner/:repo/*" element={<RepoShell />} />
        </Routes>
      </main>
    </MeContext.Provider>
  );
}

function TopBar() {
  const me = useMe();
  return (
    <header className="topbar">
      <Link to="/" className="brand">📚 DocTheCode</Link>
      <div className="spacer" />
      {me.is_admin && <Link to="/admin">Admin</Link>}
      <span className="muted small">{me.host.name}</span>
      {me.avatar_url && <img className="avatar" src={me.avatar_url} alt="" />}
      <span>{me.handle}</span>
      <button className="btn-link" onClick={() => api.logout().then(() => location.assign("/"))}>Sign out</button>
    </header>
  );
}

function RepoShell() {
  const { owner = "", repo = "" } = useParams();
  const base = `/${owner}/${repo}`;
  return (
    <>
      <div className="repo-head">
        <h2>
          <Link to={`/`}>{owner}</Link> / <Link to={base}>{repo}</Link>
        </h2>
        <nav className="tabs">
          <NavLink end to={base}>Pages</NavLink>
          <NavLink to={`${base}/pulls`}>Pull requests</NavLink>
          <NavLink to={`${base}/settings`}>Settings</NavLink>
        </nav>
      </div>
      <Routes>
        <Route index element={<RepoCode owner={owner} name={repo} />} />
        <Route path="pulls" element={<RepoPulls owner={owner} name={repo} />} />
        <Route path="pulls/:number" element={<PullView owner={owner} name={repo} />} />
        <Route path="settings" element={<RepoSettings owner={owner} name={repo} />} />
      </Routes>
    </>
  );
}
