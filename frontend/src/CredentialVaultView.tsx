import { useEffect, useState } from "react";
import { deleteAutomationCredential, fetchAutomationCredentials, saveAutomationCredential } from "./api";
import type { AutomationCredential, AutomationCredentialList } from "./types";

type Draft = {
  id?: string;
  name: string;
  kind: "windows" | "ssh" | "service";
  username: string;
  secret: string;
  scope: "global" | "endpoint";
};

const EMPTY: Draft = { name: "", kind: "windows", username: "", secret: "", scope: "endpoint" };

export function CredentialVaultView() {
  const [data, setData] = useState<AutomationCredentialList | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [remove, setRemove] = useState<AutomationCredential | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function load() {
    try {
      setData(await fetchAutomationCredentials());
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load the Automation Vault");
    }
  }

  useEffect(() => { void load(); }, []);

  async function save() {
    if (!draft) return;
    setBusy(true);
    setError("");
    try {
      await saveAutomationCredential(draft);
      setDraft(null);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the credential");
    } finally {
      setBusy(false);
    }
  }

  async function confirmRemove() {
    if (!remove) return;
    setBusy(true);
    try {
      await deleteAutomationCredential(remove.id);
      setRemove(null);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not delete the credential");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <section className="panel vault-view">
        <header>
          <div>
            <h2>Automation Vault</h2>
            <p>Reusable Windows, Linux SSH, and service credentials. Secrets are encrypted locally and are never returned to the browser or copied into jobs.</p>
          </div>
          <button className="accent" onClick={() => setDraft({ ...EMPTY })}>Add credential</button>
        </header>
        {error ? <div className="banner bad">{error}</div> : null}
        <div className="vault-storage">{data?.storage || "Encrypted local JSON vault"}</div>
        <div className="vault-list">
          {(data?.credentials || []).map((credential) => (
            <article key={credential.id} className="vault-card">
              <div>
                <strong>{credential.name}</strong>
                <span className="chip">{credential.kind === "ssh" ? "Linux / SSH" : credential.kind}</span>
                <small>{credential.username || "Secret-only credential"} · {credential.scope === "global" ? "All endpoints" : "Current endpoint"}</small>
              </div>
              <div className="vault-actions">
                <button className="ghost" onClick={() => setDraft({ id: credential.id, name: credential.name, kind: credential.kind, username: credential.username, secret: "", scope: credential.scope })}>Edit</button>
                <button className="danger" onClick={() => setRemove(credential)}>Delete</button>
              </div>
            </article>
          ))}
          {data && data.credentials.length === 0 ? <div className="empty">No automation credentials saved yet.</div> : null}
        </div>
      </section>

      {draft ? (
        <div className="modal-back" onClick={() => !busy && setDraft(null)}>
          <form className="modal vault-modal" onClick={(event) => event.stopPropagation()} onSubmit={(event) => { event.preventDefault(); void save(); }}>
            <h2>{draft.id ? "Edit credential" : "Add credential"}</h2>
            <p>{draft.id ? "Leave the secret blank to retain the encrypted value already stored." : "The secret is encrypted before it is written to disk."}</p>
            <div className="vault-form-grid">
              <label>Name<input required value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></label>
              <label>Type<select value={draft.kind} onChange={(event) => setDraft({ ...draft, kind: event.target.value as Draft["kind"] })}><option value="windows">Windows / WinRM</option><option value="ssh">Linux / SSH</option><option value="service">Service credential</option></select></label>
              <label>Username<input required={draft.kind !== "service"} autoComplete="username" value={draft.username} onChange={(event) => setDraft({ ...draft, username: event.target.value })} /></label>
              <label>Password or token<input required={!draft.id} type="password" autoComplete="new-password" value={draft.secret} onChange={(event) => setDraft({ ...draft, secret: event.target.value })} /></label>
              <label>Availability<select value={draft.scope} onChange={(event) => setDraft({ ...draft, scope: event.target.value as Draft["scope"] })}><option value="endpoint">Current vSphere endpoint</option><option value="global">All endpoints</option></select></label>
            </div>
            <div className="modal-actions"><button type="button" className="ghost" onClick={() => setDraft(null)}>Cancel</button><button className="accent" disabled={busy}>{busy ? "Saving…" : "Save credential"}</button></div>
          </form>
        </div>
      ) : null}

      {remove ? (
        <div className="modal-back" onClick={() => !busy && setRemove(null)}>
          <div className="modal confirm-modal" role="alertdialog" onClick={(event) => event.stopPropagation()}>
            <h2>Delete {remove.name}?</h2>
            <p>This permanently removes its encrypted secret. Open jobs that reference it must be completed or cancelled first.</p>
            <div className="modal-actions"><button className="ghost" onClick={() => setRemove(null)}>Keep credential</button><button className="danger" disabled={busy} onClick={() => void confirmRemove()}>{busy ? "Deleting…" : "Delete credential"}</button></div>
          </div>
        </div>
      ) : null}
    </>
  );
}
