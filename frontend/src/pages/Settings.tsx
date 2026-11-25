import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { adminApi, type DocType, type Me, type RequirementSetInfo } from "../api";

const split = (s: string) => s.split("\n").map((x) => x.trim()).filter(Boolean);

export default function Settings({ me }: { me: Me }) {
  const [types, setTypes] = useState<DocType[] | null>(null);
  const [extractable, setExtractable] = useState<Record<string, string>>({});
  const [sets, setSets] = useState<RequirementSetInfo[]>([]);
  const [error, setError] = useState<string | null>(null);
  const platform = me.role === "platform_admin";

  const load = useCallback(() => {
    adminApi.types().then((r) => { setTypes(r.types); setExtractable(r.extractable); }, (e: Error) => setError(e.message));
    adminApi.sets().then((r) => setSets(r.requirement_sets), (e: Error) => setError(e.message));
  }, []);
  useEffect(load, [load]);

  if (error) return <p className="page-pad error-text">{error}</p>;
  if (!types) return <p className="page-pad muted">Loading…</p>;
  return (
    <main className="page-pad settings">
      <Link to="/" className="back">← All applicants</Link>
      <h1>Settings</h1>

      <h2>Document checklists</h2>
      {sets.map((rs) => <ChecklistEditor key={rs.id} rs={rs} types={types} reload={load} />)}

      <h2>Document types</h2>
      <p className="muted small">
        Pages are recognised by built-in rules, then by the title phrases below, then by the trained model.
        {platform ? "" : " Only platform admins can add or change types."}
      </p>
      <div className="table-scroll">
      <table className="list compact">
        <thead><tr><th>Type</th><th>Values read</th><th>Title phrases</th><th className="num">Pages</th></tr></thead>
        <tbody>
          {types.map((t) => <TypeRow key={t.id} t={t} editable={platform} reload={load} />)}
        </tbody>
      </table>
      </div>
      {platform && <NewTypeForm extractable={extractable} reload={load} />}
    </main>
  );
}

function TypeRow({ t, editable, reload }: { t: DocType; editable: boolean; reload: () => void }) {
  const [editing, setEditing] = useState(false);
  const [phrases, setPhrases] = useState(t.title_phrases.join("\n"));
  const [error, setError] = useState<string | null>(null);
  const save = async (e: FormEvent) => {
    e.preventDefault();
    try {
      await adminApi.updateType(t.id, { title_phrases: split(phrases) });
      setEditing(false);
      reload();
    } catch (err) {
      setError((err as Error).message);
    }
  };
  return (
    <tr>
      <td>
        <div className="strong">{t.name}</div>
        <div className="muted small mono">{t.code}{t.builtin ? " · built-in" : ""}{t.active ? "" : " · off"}</div>
      </td>
      <td className="small">{t.fields.map((f) => f.label ?? f.key).join(", ") || "—"}</td>
      <td className="small">
        {editing ? (
          <form onSubmit={save} className="decision-form">
            <textarea rows={3} value={phrases} onChange={(e) => setPhrases(e.target.value)}
              placeholder="One phrase per line, e.g. Transcript of Records" />
            <div className="decision-buttons">
              <button className="secondary">Save</button>
              <button type="button" className="link-button small" onClick={() => setEditing(false)}>Cancel</button>
            </div>
            {error && <p className="error-text small">{error}</p>}
          </form>
        ) : (
          <>
            {t.title_phrases.length ? t.title_phrases.map((p) => <div key={p}>“{p}”</div>) : <span className="muted">—</span>}
            {editable && <button className="link-button small" onClick={() => setEditing(true)}>Edit phrases</button>}
          </>
        )}
      </td>
      <td className="num">{t.pages}</td>
    </tr>
  );
}

