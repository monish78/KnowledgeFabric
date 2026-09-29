import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, formatSize } from "../api.js";
import { DropZone, ErrorBox, RoleBadge, StatusText, TypeBadge, usePoll } from "../components/common.jsx";

const GRAPH_ACCEPT = ".csv,.xlsx,.xlsm";
const RAG_ACCEPT = ".pdf,.docx,.txt,.md";
const BUSY = ["extracting", "building", "ingesting"];

const suggestName = (file, kind) =>
  file.name.replace(/\.[^.]+$/, "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "")
    .replace(/^(\d)/, "kb_$1").slice(0, 55) + (kind === "graph" ? "_kg" : "_rag");

export default function Workspace() {
  const navigate = useNavigate();
  const [kbs, setKbs] = useState(null);
  const [mode, setMode] = useState("graph");
  const [graphFile, setGraphFile] = useState(null);
  const [ragFiles, setRagFiles] = useState([]);
  const [ragRuns, setRagRuns] = useState({ kb: null, runs: [] });
  const [form, setForm] = useState({ name: "", domain: "", sub: "" });
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => api("/kbs").then(setKbs).catch(setError), []);
  useEffect(() => { load(); }, [load]);
  usePoll(load, 3000, !!kbs && kbs.some((k) => BUSY.includes(k.status)));
  usePoll(() => ragRuns.kb && api(`/kbs/${ragRuns.kb}/runs`).then((runs) => setRagRuns((r) => ({ ...r, runs }))),
          2000, !!ragRuns.kb && (ragRuns.runs.length === 0 || ragRuns.runs.some((r) => r.status === "running")));

  const files = mode === "graph" ? (graphFile ? [graphFile] : []) : ragFiles;
  const choose = (kind, list) => {
    setMode(kind);
    setError(null);
    if (kind === "graph") setGraphFile(list[0]);
    else setRagFiles(list);
    setForm((f) => ({ ...f, name: f.name || suggestName(list[0], kind) }));
  };
  const reset = () => {
    setGraphFile(null);
    setRagFiles([]);
    setForm({ name: "", domain: "", sub: "" });
    setError(null);
  };

  const create = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const fd = new FormData();
      fd.append("kb_name", form.name.trim());
      fd.append("kb_type", mode);
      fd.append("domain", form.domain.trim());
      fd.append("sub_domain", form.sub.trim());
      files.forEach((f) => fd.append("files", f));
      const r = await api("/kbs", { method: "POST", form: fd });
      if (mode === "graph") {
        navigate(`/kbs/${r.kb_name}/jobs/${r.job_id}`);
      } else {
        setRagRuns({ kb: r.kb_name, runs: [] });
        reset();
        load();
      }
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const action = (kb) => {
    if (kb.status === "awaiting_review" && kb.role === "owner")
      return <button className="btn sec sm" onClick={() => navigate(`/kbs/${kb.kb_name}/review`)}>Review</button>;
    if (BUSY.includes(kb.status) && kb.last_job_id)
      return <button className="btn sec sm" onClick={() => navigate(`/kbs/${kb.kb_name}/jobs/${kb.last_job_id}`)}>View progress</button>;
    if (kb.status === "failed" && kb.role === "owner" && kb.kb_type === "graph")
      return <button className="btn sec sm" onClick={async () => {
        try { const r = await api(`/kbs/${kb.kb_name}/extract`, { method: "POST" }); navigate(`/kbs/${kb.kb_name}/jobs/${r.job_id}`); }
        catch (err) { setError(err); }
      }}>Retry extraction</button>;
    if (kb.status === "ready" && kb.role === "owner")
      return <button className="btn sec sm" onClick={() => navigate(`/access?kb=${kb.kb_name}`)}>Manage access</button>;
    if (kb.status === "ready")
      return <button className="btn sec sm" onClick={() => navigate(`/chat?kb=${kb.kb_name}`)}>Chat</button>;
    return null;
  };

  const runBadge = (r) => r.status === "completed" ? <span className="badge">Indexed</span>
    : r.status === "failed" ? <span className="badge r" title={r.summary}>Failed</span>
    : <span className="badge w">Embedding {Math.round(r.progress || 0)}%</span>;

  return (
    <div className="split" style={{ display: "flex", flexGrow: 1, minHeight: 0 }}>
      <aside style={{ width: 380, background: "#fff", borderRight: "1px solid var(--line)", padding: 24, display: "flex",
                      flexDirection: "column", gap: 20 }}>
        <div className="section">Upload data</div>
        <div className="card" style={{ padding: 18, display: "flex", flexDirection: "column", gap: 12,
                                       border: mode === "rag" ? "2px solid var(--teal)" : undefined }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div style={{ fontSize: 15, fontWeight: 600 }}>RAG documents</div><span className="badge g">Vector store</span>
          </div>
          <div className="muted" style={{ fontSize: 13, lineHeight: 1.45 }}>
            PDF, DOCX or TXT. Chunked and embedded automatically in the background, no review step.
          </div>
          <DropZone accept={RAG_ACCEPT} multiple title="Drop files here" onFiles={(l) => choose("rag", l)} />
          <div>
            {ragFiles.map((f) => (
              <div className="file" key={f.name}><span style={{ flexGrow: 1 }}>{f.name}</span>
                <span className="muted small">{formatSize(f.size)}</span></div>
            ))}
            {ragRuns.runs.map((r) => (
              <div className="file" key={r.id}><span style={{ flexGrow: 1 }}>{r.source_file}</span>{runBadge(r)}</div>
            ))}
          </div>
        </div>
        <div className="card" style={{ padding: 18, display: "flex", flexDirection: "column", gap: 12,
                                       border: mode === "graph" ? "2px solid var(--teal)" : undefined }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div style={{ fontSize: 15, fontWeight: 600 }}>Knowledge graph data</div><span className="badge b">Graph</span>
          </div>
          <div className="muted" style={{ fontSize: 13, lineHeight: 1.45 }}>
            CSV or XLSX. An LLM extracts nodes, relationships and Cypher. You review them before the graph is built.
          </div>
          <DropZone accept={GRAPH_ACCEPT} title="Drop CSV or XLSX here" onFiles={(l) => choose("graph", l)} />
          {graphFile && (
            <div className="file" style={{ background: "var(--bg)", borderRadius: 10, padding: "10px 12px" }}>
              <div style={{ flexGrow: 1 }}>
                <div style={{ fontWeight: 600 }}>{graphFile.name}</div>
                <div className="muted small">{formatSize(graphFile.size)}</div>
              </div>
              <button className="btn sec sm" type="button" onClick={() => setGraphFile(null)}
                      aria-label={`Remove ${graphFile.name}`}>Remove</button>
            </div>
          )}
        </div>
      </aside>

      <main className="page" style={{ minWidth: 0 }}>
        <div>
          <h1>Create knowledge base</h1>
          <div className="sub">You become the owner and are the only person who can grant access.</div>
        </div>
        <form className="card" onSubmit={create} style={{ padding: 24, display: "flex", flexDirection: "column", gap: 16 }}>
          <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
            {mode === "graph" ? <span className="badge b">Knowledge graph</span> : <span className="badge g">RAG store</span>}
            <span className="muted small">
              {files.length ? `Source: ${files.map((f) => f.name).join(", ")}` : "Drop a file in the upload panel to start"}
            </span>
          </div>
          <div>
            <label className="lbl" htmlFor="kbname">{mode === "graph" ? "Knowledge graph name" : "Knowledge base name"}</label>
            <input id="kbname" className="inp" value={form.name} placeholder="e.g. retail_supply_chain_kg"
                   pattern="[a-z][a-z0-9_]{2,62}" title="3-63 characters: lowercase letters, digits and underscores"
                   onChange={(e) => setForm({ ...form, name: e.target.value })} required />
          </div>
          <div className="row">
            <div className="grow">
              <label className="lbl" htmlFor="domain">Domain</label>
              <input id="domain" className="inp" value={form.domain} placeholder="e.g. Retail"
                     onChange={(e) => setForm({ ...form, domain: e.target.value })} required />
            </div>
            <div className="grow">
              <label className="lbl" htmlFor="sub">Sub-domain</label>
              <input id="sub" className="inp" value={form.sub} placeholder="e.g. Supply chain"
                     onChange={(e) => setForm({ ...form, sub: e.target.value })} required />
            </div>
          </div>
          <ErrorBox error={error} />
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 12 }}>
            <button className="btn sec" type="button" onClick={reset}>Cancel</button>
            <button className="btn" type="submit" disabled={!files.length || busy}>
              {busy ? "Uploading…" : mode === "graph" ? "Create and extract graph" : "Create and index documents"}
            </button>
          </div>
        </form>

        <div className="section">Your knowledge bases</div>
        <div className="card" style={{ overflowX: "auto" }}>
          <table>
            <thead><tr><th>Name</th><th>Type</th><th>Domain / sub-domain</th><th>Your role</th><th>Status</th><th /></tr></thead>
            <tbody>
              {kbs && kbs.length === 0 && (
                <tr><td colSpan={6} className="muted">No knowledge bases yet. Upload a file to create one.</td></tr>
              )}
              {(kbs || []).map((kb) => (
                <tr key={kb.kb_name}>
                  <td style={{ fontWeight: 600 }}>{kb.kb_name}</td>
                  <td><TypeBadge type={kb.kb_type} /></td>
                  <td>{kb.domain} / {kb.sub_domain}</td>
                  <td><RoleBadge role={kb.role} status={kb.status} /></td>
                  <td><StatusText kb={kb} /></td>
                  <td style={{ textAlign: "right" }}>{action(kb)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </main>
    </div>
  );
}
