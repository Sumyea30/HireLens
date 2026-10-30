import { Trash2, Upload } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../api";
import { useAuth } from "../auth/AuthContext";
import { useToast } from "../components/Toast";
import { Badge, Button, Card, EmptyState, ErrorState, PageHeader, Spinner } from "../components/ui";
import { useAsync } from "../hooks/useAsync";
import { formatDay } from "../lib/format";

export default function Policies() {
  const { can } = useAuth();
  const toast = useToast();
  const { data, error, loading, reload } = useAsync(() => api.listPolicies(), []);
  const template = useAsync(() => api.getJobTemplate(), []);
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (draft === null && template.data) setDraft(template.data.text);
  }, [draft, template.data]);

  const upload = async (file: File | undefined) => {
    if (!file) return;
    setBusy(true);
    try { await api.uploadPolicy(file); toast("Policy added to the knowledge base.", "good"); reload(); } catch (e) { toast(errorMessage(e), "bad"); } finally { setBusy(false); }
  };
  const remove = async (id: string) => {
    try { await api.deletePolicy(id); toast("Policy deleted.", "good"); reload(); } catch (e) { toast(errorMessage(e), "bad"); }
  };
  const saveTemplate = async () => {
    const text = (draft ?? "").trim();
    if (!text) { toast("Write the template before saving it.", "bad"); return; }
    setSaving(true);
    try {
      const saved = await api.saveJobTemplate(text);
      setDraft(saved.text);
      toast("Job description template saved. New drafts follow it.", "good");
      reload();
    } catch (e) { toast(errorMessage(e), "bad"); } finally { setSaving(false); }
  };

  return (
    <>
      <PageHeader title="Company policies" subtitle="The job assistant checks every draft against these documents."
        actions={can("manage_policies") && <>
          <input ref={input} type="file" accept=".pdf,.docx,.txt,.md" hidden onChange={(e) => { void upload(e.target.files?.[0]); e.target.value = ""; }} />
          <Button variant="primary" loading={busy} onClick={() => input.current?.click()}><Upload size={16} /> Upload policy</Button>
        </>} />
      <div className="hint">Use synthetic policy documents only. Policy text is sent to the embedding and language-model providers listed in the privacy notes.</div>
      <Card title="Job description template">
        <p className="muted small">Gemini follows this structure when a job has no template of its own. Placeholders such as {"{{title}}"} and {"{{required_skills}}"} are filled from the job profile.</p>
        {template.loading && draft === null ? <Spinner /> : template.error && draft === null ? <ErrorState message={template.error} onRetry={template.reload} /> : (
          <div className="stack">
            <textarea aria-label="Stored job description template" rows={12} value={draft ?? ""} onChange={(e) => setDraft(e.target.value)} />
            <div className="row">
              <Button variant="primary" loading={saving} disabled={!can("manage_policies")} onClick={() => void saveTemplate()}>Save template</Button>
              <span className="muted small">{template.data?.stored ? "Saved in the policy collection." : "Built-in template. Save it to store your own."}</span>
            </div>
          </div>
        )}
      </Card>
      <Card>
        {loading ? <Spinner /> : error ? <ErrorState message={error} onRetry={reload} /> : !data?.length ? <EmptyState title="No policies uploaded" text="Upload a policy so drafts can be checked against it." /> : (
          <div className="table-wrap"><table>
            <thead><tr><th>Title</th><th>Version</th><th>Effective</th><th>Status</th><th className="num">Sections</th><th /></tr></thead>
            <tbody>{data.map((p) => (
              <tr key={p.id}>
                <td><strong>{p.title}</strong></td><td>{p.version}</td><td>{formatDay(p.effective_date)}</td>
                <td><Badge tone={p.status === "active" ? "good" : "neutral"}>{p.status}</Badge></td><td className="num">{p.chunks}</td>
                <td className="num">{can("delete") && <button className="icon-btn" aria-label={`Delete ${p.title}`} onClick={() => remove(p.id)}><Trash2 size={16} /></button>}</td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
        <p className="muted small" style={{ marginTop: 12 }}>Only the active version of each document is used for checks.</p>
      </Card>
    </>
  );
}