function NewTypeForm({ extractable, reload }: { extractable: Record<string, string>; reload: () => void }) {
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [category, setCategory] = useState("academic_record");
  const [fields, setFields] = useState<string[]>(["candidate_name"]);
  const [custom, setCustom] = useState("");
  const [phrases, setPhrases] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setMsg(null);
    const customFields = split(custom).map((label) => ({
      key: label.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, ""), label,
    }));
    try {
      await adminApi.createType({ code, name, category, title_phrases: split(phrases),
        fields: [...fields.map((key) => ({ key })), ...customFields] });
      setMsg({ ok: true, text: `Added “${name}”. Re-classify existing pages with: make classify force=1` });
      setCode(""); setName(""); setPhrases(""); setCustom("");
      reload();
    } catch (err) {
      setMsg({ ok: false, text: (err as Error).message });
    }
  };
  return (
    <details className="card new-type">
      <summary className="strong">Add a document type</summary>
      <form onSubmit={submit} className="auth-form wide">
        <label>Name<input required value={name} onChange={(e) => setName(e.target.value)} placeholder="Transcript of records" /></label>
        <label>Code (lower-case, used internally)<input required pattern="[a-z][a-z0-9_]{2,62}" value={code}
          onChange={(e) => setCode(e.target.value)} placeholder="transcript" /></label>
        <label>Kind
          <select value={category} onChange={(e) => setCategory(e.target.value)}>
            <option value="academic_record">Academic record (values are read)</option>
            <option value="supporting">Supporting document</option>
          </select>
        </label>
        <fieldset className="choices">
          <legend>Values to read automatically</legend>
          {Object.entries(extractable).map(([k, label]) => (
            <label key={k} className="choice">
              <input type="checkbox" checked={fields.includes(k)}
                onChange={(e) => setFields(e.target.checked ? [...fields, k] : fields.filter((x) => x !== k))} />
              {label}
            </label>
          ))}
        </fieldset>
        <label>Other values (entered by verifiers), one per line
          <textarea rows={2} value={custom} onChange={(e) => setCustom(e.target.value)} placeholder="Total credits" />
        </label>
        <label>Title phrases that identify it (one per line, as printed in the page header)
          <textarea rows={3} required value={phrases} onChange={(e) => setPhrases(e.target.value)}
            placeholder="Transcript of Records" />
        </label>
        {msg && <p className={`small ${msg.ok ? "ok-text" : "error-text"}`}>{msg.text}</p>}
        <button className="primary">Add document type</button>
      </form>
    </details>
  );
}

function ChecklistEditor({ rs, types, reload }: { rs: RequirementSetInfo; types: DocType[]; reload: () => void }) {
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [label, setLabel] = useState("");
  const [accepted, setAccepted] = useState<string[]>([]);
  const act = async (fn: () => Promise<unknown>) => {
    setError(null);
    try { await fn(); reload(); } catch (e) { setError((e as Error).message); }
  };
  const names = Object.fromEntries(types.map((t) => [t.code, t.name]));
  return (
    <section className="card">
      <div className="strong">{rs.name}</div>
      <table className="list compact">
        <thead><tr><th>Document</th><th>Accepted types</th><th>Required</th></tr></thead>
        <tbody>
          {rs.items.map((it) => (
            <tr key={it.id}>
              <td>{it.label}{it.condition && <div className="muted small">depends on the student's answers</div>}</td>
              <td className="small">{it.accepted_types.map((c) => names[c] ?? c).join(" or ")}</td>
              <td>
                <input type="checkbox" checked={it.required} aria-label={`${it.label} required`}
                  onChange={(e) => act(() => adminApi.updateItem(it.id, { required: e.target.checked }))} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {!adding ? (
        <button className="link-button small" onClick={() => setAdding(true)}>+ Add a document to this checklist</button>
      ) : (
        <form className="auth-form wide" onSubmit={(e) => { e.preventDefault(); act(async () => {
          const key = label.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "");
          await adminApi.addItem(rs.id, { key, label, accepted_types: accepted, required: true });
          setAdding(false); setLabel(""); setAccepted([]);
        }); }}>
          <label>Label shown to students<input required value={label} onChange={(e) => setLabel(e.target.value)} /></label>
          <fieldset className="choices">
            <legend>Accepted document types</legend>
            {types.filter((t) => t.active).map((t) => (
              <label key={t.code} className="choice">
                <input type="checkbox" checked={accepted.includes(t.code)}
                  onChange={(e) => setAccepted(e.target.checked ? [...accepted, t.code] : accepted.filter((x) => x !== t.code))} />
                {t.name}
              </label>
            ))}
          </fieldset>
          <div className="decision-buttons">
            <button className="primary" disabled={!accepted.length}>Add</button>
            <button type="button" className="link-button" onClick={() => setAdding(false)}>Cancel</button>
          </div>
        </form>
      )}
      {error && <p className="error-text small">{error}</p>}
    </section>
  );
}
