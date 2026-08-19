import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchCatalog, fetchConnection, fetchInventory, fetchJobs, logout, runActions, setUiToken } from "./api";
import { BatchCount } from "./BatchCount";
import { ChangelogModal } from "./ChangelogModal";
import { BatchLimitDialog } from "./BatchLimitDialog";
import { BATCH_LIMIT } from "./batch";
import { CloneVmModal } from "./CloneVmModal";
import { DatastoresView } from "./DatastoresView";
import { downloadText, gib, bytes, powerLabel, powerClass, relTime, csvEscape, diskLabel } from "./format";
import { JobsView } from "./JobsView";
import { LoginPanel } from "./LoginPanel";
import { MigrateVmModal } from "./MigrateVmModal";
import { MonitoringView } from "./MonitoringView";
import {
  filterMachines,
  filterReclaimVms,
  sortMachines,
  sortOwners,
  sortReclaimVms,
  toggleSortDir,
  type MachineFilters,
  type MachineSortKey,
  type OwnerSortKey,
  type ReclaimFilters,
  type ReclaimSortKey,
  type SortDir,
} from "./sort";
import type { ActionName, Catalog, ConnectionInfo, InventorySnapshot, JobList, OwnerReport, VirtualMachine } from "./types";
import { APP_VERSION } from "./version";
import { ThemePanel } from "./ThemePanel";
import { ThemeProvider } from "./ThemeContext";

type View = "overview" | "monitoring" | "hosts" | "machines" | "datastores" | "jobs" | "owners" | "reclaim";

const ACTIONS: { id: ActionName; label: string; danger?: boolean; hint: string }[] = [
  { id: "shutdown", label: "Guest shutdown", hint: "Graceful stop via VMware Tools" },
  { id: "power_off", label: "Hard power off", danger: true, hint: "Immediate power off" },
  { id: "start", label: "Power on", hint: "Start a powered-off VM" },
  { id: "reboot", label: "Guest reboot", hint: "Graceful reboot via Tools" },
  { id: "reset", label: "Reset", danger: true, hint: "Hard reset" },
  { id: "suspend", label: "Suspend", hint: "Pause the VM" },
  {
    id: "destroy",
    label: "Delete from disk",
    danger: true,
    hint: "Permanently deletes the VM and its disks from the datastore. Powered-on VMs are powered off first. This cannot be undone",
  },
];

function PassphraseField({ onSave }: { onSave: () => void }) {
  const saved = localStorage.getItem("vfleet.uiToken") ?? "";
  const [value, setValue] = useState(saved);
  const dirty = value.trim() !== saved;

  function save() {
    setUiToken(value.trim());
    onSave();
  }

  return (
    <div className="settings-section">
      <label
        className="token settings-token"
        title="Protects this dashboard from other users on the same network. Set a passphrase here and in VFLEET_UI_TOKEN on the server — the UI is blocked until the token matches."
      >
        <span className="settings-token-label">Dashboard passphrase</span>
        <div className="passphrase-row">
          <input
            type="password"
            placeholder="optional"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && dirty) save(); }}
          />
          {dirty && (
            <button className="passphrase-save-btn" onClick={save}>
              Save
            </button>
          )}
        </div>
        <span className="token-hint">
          Lock this UI to a passphrase — set the same value in <code>VFLEET_UI_TOKEN</code> on the server.
        </span>
      </label>
    </div>
  );
}

