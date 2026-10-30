import { Download, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api";
import { Badge, Button, Card, EmptyState, ErrorState, PageHeader, Spinner, Tabs } from "../components/ui";
import { useAsync } from "../hooks/useAsync";
import { formatDate, titleCase } from "../lib/format";
import { clearEntries, downloadText, getEntries, getLevel, setLevel, subscribe, toLogText, type Level } from "../lib/logger";

export default function Logs() {
  const [tab, setTab] = useState<"audit" | "client">("audit");
  return (
    <>
      <PageHeader title="Activity log" subtitle="Who did what, and what the browser reported. Logs hold IDs and outcomes, never personal data." />
      <Card><Tabs tabs={[{ id: "audit", label: "Audit log (server)" }, { id: "client", label: "This browser" }]} value={tab} onChange={setTab} />
        {tab === "audit" ? <Audit /> : <Client />}
      </Card>
    </>
  );
}

function Audit() {
  const { data, error, loading, reload } = useAsync(() => api.getAudit(), []);
  const [q, setQ] = useState("");
  if (loading) return <Spinner />;
  if (error || !data) return <ErrorState message={error ?? "No data"} onRetry={reload} />;
  const rows = data.filter((e) => `${e.action} ${e.entity} ${e.entity_id} ${e.actor_role}`.toLowerCase().includes(q.toLowerCase()));
  const text = rows.map((e) => `${e.ts} ${e.outcome.toUpperCase().padEnd(6)} ${e.actor_role} ${e.action} ${e.entity}:${e.entity_id}`).join("\n");
  return (
    <div className="stack">
      <div className="row between">
        <input aria-label="Filter the audit log" placeholder="Filter by action, role or ID" value={q} onChange={(e) => setQ(e.target.value)} style={{ maxWidth: 320 }} />
        <Button disabled={!rows.length} onClick={() => downloadText("hirelens-audit.log", text)}><Download size={16} /> Download .log</Button>
      </div>
      {rows.length === 0 ? <EmptyState title="No matching events" /> : (
        <div className="table-wrap"><table>
          <thead><tr><th>Time</th><th>Who</th><th>Action</th><th>Item</th><th>Result</th></tr></thead>
          <tbody>{rows.map((e) => (
            <tr key={e.id}><td>{formatDate(e.ts)}</td><td>{e.actor_role}</td><td>{titleCase(e.action)}</td><td className="muted">{e.entity}:{e.entity_id}</td>
              <td><Badge tone={e.outcome === "ok" ? "good" : e.outcome === "denied" ? "warn" : "bad"}>{e.outcome}</Badge></td></tr>
          ))}</tbody>
        </table></div>
      )}
    </div>
  );
}

function Client() {
  const [, tick] = useState(0);
  const [level, setLvl] = useState<Level>(getLevel());
  useEffect(() => subscribe(() => tick((n) => n + 1)), []);
  const entries = getEntries();
  const shown = entries.slice(-200).reverse();
  return (
    <div className="stack">
      <div className="row between">
        <div className="row">
          <label htmlFor="lvl" className="muted small">Detail</label>
          <select id="lvl" value={level} onChange={(e) => { const l = e.target.value as Level; setLevel(l); setLvl(l); }} style={{ width: 120 }}>
            {(["debug", "info", "warn", "error"] as Level[]).map((l) => <option key={l} value={l}>{l}</option>)}
          </select>
          <span className="muted small">{entries.length} entries kept</span>
        </div>
        <div className="row">
          <Button onClick={() => clearEntries()}><Trash2 size={16} /> Clear</Button>
          <Button variant="primary" disabled={!entries.length} onClick={() => downloadText("hirelens-client.log", toLogText(entries))}><Download size={16} /> Download .log</Button>
        </div>
      </div>
      {shown.length === 0 ? <EmptyState title="Nothing logged yet" /> : shown.map((e, i) => (
        <div key={i} className="log-line"><Badge tone={e.level === "error" ? "bad" : e.level === "warn" ? "warn" : "neutral"}>{e.level}</Badge> {e.ts.slice(11, 19)} {e.event} {Object.keys(e.ctx).length ? JSON.stringify(e.ctx) : ""}</div>
      ))}
    </div>
  );
}
