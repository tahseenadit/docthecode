import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, type PageNode, type PageTree } from "../api";
import { Card, ErrorBox, Markdown, Spinner, useAsync } from "../components/ui";
import { track } from "../telemetry";

/** Confluence-style space: page tree on the left, the page on the right. `?ref=` picks a branch, `?page=` a page. */
export default function RepoCode({ owner, name }: { owner: string; name: string }) {
  const [params, setParams] = useSearchParams();
  const { data: repo, error } = useAsync(() => api.repo(owner, name), [owner, name]);
  const ref = params.get("ref") ?? repo?.default_branch ?? "";
  const { data: tree, error: treeError, reload } = useAsync<PageTree | null>(
    () => (ref ? api.pages(owner, name, ref) : Promise.resolve(null)), [owner, name, ref]);
  const canWrite = repo?.role === "admin" || repo?.role === "write";
  const selected = params.get("page") ?? tree?.home ?? firstPage(tree?.children ?? []) ?? null;
  const [creatingUnder, setCreatingUnder] = useState<string | null>(null);

  const go = (updates: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(updates)) (v === null ? next.delete(k) : next.set(k, v));
    setParams(next);
  };

  const sections = useMemo(() => collectSections(tree?.children ?? []), [tree]);

  if (error) return <ErrorBox error={error} />;
  if (!repo) return <Spinner />;
  const isDefault = ref === repo.default_branch;

  return (
    <div className="split">
      <aside className="page-sidebar">
        <select value={ref} onChange={(e) => go({ ref: e.target.value, page: null })} title="Branch">
          {Object.keys(repo.branches ?? {}).sort().map((b) => <option key={b}>{b}</option>)}
        </select>
        {!isDefault && <p className="muted small">Viewing branch <code>{ref}</code>. Edits here go into that branch.</p>}
        {tree?.home && (
          <div className={`tree-row home ${selected === tree.home ? "active" : ""}`} onClick={() => go({ page: tree.home })}>🏠 Home</div>
        )}
        <ErrorBox error={treeError} />
        {tree && <TreeLevel nodes={tree.children} selected={selected} depth={0} onOpen={(p) => go({ page: p })} />}
        {canWrite && (
          <button className="btn grow-btn" onClick={() => setCreatingUnder("")}>+ New page</button>
        )}
        {repo.links.length === 0 && (
          <p className="muted small">Not connected to GitHub yet. Connect a code repo in Settings to get doc PRs automatically.</p>
        )}
      </aside>

      <div className="stack">
        {creatingUnder !== null && (
          <NewPageForm owner={owner} name={name} branch={ref} parent={creatingUnder} sections={sections}
            onCancel={() => setCreatingUnder(null)}
            onCreated={(path) => { setCreatingUnder(null); reload(); go({ page: path, edit: "1" }); }} />
        )}
        {tree && selected ? (
          <PageView key={`${ref}:${selected}`} owner={owner} name={name} refName={ref} path={selected} tree={tree}
            canWrite={canWrite} sections={sections} startEditing={params.get("edit") === "1"}
            onOpen={(p) => go({ page: p, edit: null })} onChanged={reload} onNewChild={(p) => setCreatingUnder(p)}
            onDoneEditing={() => go({ edit: null })} />
        ) : tree ? (
          <div className="empty"><p>This space has no pages yet.</p></div>
        ) : <Spinner />}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ tree helpers
function firstPage(nodes: PageNode[]): string | null {
  for (const n of nodes) {
    if (n.kind === "page" && n.path) return n.path;
    if (n.kind === "section") return n.path ?? firstPage(n.children);
  }
  return null;
}

function collectSections(nodes: PageNode[], out: { dir: string; label: string }[] = [], prefix = ""): { dir: string; label: string }[] {
  for (const n of nodes) {
    if (n.kind === "section" && n.dir) {
      out.push({ dir: n.dir, label: prefix + n.title });
      collectSections(n.children, out, prefix + n.title + " / ");
    }
  }
  return out;
}

/** Path from the root to the node whose page is `path` (for breadcrumbs). */
function trail(nodes: PageNode[], path: string): PageNode[] | null {
  for (const n of nodes) {
    if (n.path === path) return [n];
    if (n.kind === "section") {
      const sub = trail(n.children, path);
      if (sub) return [n, ...sub];
    }
  }
  return null;
}

function isAncestor(node: PageNode, selected: string | null): boolean {
  return !!selected && !!node.dir && selected.startsWith(node.dir + "/");
}

function TreeLevel({ nodes, selected, depth, onOpen }: { nodes: PageNode[]; selected: string | null; depth: number; onOpen: (p: string) => void }) {
  return (
    <ul className="page-tree">
      {nodes.map((n) => <TreeItem key={n.dir ?? n.path} node={n} selected={selected} depth={depth} onOpen={onOpen} />)}
    </ul>
  );
}

function TreeItem({ node, selected, depth, onOpen }: { node: PageNode; selected: string | null; depth: number; onOpen: (p: string) => void }) {
  const [open, setOpen] = useState(depth === 0 || isAncestor(node, selected));
  useEffect(() => { if (isAncestor(node, selected)) setOpen(true); }, [node, selected]);
  const active = node.path !== null && node.path === selected;
  const icon = node.kind === "file" ? "📎" : node.kind === "section" ? "" : "📄";
  return (
    <li>
      <div className={`tree-row ${active ? "active" : ""} ${node.kind}`} style={{ paddingLeft: 6 + depth * 14 }}
        onClick={() => (node.path ? onOpen(node.path) : setOpen(!open))}>
        {node.kind === "section" && (
          <span className="twisty" onClick={(e) => { e.stopPropagation(); setOpen(!open); }}>{open ? "▾" : "▸"}</span>
        )}
        {icon && <span>{icon}</span>} <span className="tree-title">{node.title}</span>
      </div>
      {node.kind === "section" && open && <TreeLevel nodes={node.children} selected={selected} depth={depth + 1} onOpen={onOpen} />}
    </li>
  );
}

// ------------------------------------------------------------------ page view
const DOC_KINDS: Record<string, string> = {
  business: "Business", product: "Business", domain: "Business", concepts: "Business", explanation: "Business",
  tutorial: "Tutorial", tutorials: "Tutorial", guide: "Tutorial", guides: "Tutorial", "how-to": "Tutorial",
  reference: "Reference", api: "Reference",
  technical: "Technical", architecture: "Technical", engineering: "Technical", operations: "Technical",
};

/** The page's kind of documentation from its `type:` front matter, if any. */
function docKind(content: string): string | null {
  const fm = content.match(/^---\s*\n([\s\S]*?)\n---/);
  const type = fm?.[1].match(/^type\s*:\s*(.+)$/im)?.[1].trim().replace(/^["']|["']$/g, "").toLowerCase();
  return (type && DOC_KINDS[type]) || null;
}

function stripForDisplay(content: string, title: string): string {
  let body = content.replace(/^---\s*\n[\s\S]*?\n---\s*(\n|$)/, "");
  const firstHeading = body.match(/^\s*# (.+)\n?/);
  if (firstHeading && firstHeading[1].trim() === title.trim()) body = body.slice(firstHeading[0].length);
  return body;
}

function resolveRelative(fromPath: string, href: string): string | null {
  if (/^[a-z]+:|^#|^\/\//i.test(href)) return null;
  const [target] = href.split("#");
  if (!/\.(md|markdown|mdx)$/i.test(target)) return null;
  const parts = fromPath.split("/").slice(0, -1);
  for (const seg of target.split("/")) {
    if (seg === "..") parts.pop();
    else if (seg && seg !== ".") parts.push(seg);
  }
  return parts.join("/");
}

function PageView(props: {
  owner: string; name: string; refName: string; path: string; tree: PageTree; canWrite: boolean;
  sections: { dir: string; label: string }[]; startEditing: boolean;
  onOpen: (p: string) => void; onChanged: () => void; onNewChild: (p: string) => void; onDoneEditing: () => void;
}) {
  const { owner, name, refName, path, tree, canWrite } = props;
  const { data, error, reload } = useAsync(() => api.file(owner, name, path, refName), [owner, name, path, refName]);
  const [editing, setEditing] = useState(props.startEditing);
  const [moving, setMoving] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const crumbs = trail(tree.children, path) ?? [];
  const node = crumbs[crumbs.length - 1];
  const title = path === tree.home ? "Home" : node?.title ?? path;
  const isMd = /\.(md|markdown|mdx)$/i.test(path);

  const act = async (fn: () => Promise<unknown>) => {
    setActionError(null);
    try { await fn(); props.onChanged(); } catch (e) { setActionError((e as Error).message); }
  };

  if (error) return <ErrorBox error={error} />;
  if (!data) return <Spinner />;
  if (editing) {
    return <Editor owner={owner} name={name} branch={refName} path={path} initial={data.content} baseSha={data.commit}
      onDone={() => { setEditing(false); props.onDoneEditing(); reload(); props.onChanged(); }} />;
  }

  return (
    <article className="page">
      <nav className="crumbs muted small">
        {tree.home && path !== tree.home && <a onClick={() => props.onOpen(tree.home!)}>Home</a>}
        {crumbs.slice(0, -1).map((c) => (
          <span key={c.dir ?? c.path}> / {c.path ? <a onClick={() => props.onOpen(c.path!)}>{c.title}</a> : c.title}</span>
        ))}
      </nav>
      <div className="row between page-head">
        <h1>{title}{isMd && docKind(data.content) && <span className="badge muted doc-kind" title="Kind of documentation">{docKind(data.content)}</span>}</h1>
        {canWrite && isMd && (
          <div className="row">
            <button className="btn" onClick={() => { setEditing(true); track("edit_opened"); }}>Edit</button>
            <button className="btn" onClick={() => props.onNewChild(path)}>+ Child page</button>
            {path !== tree.home && <>
              <button className="btn" onClick={() => {
                const t = prompt("New title", title);
                if (t && t.trim() && t !== title) act(() => api.renamePage(owner, name, { branch: refName, path, title: t }));
              }}>Rename</button>
              <button className="btn" onClick={() => setMoving(!moving)}>Move</button>
              <button className="btn btn-danger" onClick={() => {
                const what = node?.kind === "section" ? `the section “${title}” and all its child pages` : `“${title}”`;
                if (confirm(`Delete ${what}?`)) act(async () => { await api.deletePage(owner, name, { branch: refName, path }); props.onOpen(tree.home ?? firstPage(tree.children) ?? ""); });
              }}>Delete</button>
            </>}
          </div>
        )}
      </div>
      <ErrorBox error={actionError} />
      {moving && (
        <MoveForm sections={props.sections.filter((s) => !(node?.dir && (s.dir === node.dir || s.dir.startsWith(node.dir + "/"))))}
          onCancel={() => setMoving(false)}
          onMove={(parent) => act(async () => {
            const res = await api.movePage(owner, name, { branch: refName, path, new_parent: parent });
            setMoving(false);
            props.onOpen(node?.kind === "section" ? `${res.path}/${path.split("/").pop()}` : res.path);
          })} />
      )}
      {isMd ? (
        <Markdown onPageLink={(href) => {
          const target = resolveRelative(path, href);
          return target ? `?${new URLSearchParams({ ref: refName, page: target })}` : null;
        }}>{stripForDisplay(data.content, title)}</Markdown>
      ) : (
        <Card title={`Attachment: ${path}`}><pre className="code">{data.content.slice(0, 20000)}</pre></Card>
      )}
    </article>
  );
}

function NewPageForm({ owner, name, branch, parent, sections, onCancel, onCreated }: {
  owner: string; name: string; branch: string; parent: string; sections: { dir: string; label: string }[];
  onCancel: () => void; onCreated: (path: string) => void;
}) {
  const [title, setTitle] = useState("");
  const [where, setWhere] = useState(parent);
  const [error, setError] = useState<string | null>(null);
  const isPageParent = where !== "" && !sections.some((s) => s.dir === where);
  return (
    <Card title="New page">
      <ErrorBox error={error} />
      <form className="stack" onSubmit={async (e) => {
        e.preventDefault();
        try {
          const res = await api.newPage(owner, name, { branch, parent: where, title });
          track("page_created", { child: where !== "" });
          onCreated(res.path);
        } catch (err) { setError((err as Error).message); }
      }}>
        <input autoFocus required placeholder="Page title" value={title} onChange={(e) => setTitle(e.target.value)} />
        <label>Inside
          <select value={where} onChange={(e) => setWhere(e.target.value)}>
            <option value="">Top level</option>
            {isPageParent && <option value={where}>Under the current page</option>}
            {sections.map((s) => <option key={s.dir} value={s.dir}>{s.label}</option>)}
          </select>
        </label>
        {isPageParent && <p className="muted small">The current page becomes a section with this page inside it.</p>}
        <div className="row">
          <button className="btn btn-primary" disabled={!title.trim()}>Create and edit</button>
          <button type="button" className="btn" onClick={onCancel}>Cancel</button>
        </div>
      </form>
    </Card>
  );
}

function MoveForm({ sections, onMove, onCancel }: { sections: { dir: string; label: string }[]; onMove: (parent: string) => void; onCancel: () => void }) {
  const [parent, setParent] = useState("");
  return (
    <div className="row notice">
      <span>Move to</span>
      <select value={parent} onChange={(e) => setParent(e.target.value)}>
        <option value="">Top level</option>
        {sections.map((s) => <option key={s.dir} value={s.dir}>{s.label}</option>)}
      </select>
      <button className="btn btn-primary" onClick={() => onMove(parent)}>Move</button>
      <button className="btn" onClick={onCancel}>Cancel</button>
    </div>
  );
}

function Editor({ owner, name, branch, path, initial, baseSha, onDone }: {
  owner: string; name: string; branch: string; path: string; initial: string; baseSha?: string; onDone: () => void;
}) {
  const [content, setContent] = useState(initial);
  const [message, setMessage] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState(false);
  useEffect(() => setContent(initial), [initial]);

  async function save() {
    try {
      await api.writeFile(owner, name, { branch, path, content, message, expected_sha: baseSha });
      onDone();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <Card title={`Editing ${path}`} actions={<button className="btn" onClick={() => setPreview(!preview)}>{preview ? "Write" : "Preview"}</button>}>
      <ErrorBox error={error} />
      {preview ? <Markdown>{content}</Markdown> :
        <textarea className="editor" value={content} onChange={(e) => setContent(e.target.value)} spellCheck autoFocus />}
      <p className="muted small">The first <code># Heading</code> is the page title. Add <code>order: 1</code> in front matter to pin a page's position.</p>
      <div className="row">
        <input className="grow" placeholder={`Describe your change (saved to ${branch})`} value={message} onChange={(e) => setMessage(e.target.value)} />
        <button className="btn btn-primary" onClick={save}>Save</button>
        <button className="btn" onClick={onDone}>Cancel</button>
      </div>
    </Card>
  );
}
