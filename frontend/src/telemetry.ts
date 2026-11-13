// Anonymous usage data from the browser: screen views (as route templates, never real
// repo or page names), a few UI actions, and uncaught errors. The server adds nothing
// identifying and forwards it only if the organization's admin has usage data switched on.

type Ev = { name: string; props: Record<string, string | number | boolean | null> };
type Err = { kind: string; message: string; stack: string[]; route: string };

const events: Ev[] = [];
const errors: Err[] = [];

/** Turn a real path into a template, e.g. /acme/payments-docs/pulls/12 → /:owner/:repo/pulls/:number */
export function routeTemplate(path: string): string {
  if (["/", "/new", "/admin", "/setup"].includes(path)) return path;
  const parts = path.split("/").filter(Boolean);
  if (parts.length < 2) return "/:unknown";
  const rest = parts.slice(2).map((p, i) => (i === 1 && parts[2] === "pulls" ? ":number" : p));
  return ["/:owner/:repo", ...rest].join("/").replace(/\/$/, "");
}

export function track(name: string, props: Ev["props"] = {}) {
  if (events.length < 200) events.push({ name, props });
}

export function trackPageView(path: string) {
  track("page_view", { route: routeTemplate(path) });
}

function flush(useBeacon = false) {
  if (!events.length && !errors.length) return;
  const body = JSON.stringify({ events: events.splice(0, 50), errors: errors.splice(0, 10) });
  if (useBeacon && navigator.sendBeacon) {
    navigator.sendBeacon("/api/telemetry/client", new Blob([body], { type: "application/json" }));
  } else {
    fetch("/api/telemetry/client", { method: "POST", headers: { "Content-Type": "application/json" }, body, keepalive: true })
      .catch(() => { /* never bother the user */ });
  }
}

function recordError(e: unknown) {
  if (errors.length >= 20) return;
  const err = e instanceof Error ? e : new Error(String(e));
  errors.push({
    kind: err.name || "Error",
    message: (err.message || "").slice(0, 300),
    stack: (err.stack ?? "").split("\n").slice(1, 12).map((l) => l.trim()),
    route: routeTemplate(location.pathname),
  });
}

export function startTelemetry() {
  window.addEventListener("error", (e) => recordError(e.error ?? e.message));
  window.addEventListener("unhandledrejection", (e) => recordError(e.reason));
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") flush(true); });
  setInterval(() => flush(), 30_000);
}