export function App() {
  const [view, setView] = useState<View>("overview");
  const [data, setData] = useState<InventorySnapshot | null>(null);
  const [error, setError] = useState("");
  const [q, setQ] = useState("");
  const [qInput, setQInput] = useState("");
  const [owner, setOwner] = useState("");
  const [ownerInput, setOwnerInput] = useState("");
  const [cluster, setCluster] = useState("");
  const [power, setPower] = useState("");
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  const [pending, setPending] = useState<ActionName | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [expandedOwner, setExpandedOwner] = useState("");
  const [showLogin, setShowLogin] = useState(false);
  const [connection, setConnection] = useState<ConnectionInfo | null>(null);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [jobs, setJobs] = useState<JobList | null>(null);
  const [showClone, setShowClone] = useState(false);
  const [showMigrate, setShowMigrate] = useState(false);
  const [migrateTargets, setMigrateTargets] = useState<VirtualMachine[] | null>(null);
  const [actionTargets, setActionTargets] = useState<VirtualMachine[] | null>(null);
  const [batchIntent, setBatchIntent] = useState<"migrate" | ActionName | null>(null);
  const [monitorOwner, setMonitorOwner] = useState("");
  const [showChangelog, setShowChangelog] = useState(false);
  const [showTheme, setShowTheme] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const themeRef = useRef<HTMLDivElement>(null);
  const settingsRef = useRef<HTMLDivElement>(null);

  const openMachinesForOwner = useCallback((ownerKey: string) => {
    setOwnerInput(ownerKey);
    setOwner(ownerKey);
    setView("machines");
  }, []);

  function setIfChanged<T>(setter: React.Dispatch<React.SetStateAction<T>>) {
    return (next: T) => setter((prev) => (JSON.stringify(prev) === JSON.stringify(next) ? prev : next));
  }

  async function load() {
    const setDataIfChanged = setIfChanged(setData);
    const setConnectionIfChanged = setIfChanged(setConnection);
    const setCatalogIfChanged = setIfChanged(setCatalog);
    const setJobsIfChanged = setIfChanged(setJobs);

    try {
      const snapshot = await fetchInventory({ q, owner, cluster, power });
      setDataIfChanged(snapshot);
      setConnectionIfChanged(snapshot.connection);
      setError("");
      const neverSynced = !snapshot.connection.last_sync && snapshot.vms.length === 0;
      if (snapshot.connection.mode === "vcenter" && !snapshot.connection.connected && neverSynced) {
        setShowLogin(true);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load inventory");
      try {
        const info = await fetchConnection();
        setConnectionIfChanged(info);
        if (info.mode === "vcenter" && !info.connected && !info.last_sync) setShowLogin(true);
      } catch {
        setShowLogin(true);
      }
    }
    try {
      setCatalogIfChanged(await fetchCatalog());
    } catch {
      /* catalog fills after the first successful sync */
    }
    try {
      setJobsIfChanged(await fetchJobs());
    } catch {
      /* jobs endpoint should exist once the API is up */
    }
  }

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setQ(qInput);
      setOwner(ownerInput);
    }, 250);
    return () => window.clearTimeout(timer);
  }, [qInput, ownerInput]);

  useEffect(() => {
    void load();
  }, [q, owner, cluster, power]);

  useEffect(() => {
    if (showLogin) return;
    const timer = window.setInterval(() => void load(), 8000);
    return () => window.clearInterval(timer);
  }, [q, owner, cluster, power, showLogin]);

  useEffect(() => {
    if (!showTheme) return;
    function onDown(e: MouseEvent) {
      if (themeRef.current && !themeRef.current.contains(e.target as Node)) {
        setShowTheme(false);
      }
    }
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [showTheme]);

  useEffect(() => {
    if (!showSettings) return;
    function onDown(e: MouseEvent) {
      if (settingsRef.current && !settingsRef.current.contains(e.target as Node)) {
        setShowSettings(false);
        setShowTheme(false);
      }
    }
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [showSettings]);

  const selectedIds = useMemo(
    () => Object.entries(selected).filter(([, on]) => on).map(([id]) => id),
    [selected],
  );

  function selectedFromData(): VirtualMachine[] {
    return (data?.vms ?? []).filter((vm) => selected[vm.id]);
  }

  const openMigrate = useCallback(() => {
    const rows = selectedFromData();
    if (rows.length === 0) return;
    if (rows.length > BATCH_LIMIT) {
      setBatchIntent("migrate");
      return;
    }
    setMigrateTargets(rows);
    setShowMigrate(true);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, selected]);

  function requestAction(action: ActionName) {
    const rows = selectedFromData();
    if (rows.length === 0) return;
    if (rows.length > BATCH_LIMIT) {
      setBatchIntent(action);
      return;
    }
    setActionTargets(rows);
    setPending(action);
  }

  function acceptBatch(included: VirtualMachine[]) {
    const intent = batchIntent;
    setBatchIntent(null);
    if (!intent) return;
    if (intent === "migrate") {
      setMigrateTargets(included);
      setShowMigrate(true);
      return;
    }
    setActionTargets(included);
    setPending(intent);
  }

  function dropCompleted(ids: string[]) {
    const done = new Set(ids);
    setSelected((current) => {
      const next = { ...current };
      for (const id of done) delete next[id];
      return next;
    });
  }

  const reclaim = useMemo(
    () => (data?.vms ?? []).filter((vm) => vm.power_state === "POWERED_ON" && vm.idle_score >= 40),
    [data],
  );

  const toggle = useCallback((id: string) => {
    setSelected((current) => ({ ...current, [id]: !current[id] }));
  }, []);

  const toggleMany = useCallback((vms: VirtualMachine[], on?: boolean) => {
    setSelected((current) => {
      const next = { ...current };
      const enable = on ?? !vms.every((vm) => current[vm.id]);
      for (const vm of vms) next[vm.id] = enable;
      return next;
    });
  }, []);

  async function confirmAction() {
    const targets = actionTargets ?? selectedFromData();
    if (!pending || targets.length === 0) return;
    setBusy(true);
    try {
      const results = await runActions(
        targets.map((vm) => vm.id),
        pending,
      );
      const failed = results.filter((row) => !row.ok);
      setNotice(
        failed.length
          ? `${results.length - failed.length} succeeded, ${failed.length} failed: ${failed.map((row) => `${row.name || row.vm_id}: ${row.message}`).join("; ")}`
          : `${results.length} ${pending} request(s) queued locally. The relay will run them even if vCenter drops.`,
      );
      dropCompleted(targets.map((vm) => vm.id));
      setPending(null);
      setActionTargets(null);
      await load();
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Action failed");
    } finally {
      setBusy(false);
    }
  }

  async function onDisconnect(forget: boolean) {
    try {
      const info = await logout(forget);
      setConnection(info);
      setNotice(forget ? "Disconnected and cleared saved vCenter credentials." : "Disconnected. Demo inventory is active.");
      await load();
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Disconnect failed");
    }
  }

  function exportOwners(rows: OwnerReport[]) {
    const header = [
      "owner",
      "source",
      "vms",
      "on",
      "off",
      "suspended",
      "vcpu",
      "memory_gib",
      "idle_candidates",
      "reclaimable_gib",
      "vm_names",
    ];
    const lines = [header.join(",")];
    for (const row of rows) {
      lines.push(
        [
          csvEscape(row.owner_key),
          row.owner_source,
          row.vm_count,
          row.powered_on,
          row.powered_off,
          row.suspended,
          row.cpu_count,
          (row.memory_mib / 1024).toFixed(1),
          row.idle_candidates,
          (row.reclaimable_memory_mib / 1024).toFixed(1),
          csvEscape(row.vms.join(" ")),
        ].join(","),
      );
    }
    downloadText("vfleet-owners.csv", lines.join("\n"));
  }

  const vms = useMemo(() => data?.vms ?? [], [data]);
  const owners = useMemo(() => data?.owners ?? [], [data]);
  const hosts = useMemo(() => data?.hosts ?? [], [data]);
  const clusters = useMemo(() => data?.clusters ?? [], [data]);
  const totals = useMemo(() => summarize(vms), [vms]);

  const onVmSelectVisible = useCallback((rows: VirtualMachine[]) => toggleMany(rows), [toggleMany]);
  const onReclaimSelectVisible = useCallback((rows: VirtualMachine[]) => toggleMany(rows, true), [toggleMany]);
  const onOwnerSelectGroup = useCallback((group: VirtualMachine[]) => {
    toggleMany(group, true);
    setView("machines");
  }, [toggleMany]);
  const onExportOwners = useCallback(() => exportOwners(owners), [owners]);
  const onDatastoreQueued = useCallback((_job: unknown, message: string) => {
    setNotice(message);
    setView("jobs");
    void load();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const onDatastoreNotice = useCallback((message: string) => {
    setNotice(message);
    void load();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const onJobsRefresh = useCallback(() => void load(), []); // eslint-disable-line react-hooks/exhaustive-deps

  return (
  <ThemeProvider>
    <div className="shell">
      <aside className="rail">
        <div className="brand">
          <span className="mark" />
          <div>
            <strong>vFleet</strong>
            <em>vCenter control</em>
          </div>
          <button
            className="version-badge"
            onClick={() => setShowChangelog(true)}
            title={`vFleet v${APP_VERSION} — view release notes`}
          >
            v{APP_VERSION}
          </button>
        </div>
        <nav>
          {(
            [
              ["overview", "Overview"],
              ["monitoring", "Monitoring"],
              ["hosts", "Hosts"],
              ["machines", "Machines"],
              ["datastores", "Datastores"],
              ["jobs", "Jobs"],
              ["owners", "Owners"],
              ["reclaim", "Reclaim"],
            ] as [View, string][]
          ).map(([id, label]) => (
            <button key={id} className={view === id ? "active" : ""} onClick={() => setView(id)}>
              {label}
              {id === "reclaim" && reclaim.length > 0 ? <span className="count">{reclaim.length}</span> : null}
              {id === "jobs" && (jobs?.queued || 0) + (jobs?.active || 0) > 0 ? (
                <span className="count">{(jobs?.queued || 0) + (jobs?.active || 0)}</span>
              ) : null}
            </button>
          ))}
        </nav>
        <div className="rail-foot">
          <StatusPill connection={connection} loading={!data && !error} />
          {connection?.mode === "vcenter" ? (
            <>
              {!connection.connected ? (
                <button className="accent wide" onClick={() => setShowLogin(true)}>
                  Reconnect to vCenter
                </button>
              ) : null}
              {connection.can_disconnect ? (
                <button className="ghost wide" onClick={() => void onDisconnect(false)}>
                  Work offline
                </button>
              ) : null}
            </>
          ) : (
            <button className="accent wide" onClick={() => setShowLogin(true)}>
              Connect to vCenter
            </button>
          )}
        </div>

        <div className="rail-settings-wrap" ref={settingsRef}>
          <button
            className={`rail-theme-btn${showSettings ? " active" : ""}`}
            onClick={() => { setShowSettings((v) => !v); setShowTheme(false); }}
            title="Settings"
            aria-label="Settings"
            aria-expanded={showSettings}
          >
            <svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true" className="cog-icon">
              <path d="M10 12.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Z" fill="currentColor"/>
              <path fillRule="evenodd" clipRule="evenodd" d="M8.257 2.104a.75.75 0 0 1 1.486 0l.244.98a6.02 6.02 0 0 1 1.308.541l.855-.536a.75.75 0 0 1 .97.116l1.414 1.414a.75.75 0 0 1 .116.97l-.536.855c.214.41.394.845.541 1.308l.98.244a.75.75 0 0 1 0 1.486l-.98.244a6.02 6.02 0 0 1-.541 1.308l.536.855a.75.75 0 0 1-.116.97l-1.414 1.414a.75.75 0 0 1-.97.116l-.855-.536a6.02 6.02 0 0 1-1.308.541l-.244.98a.75.75 0 0 1-1.486 0l-.244-.98a6.02 6.02 0 0 1-1.308-.541l-.855.536a.75.75 0 0 1-.97-.116L3.466 14.22a.75.75 0 0 1-.116-.97l.536-.855a6.02 6.02 0 0 1-.541-1.308l-.98-.244a.75.75 0 0 1 0-1.486l.98-.244a6.02 6.02 0 0 1 .541-1.308l-.536-.855a.75.75 0 0 1 .116-.97L4.88 4.566a.75.75 0 0 1 .97-.116l.855.536a6.02 6.02 0 0 1 1.308-.541l.244-.98ZM10 13.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z" fill="currentColor"/>
            </svg>
            Settings
          </button>

          {showSettings && (
            <div className="settings-panel" role="dialog" aria-label="Settings">
              <div className="theme-panel-header">
                <span className="theme-panel-title">Settings</span>
                <button className="theme-panel-close" onClick={() => { setShowSettings(false); setShowTheme(false); }} aria-label="Close settings">✕</button>
              </div>

              {/* Appearance */}
              <div className="settings-section" ref={themeRef}>
                <button
                  className={`settings-row-btn${showTheme ? " active" : ""}`}
                  onClick={() => setShowTheme((v) => !v)}
                >
                  <svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true" className="cog-icon">
                    <path d="M10 12.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Z" fill="currentColor"/>
                    <path fillRule="evenodd" clipRule="evenodd" d="M8.257 2.104a.75.75 0 0 1 1.486 0l.244.98a6.02 6.02 0 0 1 1.308.541l.855-.536a.75.75 0 0 1 .97.116l1.414 1.414a.75.75 0 0 1 .116.97l-.536.855c.214.41.394.845.541 1.308l.98.244a.75.75 0 0 1 0 1.486l-.98.244a6.02 6.02 0 0 1-.541 1.308l.536.855a.75.75 0 0 1-.116.97l-1.414 1.414a.75.75 0 0 1-.97.116l-.855-.536a6.02 6.02 0 0 1-1.308.541l-.244.98a.75.75 0 0 1-1.486 0l-.244-.98a6.02 6.02 0 0 1-1.308-.541l-.855.536a.75.75 0 0 1-.97-.116L3.466 14.22a.75.75 0 0 1-.116-.97l.536-.855a6.02 6.02 0 0 1-.541-1.308l-.98-.244a.75.75 0 0 1 0-1.486l.98-.244a6.02 6.02 0 0 1 .541-1.308l-.536-.855a.75.75 0 0 1 .116-.97L4.88 4.566a.75.75 0 0 1 .97-.116l.855.536a6.02 6.02 0 0 1 1.308-.541l.244-.98ZM10 13.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z" fill="currentColor"/>
                  </svg>
                  Appearance
                  <span className="settings-row-chevron">{showTheme ? "▴" : "▾"}</span>
                </button>
                {showTheme && <ThemePanel onClose={() => setShowTheme(false)} inline />}
              </div>

              <div className="settings-divider" />

              {/* Credentials */}
              {connection?.mode === "vcenter" && connection.can_disconnect && connection.env_ready && (
                <div className="settings-section">
                  <button
                    className="settings-row-btn danger-row"
                    onClick={() => { void onDisconnect(true); setShowSettings(false); }}
                  >
                    <svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true" className="cog-icon">
                      <path d="M9 2a1 1 0 0 0 0 2h2a1 1 0 1 0 0-2H9Z" fill="currentColor"/>
                      <path fillRule="evenodd" clipRule="evenodd" d="M4 5a2 2 0 0 1 2-2 3 3 0 0 0 3 3h2a3 3 0 0 0 3-3 2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5Zm3 4a1 1 0 0 0 0 2h.01a1 1 0 1 0 0-2H7Zm2 0a1 1 0 0 1 1-1h2a1 1 0 1 1 0 2h-2a1 1 0 0 1-1-1Zm-2 4a1 1 0 1 0 0 2h.01a1 1 0 1 0 0-2H7Zm2 0a1 1 0 0 1 1-1h2a1 1 0 1 1 0 2h-2a1 1 0 0 1-1-1Z" fill="currentColor"/>
                    </svg>
                    Forget saved credentials
                  </button>
                </div>
              )}

              {/* Passphrase */}
              <PassphraseField onSave={() => void load()} />
            </div>
          )}
        </div>

        <footer className="rail-credit">
          <a href="mailto:Michael@Zipkin.dev" className="rail-credit-email">Michael@Zipkin.dev</a>
          <span className="rail-credit-license">GNU AGPL v3 — source must be shared if deployed as a service.</span>
        </footer>
      </aside>

      <main>
        <header className="top">
          <div className="filters">
            <input
              value={qInput}
              onChange={(event) => setQInput(event.target.value)}
              placeholder="Search name or owner…"
            />
            <input
              value={ownerInput}
              onChange={(event) => setOwnerInput(event.target.value)}
              placeholder="Owner / prefix"
            />
            <select value={cluster} onChange={(event) => setCluster(event.target.value)}>
              <option value="">All clusters</option>
              {clusters.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
            <select value={power} onChange={(event) => setPower(event.target.value)}>
              <option value="">Any power</option>
              <option value="POWERED_ON">On</option>
              <option value="POWERED_OFF">Off</option>
              <option value="SUSPENDED">Suspended</option>
            </select>
          </div>
          {selectedIds.length > 0 ? <BatchCount count={selectedIds.length} /> : null}
          <button className="accent" onClick={() => setShowClone(true)}>
            New VM
          </button>
          {view === "machines" ? (
            <button className="ghost" disabled={selectedIds.length === 0} onClick={() => openMigrate()}>
              Host migrate
            </button>
          ) : null}
          <button className="ghost" onClick={() => void load()}>
            Refresh
          </button>
        </header>

        {connection?.stale ? (
          <div className="banner">
            vCenter is unreachable over the VPN. Showing the last local snapshot
            {connection.last_sync ? ` from ${relTime(connection.last_sync)}` : ""}. Clones, uploads, migrates, and power
            actions stay in the Jobs queue and resume automatically.
            <button onClick={() => setShowLogin(true)}>Reconnect</button>
          </div>
        ) : null}
        {error ? <div className="banner bad">{error}</div> : null}
        {notice ? (
          <div className="banner">
            {notice}
            <button onClick={() => setNotice("")}>Dismiss</button>
          </div>
        ) : null}

        {selectedIds.length > 0 ? (
          <div className="action-bar">
            <span className="action-bar-count">
              <BatchCount count={selectedIds.length} />
              <span>selected</span>
              <button className="text" onClick={() => setSelected({})}>
                clear
              </button>
            </span>
            <div className="action-buttons">
              <button onClick={() => openMigrate()}>Host migrate</button>
              {ACTIONS.map((action) => (
                <button
                  key={action.id}
                  className={action.danger ? "danger" : ""}
                  onClick={() => requestAction(action.id)}
                >
                  {action.label}
                </button>
              ))}
            </div>
          </div>
        ) : null}

        {view === "overview" && data ? (
          <Overview
            clusters={clusters}
            totals={totals}
            owners={owners}
            reclaim={reclaim}
            connection={connection}
            onOpen={setView}
            onConnect={() => setShowLogin(true)}
          />
        ) : null}
        {view === "monitoring" ? (
          <MonitoringView
            ownerFilter={monitorOwner}
            onOwnerFilter={setMonitorOwner}
            onOpenMachines={openMachinesForOwner}
            hosts={hosts}
            vms={vms}
            catalog={catalog}
          />
        ) : null}
        {view === "hosts" ? <HostTable hosts={hosts} /> : null}
        {view === "machines" ? (
          <VmTable
            vms={vms}
            selected={selected}
            onToggle={toggle}
            onSelectVisible={onVmSelectVisible}
            onMigrate={openMigrate}
          />
        ) : null}
        {view === "datastores" ? (
          <DatastoresView
            catalog={catalog}
            clusters={clusters}
            onQueued={onDatastoreQueued}
            onNotice={onDatastoreNotice}
          />
        ) : null}
        {view === "jobs" ? <JobsView data={jobs} onRefresh={onJobsRefresh} /> : null}
        {view === "owners" ? (
          <OwnerTable
            owners={owners}
            vms={vms}
            expanded={expandedOwner}
            onExpand={setExpandedOwner}
            onSelectGroup={onOwnerSelectGroup}
            onExport={onExportOwners}
          />
        ) : null}
        {view === "reclaim" ? (
          <ReclaimTable
            vms={reclaim}
            selected={selected}
            onToggle={toggle}
            onSelectVisible={onReclaimSelectVisible}
          />
        ) : null}
      </main>

      {showChangelog ? <ChangelogModal onClose={() => setShowChangelog(false)} /> : null}

      {showLogin ? (
        <LoginPanel
          connection={connection}
          onClose={() => setShowLogin(false)}
          onConnected={(info) => {
            setConnection(info);
            setShowLogin(false);
            setNotice(info.message);
            void load();
          }}
        />
      ) : null}

      {showClone ? (
        <CloneVmModal
          catalog={catalog}
          clusters={clusters}
          onClose={() => setShowClone(false)}
          onQueued={(job) => {
            setShowClone(false);
            setNotice(`${job.title} queued locally. Watch Jobs for progress; it will resume if the VPN drops.`);
            setView("jobs");
            void load();
          }}
        />
      ) : null}

      {showMigrate && (migrateTargets ?? selectedFromData()).length > 0 ? (
        <MigrateVmModal
          vms={migrateTargets ?? selectedFromData()}
          hosts={hosts}
          catalog={catalog}
          onClose={() => {
            setShowMigrate(false);
            setMigrateTargets(null);
          }}
          onQueued={(job) => {
            const done = (migrateTargets ?? selectedFromData()).map((vm) => vm.id);
            setShowMigrate(false);
            setMigrateTargets(null);
            dropCompleted(done);
            const leftover = selectedIds.length - done.length;
            setNotice(
              leftover > 0
                ? `${job.title} queued. ${leftover} VM(s) remain selected for a second batch.`
                : `${job.title} queued locally. Watch Jobs for progress; it will resume if the VPN drops.`,
            );
            setView("jobs");
            void load();
          }}
        />
      ) : null}

      {batchIntent ? (
        <BatchLimitDialog
          vms={selectedFromData()}
          actionLabel={batchIntent === "migrate" ? "Host migrate" : ACTIONS.find((item) => item.id === batchIntent)?.label || "This action"}
          onCancel={() => setBatchIntent(null)}
          onAccept={acceptBatch}
        />
      ) : null}

      {pending ? (
        <div className="modal-back" onClick={() => !busy && setPending(null)}>
          <div className="modal" onClick={(event) => event.stopPropagation()}>
            <h2>Confirm {ACTIONS.find((item) => item.id === pending)?.label}</h2>
            <p>
              {ACTIONS.find((item) => item.id === pending)?.hint}. Queued on this machine; the relay retries if vCenter
              drops.
            </p>
            {(() => {
              const targets = actionTargets ?? selectedFromData();
              return (
                <>
                  <p className="migrate-summary">
                    <BatchCount count={targets.length} /> in this job
                  </p>
                  <ul>
                    {targets.slice(0, 12).map((vm) => (
                      <li key={vm.id}>{vm.name}</li>
                    ))}
                    {targets.length > 12 ? <li>and {targets.length - 12} more</li> : null}
                  </ul>
                </>
              );
            })()}
            <div className="modal-actions">
              <button
                className="ghost"
                disabled={busy}
                onClick={() => {
                  setPending(null);
                  setActionTargets(null);
                }}
              >
                Cancel
              </button>
              <button className="danger" disabled={busy} onClick={() => void confirmAction()}>
                {busy ? "Working…" : pending === "destroy" ? "Delete permanently" : "Confirm"}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  </ThemeProvider>
  );
}

function summarize(vms: VirtualMachine[]) {
  return {
    count: vms.length,
    on: vms.filter((vm) => vm.power_state === "POWERED_ON").length,
    off: vms.filter((vm) => vm.power_state === "POWERED_OFF").length,
    suspended: vms.filter((vm) => vm.power_state === "SUSPENDED").length,
    cpu: vms.reduce((sum, vm) => sum + vm.cpu_count, 0),
    mem: vms.reduce((sum, vm) => sum + vm.memory_mib, 0),
    idle: vms.filter((vm) => vm.idle_score >= 40 && vm.power_state === "POWERED_ON").length,
  };
}

function StatusPill({ connection, loading }: { connection: ConnectionInfo | null; loading: boolean }) {
  if (!connection) return <div className="pill muted">{loading ? "Connecting…" : "Not signed in"}</div>;
  const tone = connection.stale ? "warm" : connection.connected ? "ok" : "bad";
  return (
    <div className={`pill ${tone}`}>
      <span>{connection.mode === "vcenter" ? (connection.stale ? "Relay · stale" : "vCenter") : "Demo"}</span>
      <small>{connection.host || "local demo"}</small>
      <small>
        {connection.stale
          ? connection.last_sync
            ? `last sync ${relTime(connection.last_sync)}`
            : "waiting for vCenter"
          : connection.user || connection.message}
      </small>
      {(connection.queued_jobs || 0) + (connection.active_jobs || 0) > 0 ? (
        <small>
          {connection.active_jobs} running · {connection.queued_jobs} queued
        </small>
      ) : null}
    </div>
  );
}

function Meter({ value, warn = 80 }: { value: number; warn?: number }) {
  const tone = value >= warn ? "hot" : value >= warn * 0.6 ? "warm" : "ok";
  return (
    <div className="meter">
      <div className={tone} style={{ width: `${Math.min(100, Math.max(0, value))}%` }} />
      <em>{value.toFixed(0)}%</em>
    </div>
  );
}

const Overview = React.memo(function Overview({
  clusters,
  totals,
  owners,
  reclaim,
  connection,
  onOpen,
  onConnect,
}: {
  clusters: InventorySnapshot["clusters"];
  totals: ReturnType<typeof summarize>;
  owners: OwnerReport[];
  reclaim: VirtualMachine[];
  connection: ConnectionInfo | null;
  onOpen: (view: View) => void;
  onConnect: () => void;
}) {
  return (
    <section className="stack">
      {connection?.mode !== "vcenter" ? (
        <div className="panel connect-card">
          <header>
            <div>
              <h2>Sign in to vCenter</h2>
              <p>
                Add the server hostname and account here. You can keep using demo data until you connect. Saving credentials
                writes them into local <code>.env</code> so the next launch can reconnect.
              </p>
            </div>
            <button className="accent" onClick={onConnect}>
              Connect
            </button>
          </header>
        </div>
      ) : null}
      <div className="tiles">
        <article>
          <span>Virtual machines</span>
          <strong>{totals.count}</strong>
          <small>
            {totals.on} on · {totals.off} off · {totals.suspended} suspended
          </small>
        </article>
        <article>
          <span>Allocated vCPU</span>
          <strong>{totals.cpu}</strong>
          <small>across filtered inventory</small>
        </article>
        <article>
          <span>Allocated memory</span>
          <strong>{gib(totals.mem)}</strong>
          <small>guest configured size</small>
        </article>
        <article className={totals.idle ? "alert" : ""}>
          <span>Reclaim candidates</span>
          <strong>{totals.idle}</strong>
          <small>powered on, idle or untouched</small>
        </article>
      </div>

      <div className="split">
        <div className="panel">
          <header>
            <h2>Clusters</h2>
          </header>
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Hosts</th>
                <th>VMs</th>
                <th>CPU</th>
                <th>Memory</th>
              </tr>
            </thead>
            <tbody>
              {clusters.map((cluster) => (
                <tr key={cluster.id}>
                  <td>{cluster.name}</td>
                  <td>{cluster.host_count}</td>
                  <td>{cluster.vm_count}</td>
                  <td>
                    <Meter value={cluster.cpu_usage_pct} />
                  </td>
                  <td>
                    <Meter value={cluster.memory_usage_pct} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="panel">
          <header>
            <h2>Heaviest owners</h2>
            <button className="text" onClick={() => onOpen("owners")}>
              View all
            </button>
          </header>
          <ul className="owner-list">
            {owners.slice(0, 6).map((row) => (
              <li key={row.owner_key}>
                <div>
                  <strong>{row.owner_key}</strong>
                  <small>
                    {row.vm_count} VMs · {row.powered_on} on · {row.cpu_count} vCPU
                  </small>
                </div>
                <b>{gib(row.memory_mib)}</b>
              </li>
            ))}
          </ul>
        </div>
      </div>

      <div className="panel">
        <header>
          <h2>Idle but reserved</h2>
          <button className="text" onClick={() => onOpen("reclaim")}>
            Open reclaim
          </button>
        </header>
        {reclaim.length === 0 ? (
          <p className="empty">No powered-on idle candidates in this filter.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>VM</th>
                <th>Owner</th>
                <th>CPU</th>
                <th>Memory</th>
                <th>Last activity</th>
              </tr>
            </thead>
            <tbody>
              {reclaim.slice(0, 6).map((vm) => (
                <tr key={vm.id}>
                  <td>{vm.name}</td>
                  <td>{vm.owner_key}</td>
                  <td>
                    {vm.cpu_count} · {vm.cpu_usage_pct.toFixed(0)}%
                  </td>
                  <td>
                    {gib(vm.memory_mib)} · {vm.memory_usage_pct.toFixed(0)}%
                  </td>
                  <td>{relTime(vm.last_activity)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
});

const HostTable = React.memo(function HostTable({ hosts }: { hosts: InventorySnapshot["hosts"] }) {
  return (
    <div className="panel">
      <header>
        <h2>ESXi hosts</h2>
      </header>
      <table>
        <thead>
          <tr>
            <th>Host</th>
            <th>Cluster</th>
            <th>State</th>
            <th>VMs</th>
            <th>CPU</th>
            <th>Memory</th>
          </tr>
        </thead>
        <tbody>
          {hosts.map((host) => (
            <tr key={host.id}>
              <td>
                {host.name}
                <small className="sub">{host.cpu_cores} cores</small>
              </td>
              <td>{host.cluster_name}</td>
              <td>
                <span className="chip">{host.connection_state}</span>
              </td>
              <td>{host.vm_count}</td>
              <td>
                <Meter value={host.cpu_usage_pct} />
              </td>
              <td>
                <Meter value={host.memory_usage_pct} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
});

const VmTable = React.memo(function VmTable({
  vms,
  selected,
  onToggle,
  onSelectVisible,
  onMigrate,
}: {
  vms: VirtualMachine[];
  selected: Record<string, boolean>;
  onToggle: (id: string) => void;
  onSelectVisible: (vms: VirtualMachine[]) => void;
  onMigrate: () => void;
}) {
  const [sortKey, setSortKey] = useState<MachineSortKey>("name");
  const [sortDir, setSortDir] = useState<SortDir>("asc");
  const [filters, setFilters] = useState<MachineFilters>({});

  function pickSort(key: MachineSortKey) {
    setSortDir((dir) => toggleSortDir(sortKey, key, dir));
    setSortKey(key);
  }

  function setFilter(key: MachineSortKey, value: string) {
    setFilters((current) => ({ ...current, [key]: value }));
  }

  const filtered = useMemo(() => filterMachines(vms, filters), [vms, filters]);
  const sorted = useMemo(() => sortMachines(filtered, sortKey, sortDir), [filtered, sortKey, sortDir]);
  const filterCount = Object.values(filters).filter((value) => value.trim()).length;
  const storage = sorted.reduce((sum, vm) => sum + vm.storage_provisioned_bytes, 0);
  const selectedCount = useMemo(() => Object.values(selected).filter(Boolean).length, [selected]);

  return (
    <div className="panel">
      <header>
        <div>
          <h2>Virtual machines</h2>
          <p>
            Select one or more VMs, then use <strong>Host migrate</strong> to move hosts, convert Thick to Thin, or change
            datastore/network. Thick→Thin uses Storage vMotion and can stay on the current datastore.
          </p>
        </div>
        <div className="header-meta">
          <span className="header-meta-line">
            {sorted.length}
            {filterCount ? ` of ${vms.length}` : ""} VMs · {bytes(storage)}
            {selectedCount ? (
              <>
                {" · "}
                <BatchCount count={selectedCount} />
              </>
            ) : null}
          </span>
          <div className="header-meta-actions">
            {filterCount ? (
              <button className="text" onClick={() => setFilters({})}>
                Clear filters
              </button>
            ) : null}
            <button className="text" onClick={() => onSelectVisible(sorted)} disabled={sorted.length === 0}>
              Select page
            </button>
            <button className="accent compact" disabled={selectedCount === 0} onClick={onMigrate}>
              Host migrate
            </button>
          </div>
        </div>
      </header>
      <table>
        <thead>
          <tr>
            <th />
            <SortHeader
              label="Name"
              active={sortKey === "name"}
              dir={sortDir}
              onClick={() => pickSort("name")}
              filter={filters.name}
              onFilter={(value) => setFilter("name", value)}
              filterPlaceholder="Name…"
            />
            <SortHeader
              label="Owner"
              active={sortKey === "owner_key"}
              dir={sortDir}
              onClick={() => pickSort("owner_key")}
              filter={filters.owner_key}
              onFilter={(value) => setFilter("owner_key", value)}
              filterPlaceholder="Owner…"
            />
            <SortHeader
              label="Power"
              active={sortKey === "power_state"}
              dir={sortDir}
              onClick={() => pickSort("power_state")}
              filter={filters.power_state}
              onFilter={(value) => setFilter("power_state", value)}
              filterPlaceholder="On / Off"
            />
            <SortHeader
              label="Cluster / host"
              active={sortKey === "cluster_name"}
              dir={sortDir}
              onClick={() => pickSort("cluster_name")}
              filter={filters.cluster_name}
              onFilter={(value) => setFilter("cluster_name", value)}
              filterPlaceholder="Cluster…"
            />
            <SortHeader
              label="vCPU"
              active={sortKey === "cpu_count"}
              dir={sortDir}
              onClick={() => pickSort("cpu_count")}
              filter={filters.cpu_count}
              onFilter={(value) => setFilter("cpu_count", value)}
              filterPlaceholder="e.g. >=4"
            />
            <SortHeader
              label="Memory"
              active={sortKey === "memory_mib"}
              dir={sortDir}
              onClick={() => pickSort("memory_mib")}
              filter={filters.memory_mib}
              onFilter={(value) => setFilter("memory_mib", value)}
              filterPlaceholder="GiB"
            />
            <SortHeader
              label="Storage"
              active={sortKey === "storage_provisioned_bytes"}
              dir={sortDir}
              onClick={() => pickSort("storage_provisioned_bytes")}
              filter={filters.storage_provisioned_bytes}
              onFilter={(value) => setFilter("storage_provisioned_bytes", value)}
              filterPlaceholder="e.g. >80"
            />
            <SortHeader
              label="Disk"
              active={sortKey === "disk_provisioning"}
              dir={sortDir}
              onClick={() => pickSort("disk_provisioning")}
              filter={filters.disk_provisioning}
              onFilter={(value) => setFilter("disk_provisioning", value)}
              filterPlaceholder="Thin / Thick"
            />
            <SortHeader
              label="Activity"
              active={sortKey === "last_activity"}
              dir={sortDir}
              onClick={() => pickSort("last_activity")}
              filter={filters.last_activity}
              onFilter={(value) => setFilter("last_activity", value)}
              filterPlaceholder="Activity…"
            />
          </tr>
        </thead>
        <tbody>
          {sorted.length === 0 ? (
            <tr>
              <td colSpan={10} className="empty">
                {vms.length === 0 ? "No virtual machines in this view." : "No rows match these column filters."}
              </td>
            </tr>
          ) : (
            sorted.map((vm) => (
              <tr key={vm.id} className={vm.idle_score >= 40 ? "idle" : ""}>
                <td>
                  <input type="checkbox" checked={Boolean(selected[vm.id])} onChange={() => onToggle(vm.id)} />
                </td>
                <td>
                  {vm.name}
                  <small className="sub">{vm.ip_address || vm.guest_os || "—"}</small>
                </td>
                <td>
                  {vm.owner_key}
                  <small className="sub">{vm.owner_source}</small>
                </td>
                <td>
                  <span className={`chip power ${powerClass(vm.power_state)}`}>{powerLabel(vm.power_state)}</span>
                </td>
                <td>
                  {vm.cluster_name}
                  <small className="sub">{vm.host_name}</small>
                </td>
                <td>
                  {vm.cpu_count}
                  <small className="sub">
                    {vm.cpu_usage_pct.toFixed(0)}% · {vm.cpu_usage_mhz} MHz
                  </small>
                </td>
                <td>
                  {gib(vm.memory_mib)}
                  <small className="sub">{vm.memory_usage_pct.toFixed(0)}% used</small>
                </td>
                <td>
                  {bytes(vm.storage_provisioned_bytes)}
                  <small className="sub">{bytes(vm.storage_used_bytes)} used</small>
                </td>
                <td>
                  <DiskChip kind={vm.disk_provisioning} />
                </td>
                <td>
                  {relTime(vm.last_activity)}
                  <small className="sub">{vm.last_activity_source.replace("_", " ")}</small>
                </td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
});

function DiskChip({ kind }: { kind: string }) {
  return <span className={`chip disk ${kind}`}>{diskLabel(kind)}</span>;
}

function SortHeader({
  label,
  active,
  dir,
  onClick,
  filter,
  onFilter,
  filterPlaceholder = "Filter",
}: {
  label: string;
  active: boolean;
  dir: SortDir;
  onClick: () => void;
  filter?: string;
  onFilter?: (value: string) => void;
  filterPlaceholder?: string;
}) {
  return (
    <th>
      <div className="col-head">
        <button type="button" className={`sort-th${active ? " active" : ""}`} onClick={onClick}>
          {label}
          <span className="sort-mark" aria-hidden="true">
            {active ? (dir === "asc" ? "▲" : "▼") : "↕"}
          </span>
        </button>
        {onFilter ? (
          <input
            className={`col-filter${filter?.trim() ? " has-value" : ""}`}
            value={filter ?? ""}
            placeholder={filterPlaceholder}
            aria-label={`Filter ${label}`}
            onChange={(event) => onFilter(event.target.value)}
            onClick={(event) => event.stopPropagation()}
            onKeyDown={(event) => event.stopPropagation()}
          />
        ) : null}
      </div>
    </th>
  );
}

const OwnerTable = React.memo(function OwnerTable({
  owners,
  vms,
  expanded,
  onExpand,
  onSelectGroup,
  onExport,
}: {
  owners: OwnerReport[];
  vms: VirtualMachine[];
  expanded: string;
  onExpand: (owner: string) => void;
  onSelectGroup: (group: VirtualMachine[]) => void;
  onExport: () => void;
}) {
  const [sortKey, setSortKey] = useState<OwnerSortKey>("memory_mib");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  function pickSort(key: OwnerSortKey) {
    setSortDir((dir) => toggleSortDir(sortKey, key, dir));
    setSortKey(key);
  }

  const sorted = useMemo(() => sortOwners(owners, sortKey, sortDir), [owners, sortKey, sortDir]);

  return (
    <div className="panel">
      <header>
        <h2>Owner / naming groups</h2>
        <button className="text" onClick={onExport}>
          Export CSV
        </button>
      </header>
      <table>
        <thead>
          <tr>
            <SortHeader label="Owner" active={sortKey === "owner_key"} dir={sortDir} onClick={() => pickSort("owner_key")} />
            <SortHeader label="VMs" active={sortKey === "vm_count"} dir={sortDir} onClick={() => pickSort("vm_count")} />
            <SortHeader label="On / Off / Susp" active={sortKey === "powered_on"} dir={sortDir} onClick={() => pickSort("powered_on")} />
            <SortHeader label="vCPU" active={sortKey === "cpu_count"} dir={sortDir} onClick={() => pickSort("cpu_count")} />
            <SortHeader label="Memory" active={sortKey === "memory_mib"} dir={sortDir} onClick={() => pickSort("memory_mib")} />
            <SortHeader label="Idle" active={sortKey === "idle_candidates"} dir={sortDir} onClick={() => pickSort("idle_candidates")} />
            <th />
          </tr>
        </thead>
        <tbody>
          {sorted.map((row) => {
            const members = vms.filter((vm) => vm.owner_key === row.owner_key);
            const open = expanded === row.owner_key;
            return (
              <React.Fragment key={row.owner_key}>
                <tr>
                  <td>
                    <button className="text" onClick={() => onExpand(open ? "" : row.owner_key)}>
                      {row.owner_key}
                    </button>
                    <small className="sub">{row.owner_source}</small>
                  </td>
                  <td>{row.vm_count}</td>
                  <td>
                    {row.powered_on} / {row.powered_off} / {row.suspended}
                  </td>
                  <td>{row.cpu_count}</td>
                  <td>{gib(row.memory_mib)}</td>
                  <td>{row.idle_candidates}</td>
                  <td>
                    <button className="text" onClick={() => onSelectGroup(members)}>
                      Select
                    </button>
                  </td>
                </tr>
                {open
                  ? members.map((vm) => (
                      <tr key={vm.id} className="nested">
                        <td colSpan={2}>{vm.name}</td>
                        <td>
                          <span className={`chip power ${powerClass(vm.power_state)}`}>{powerLabel(vm.power_state)}</span>
                        </td>
                        <td>{vm.cpu_count}</td>
                        <td>{gib(vm.memory_mib)}</td>
                        <td colSpan={2}>{relTime(vm.last_activity)}</td>
                      </tr>
                    ))
                  : null}
              </React.Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
});

const ReclaimTable = React.memo(function ReclaimTable({
  vms,
  selected,
  onToggle,
  onSelectVisible,
}: {
  vms: VirtualMachine[];
  selected: Record<string, boolean>;
  onToggle: (id: string) => void;
  onSelectVisible: (vms: VirtualMachine[]) => void;
}) {
  const [sortKey, setSortKey] = useState<ReclaimSortKey>("idle_score");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [filters, setFilters] = useState<ReclaimFilters>({});

  function pickSort(key: ReclaimSortKey) {
    setSortDir((dir) => toggleSortDir(sortKey, key, dir));
    setSortKey(key);
  }

  function setFilter(key: ReclaimSortKey, value: string) {
    setFilters((current) => ({ ...current, [key]: value }));
  }

  const filtered = useMemo(() => filterReclaimVms(vms, filters), [vms, filters]);
  const sorted = useMemo(() => sortReclaimVms(filtered, sortKey, sortDir), [filtered, sortKey, sortDir]);
  const filterCount = Object.values(filters).filter((value) => value.trim()).length;

  function exportCsv() {
    const header = ["Name", "Cluster", "Owner", "Score", "vCPU", "CPU %", "Memory (GiB)", "Mem %", "Storage Provisioned (GiB)", "Storage Used (GiB)", "Disk", "Idle (days)", "Why"];
    const rows = sorted.map((vm) => [
      vm.name,
      vm.cluster_name ?? "",
      vm.owner_key ?? "",
      String(vm.idle_score),
      String(vm.cpu_count),
      vm.cpu_usage_pct.toFixed(1),
      (vm.memory_mib / 1024).toFixed(2),
      vm.memory_usage_pct.toFixed(1),
      (vm.storage_provisioned_bytes / 1073741824).toFixed(2),
      (vm.storage_used_bytes / 1073741824).toFixed(2),
      vm.disk_provisioning ?? "",
      vm.days_idle != null ? vm.days_idle.toFixed(0) : "",
      vm.reclaim_reason ?? "",
    ]);
    const csv = [header, ...rows].map((row) => row.map(csvEscape).join(",")).join("\n");
    const label = filterCount ? `reclaim-filtered-${sorted.length}` : "reclaim-all";
    downloadText(`${label}.csv`, csv);
  }
  const memory = sorted.reduce((sum, vm) => sum + vm.memory_mib, 0);
  const cpu = sorted.reduce((sum, vm) => sum + vm.cpu_count, 0);
  const storage = sorted.reduce((sum, vm) => sum + vm.storage_provisioned_bytes, 0);
  return (
    <div className="panel">
      <header>
        <div>
          <h2>Reclaim queue</h2>
          <p>
            Powered-on VMs with low utilization and little recent vCenter activity. Guest last-login is not available
            from vCenter itself — this uses console/power events, boot time, and live CPU/memory. Filter any column like
            a spreadsheet; use {">"}40 on Score or Idle if you want a numeric cutoff. Disk is Thick, Thin, or Mixed.
          </p>
        </div>
        <div className="header-meta">
          <span>
            {sorted.length}
            {filterCount ? ` of ${vms.length}` : ""} VMs · {cpu} vCPU · {gib(memory)} · {bytes(storage)}
          </span>
          <div className="header-meta-actions">
            {filterCount ? (
              <button className="text" onClick={() => setFilters({})}>
                Clear filters
              </button>
            ) : null}
            <button className="text" onClick={() => onSelectVisible(sorted)} disabled={sorted.length === 0}>
              Select all
            </button>
            <button className="text" onClick={exportCsv} disabled={sorted.length === 0}>
              Export CSV
            </button>
          </div>
        </div>
      </header>
      <table>
        <thead>
          <tr>
            <th />
            <SortHeader
              label="VM"
              active={sortKey === "name"}
              dir={sortDir}
              onClick={() => pickSort("name")}
              filter={filters.name}
              onFilter={(value) => setFilter("name", value)}
              filterPlaceholder="Name…"
            />
            <SortHeader
              label="Owner"
              active={sortKey === "owner_key"}
              dir={sortDir}
              onClick={() => pickSort("owner_key")}
              filter={filters.owner_key}
              onFilter={(value) => setFilter("owner_key", value)}
              filterPlaceholder="Owner…"
            />
            <SortHeader
              label="Score"
              active={sortKey === "idle_score"}
              dir={sortDir}
              onClick={() => pickSort("idle_score")}
              filter={filters.idle_score}
              onFilter={(value) => setFilter("idle_score", value)}
              filterPlaceholder="e.g. >40"
            />
            <SortHeader
              label="CPU / Mem"
              active={sortKey === "memory_mib"}
              dir={sortDir}
              onClick={() => pickSort("memory_mib")}
              filter={filters.memory_mib}
              onFilter={(value) => setFilter("memory_mib", value)}
              filterPlaceholder="CPU or RAM"
            />
            <SortHeader
              label="Storage"
              active={sortKey === "storage_provisioned_bytes"}
              dir={sortDir}
              onClick={() => pickSort("storage_provisioned_bytes")}
              filter={filters.storage_provisioned_bytes}
              onFilter={(value) => setFilter("storage_provisioned_bytes", value)}
              filterPlaceholder="e.g. >80"
            />
            <SortHeader
              label="Disk"
              active={sortKey === "disk_provisioning"}
              dir={sortDir}
              onClick={() => pickSort("disk_provisioning")}
              filter={filters.disk_provisioning}
              onFilter={(value) => setFilter("disk_provisioning", value)}
              filterPlaceholder="Thin / Thick"
            />
            <SortHeader
              label="Idle"
              active={sortKey === "days_idle"}
              dir={sortDir}
              onClick={() => pickSort("days_idle")}
              filter={filters.days_idle}
              onFilter={(value) => setFilter("days_idle", value)}
              filterPlaceholder="e.g. >14"
            />
            <SortHeader
              label="Why"
              active={sortKey === "reclaim_reason"}
              dir={sortDir}
              onClick={() => pickSort("reclaim_reason")}
              filter={filters.reclaim_reason}
              onFilter={(value) => setFilter("reclaim_reason", value)}
              filterPlaceholder="Reason…"
            />
          </tr>
        </thead>
        <tbody>
          {sorted.length === 0 ? (
            <tr>
              <td colSpan={9} className="empty">
                {vms.length === 0 ? "No reclaim candidates right now." : "No rows match these column filters."}
              </td>
            </tr>
          ) : (
            sorted.map((vm) => (
              <tr key={vm.id}>
                <td>
                  <input type="checkbox" checked={Boolean(selected[vm.id])} onChange={() => onToggle(vm.id)} />
                </td>
                <td>
                  {vm.name}
                  <small className="sub">{vm.cluster_name}</small>
                </td>
                <td>{vm.owner_key}</td>
                <td>
                  <b className="score">{vm.idle_score}</b>
                </td>
                <td>
                  {vm.cpu_count} vCPU {vm.cpu_usage_pct.toFixed(0)}%
                  <small className="sub">
                    {gib(vm.memory_mib)} · {vm.memory_usage_pct.toFixed(0)}%
                  </small>
                </td>
                <td>
                  {bytes(vm.storage_provisioned_bytes)}
                  <small className="sub">{bytes(vm.storage_used_bytes)} used</small>
                </td>
                <td>
                  <DiskChip kind={vm.disk_provisioning} />
                </td>
                <td>{vm.days_idle != null ? `${vm.days_idle.toFixed(0)}d` : "unknown"}</td>
                <td className="reason">{vm.reclaim_reason}</td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
});
