import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Pie,
  PieChart,
  ReferenceArea,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { fetchMetrics } from "./api";
import { bytes, gib } from "./format";
import { sortMonitorOwners, toggleSortDir, type MonitorOwnerSortKey, type SortDir } from "./sort";
import type { Catalog, HostSummary, MetricsResponse, OwnerUtilization, VirtualMachine } from "./types";

const HOUR_OPTIONS = [
  { label: "1 hour", value: 1 },
  { label: "6 hours", value: 6 },
  { label: "24 hours", value: 24 },
  { label: "7 days", value: 168 },
  { label: "14 days", value: 336 },
];

const IDLE_THRESHOLD_OPTIONS = [
  { label: "30 days", value: 30 },
  { label: "60 days", value: 60 },
  { label: "90 days", value: 90 },
  { label: "120 days", value: 120 },
  { label: "180 days", value: 180 },
  { label: "365 days", value: 365 },
];

const PIE_COLORS = [
  "#d6f261",
  "#86c9a3",
  "#8fd18a",
  "#e3b15a",
  "#7eb8da",
  "#c792ea",
  "#f07178",
  "#89ddff",
  "#ffcb6b",
  "#82aaff",
  "#c3e88d",
  "#ff5370",
  "#b2ccd6",
  "#f78c6c",
  "#bb80b3",
  "#a6e22e",
  "#66d9ef",
  "#ae81ff",
  "#e6db74",
  "#fd971f",
];

type PieRow = { name: string; value: number; owner_key: string };

type ZoomRange = { from: string; to: string };

type Props = {
  ownerFilter: string;
  onOwnerFilter: (owner: string) => void;
  onOpenMachines: (owner: string) => void;
  hosts?: HostSummary[];
  vms?: VirtualMachine[];
  catalog?: Catalog | null;
};

function formatAxisTime(ts: string, spanMs: number) {
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return ts;
  if (spanMs >= 48 * 3600 * 1000) {
    return date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit" });
  }
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function formatTooltipTime(ts: string) {
  const date = new Date(ts);
  return date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function latestPct(series: MetricsResponse["series"], key: "cpu_pct" | "memory_pct" | "disk_pct") {
  if (!series.length) return 0;
  return series[series.length - 1][key];
}

// Groups owners below the threshold percentage into "Other (N)" so the legend stays readable.
const PIE_MIN_PCT = 1.5;

function topOwnersForPie(
  owners: OwnerUtilization[],
  metric: "cpu_share_pct" | "memory_share_pct" | "storage_share_pct",
  limit = 10,
): PieRow[] {
  const sorted = [...owners].sort((a, b) => b[metric] - a[metric]);
  // Hard limit first, then drop anything below the visibility threshold
  const hardCapped = sorted.slice(0, limit);
  const top = hardCapped.filter((r) => r[metric] >= PIE_MIN_PCT);
  const rest = [
    ...hardCapped.filter((r) => r[metric] < PIE_MIN_PCT),
    ...sorted.slice(limit),
  ];
  const rows: PieRow[] = top.map((row) => ({
    name: row.owner_key,
    owner_key: row.owner_key,
    value: Math.round(row[metric] * 100) / 100,
  }));
  if (rest.length) {
    rows.push({
      name: `Other (${rest.length})`,
      owner_key: "",
      value: Math.round(rest.reduce((sum, row) => sum + row[metric], 0) * 100) / 100,
    });
  }
  return rows.filter((row) => row.value > 0);
}

function ChartTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean;
  payload?: Array<{ name: string; value: number; color: string }>;
  label?: string;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="chart-tooltip">
      <strong>{label ? formatTooltipTime(String(label)) : ""}</strong>
      {payload.map((row) => (
        <div key={row.name}>
          <span style={{ color: row.color }}>{row.name}</span> {row.value.toFixed(1)}%
        </div>
      ))}
    </div>
  );
}

function SortHeader({
  label,
  active,
  dir,
  onClick,
}: {
  label: string;
  active: boolean;
  dir: SortDir;
  onClick: () => void;
}) {
  return (
    <th>
      <button type="button" className={`sort-th${active ? " active" : ""}`} onClick={onClick}>
        {label}
        <span className="sort-mark" aria-hidden="true">
          {active ? (dir === "asc" ? "▲" : "▼") : "↕"}
        </span>
      </button>
    </th>
  );
}

function PieTooltip({
  active,
  payload,
}: {
  active?: boolean;
  payload?: Array<{ name: string; payload: PieRow; value: number }>;
}) {
  if (!active || !payload?.length) return null;
  const row = payload[0];
  return (
    <div className="chart-tooltip">
      <strong>{row.name}</strong>
      <div>{row.value.toFixed(1)}% of total</div>
    </div>
  );
}

function UtilLineChart({
  title,
  dataKey,
  color,
  data,
  selectFrom,
  selectTo,
  onSelectStart,
  onSelectMove,
  extra,
}: {
  title: string;
  dataKey: "cpu_pct" | "memory_pct" | "disk_pct";
  color: string;
  data: MetricsResponse["series"];
  selectFrom: string;
  selectTo: string;
  onSelectStart: (ts: string) => void;
  onSelectMove: (ts: string) => void;
  extra?: React.ReactNode;
}) {
  const chartData = data.map((row) => ({
    ts: row.ts,
    value: row[dataKey],
  }));
  const spanMs =
    chartData.length >= 2
      ? Math.max(1, new Date(chartData[chartData.length - 1].ts).getTime() - new Date(chartData[0].ts).getTime())
      : 0;
  const left = selectFrom && selectTo ? (selectFrom < selectTo ? selectFrom : selectTo) : "";
  const right = selectFrom && selectTo ? (selectFrom < selectTo ? selectTo : selectFrom) : "";

  return (
    <section className="panel monitor-chart">
      <header>
        <div>
          <h2>{title}</h2>
          <p>Drag a range to zoom it to the full chart width</p>
        </div>
        {extra}
      </header>
      <div className="chart-wrap zoomable">
        {chartData.length ? (
          <ResponsiveContainer width="100%" height={220}>
            <AreaChart
              data={chartData}
              margin={{ top: 8, right: 12, left: -8, bottom: 0 }}
              onMouseDown={(state) => {
                if (state?.activeLabel) onSelectStart(String(state.activeLabel));
              }}
              onMouseMove={(state) => {
                if (selectFrom && state?.activeLabel) onSelectMove(String(state.activeLabel));
              }}
            >
              <defs>
                <linearGradient id={`grad-${dataKey}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor={color} stopOpacity={0.45} />
                  <stop offset="95%" stopColor={color} stopOpacity={0.02} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="rgba(154, 165, 140, 0.15)" vertical={false} />
              <XAxis
                dataKey="ts"
                tickFormatter={(value) => formatAxisTime(String(value), spanMs)}
                stroke="var(--muted)"
                fontSize={11}
                minTickGap={28}
              />
              <YAxis domain={[0, 100]} stroke="var(--muted)" fontSize={11} tickFormatter={(v) => `${v}%`} />
              <Tooltip content={<ChartTooltip />} labelFormatter={(value) => String(value)} />
              <Area
                type="monotone"
                dataKey="value"
                name={title}
                stroke={color}
                fill={`url(#grad-${dataKey})`}
                strokeWidth={2}
                dot={false}
                activeDot={{ r: 4 }}
              />
              {left && right && left !== right ? (
                <ReferenceArea x1={left} x2={right} strokeOpacity={0.25} fill={color} fillOpacity={0.18} />
              ) : null}
            </AreaChart>
          </ResponsiveContainer>
        ) : (
          <div className="chart-empty">Collecting samples… history fills in as the relay syncs.</div>
        )}
      </div>
    </section>
  );
}

function OwnerPieChart({
  title,
  data,
  onSelect,
}: {
  title: string;
  data: PieRow[];
  onSelect: (owner: string) => void;
}) {
  // Legend rows × ~18px per row + pie height. Cap legend at bottom so pie gets full width.
  const legendRows = Math.ceil(data.length / 2);
  const chartHeight = 200 + legendRows * 18;
  return (
    <section className="panel monitor-chart compact">
      <header>
        <h2>{title}</h2>
      </header>
      <div className="chart-wrap pie">
        {data.length ? (
          <ResponsiveContainer width="100%" height={chartHeight}>
            <PieChart>
              <Pie
                data={data}
                dataKey="value"
                nameKey="name"
                cx="50%"
                cy={100}
                innerRadius={52}
                outerRadius={88}
                paddingAngle={1}
                onClick={(entry) => {
                  const row = entry as PieRow;
                  if (row.owner_key) onSelect(row.owner_key);
                }}
              >
                {data.map((row, index) => (
                  <Cell key={row.name} fill={PIE_COLORS[index % PIE_COLORS.length]} cursor={row.owner_key ? "pointer" : "default"} />
                ))}
              </Pie>
              <Tooltip content={<PieTooltip />} />
              <Legend
                layout="horizontal"
                align="center"
                verticalAlign="bottom"
                wrapperStyle={{ fontSize: 10, paddingTop: 8 }}
                formatter={(value: string) =>
                  value.length > 16 ? value.slice(0, 15) + "…" : value
                }
              />
            </PieChart>
          </ResponsiveContainer>
        ) : (
          <div className="chart-empty">No owner data yet.</div>
        )}
      </div>
    </section>
  );
}

export const MonitoringView = React.memo(function MonitoringView({
  ownerFilter,
  onOwnerFilter,
  onOpenMachines,
  hosts = [],
  vms = [],
  catalog = null,
}: Props) {
  const [hours, setHours] = useState(336);
  const [hostFilter, setHostFilter] = useState("");
  const [datastoreFilter, setDatastoreFilter] = useState("");
  const [metrics, setMetrics] = useState<MetricsResponse | null>(null);
  const [error, setError] = useState("");
  const [topLimit, setTopLimit] = useState(10);
  const [idleThreshold, setIdleThreshold] = useState(120);
  const [sortKey, setSortKey] = useState<MonitorOwnerSortKey>("cpu_share_pct");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [zoom, setZoom] = useState<ZoomRange | null>(null);
  const [zoomStack, setZoomStack] = useState<ZoomRange[]>([]);
  const [selectFrom, setSelectFrom] = useState("");
  const [selectTo, setSelectTo] = useState("");
  const dragRef = useRef({
    from: "",
    to: "",
    series: [] as MetricsResponse["series"],
    zoom: null as ZoomRange | null,
    visible: [] as MetricsResponse["series"],
  });

  function pickSort(key: MonitorOwnerSortKey) {
    setSortDir((dir) => toggleSortDir(sortKey, key, dir));
    setSortKey(key);
  }

  async function load() {
    try {
      const payload = await fetchMetrics({
        hours,
        owner: ownerFilter || undefined,
        host: hostFilter || undefined,
        datastore: datastoreFilter || undefined,
        since: zoom?.from,
        until: zoom?.to,
      });
      setMetrics((prev) => (JSON.stringify(prev) === JSON.stringify(payload) ? prev : payload));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load metrics");
    }
  }

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 8000);
    return () => window.clearInterval(timer);
  }, [hours, ownerFilter, hostFilter, datastoreFilter, zoom?.from, zoom?.to]);

  const series = metrics?.series ?? [];
  const visibleSeries = useMemo(() => {
    if (!zoom) return series;
    const sliced = series.filter((point) => point.ts >= zoom.from && point.ts <= zoom.to);
    return sliced.length >= 2 ? sliced : series;
  }, [series, zoom]);

  dragRef.current = { from: selectFrom, to: selectTo, series, zoom, visible: visibleSeries };

  useEffect(() => {
    function onUp() {
      const drag = dragRef.current;
      if (!drag.from || !drag.to || drag.from === drag.to) {
        if (drag.from) {
          setSelectFrom("");
          setSelectTo("");
        }
        return;
      }
      const from = drag.from < drag.to ? drag.from : drag.to;
      const to = drag.from < drag.to ? drag.to : drag.from;
      const source = drag.zoom ? drag.visible : drag.series;
      const inRange = source.filter((point) => point.ts >= from && point.ts <= to);
      setSelectFrom("");
      setSelectTo("");
      if (inRange.length < 2) return;
      setZoomStack((stack) => (drag.zoom ? [...stack, drag.zoom] : stack));
      setZoom({ from, to });
    }
    window.addEventListener("mouseup", onUp);
    return () => window.removeEventListener("mouseup", onUp);
  }, []);

  function startSelect(ts: string) {
    setSelectFrom(ts);
    setSelectTo(ts);
  }

  function moveSelect(ts: string) {
    setSelectTo(ts);
  }

  function zoomOut() {
    const previous = zoomStack[zoomStack.length - 1] ?? null;
    setZoomStack((stack) => stack.slice(0, -1));
    setZoom(previous);
  }

  function resetZoom() {
    setZoom(null);
    setZoomStack([]);
    setSelectFrom("");
    setSelectTo("");
  }

  function changeHours(value: number) {
    setHours(value);
    resetZoom();
  }

  const storageSources = useMemo(() => {
    const rows = catalog?.datastores ?? [];
    if (!hostFilter) return rows;
    return rows.filter((item) => !item.host_ids.length || item.host_ids.includes(hostFilter));
  }, [catalog, hostFilter]);

  function changeHost(value: string) {
    setHostFilter(value);
    if (value && datastoreFilter) {
      const stillVisible = (catalog?.datastores ?? []).some(
        (item) => item.id === datastoreFilter && (!item.host_ids.length || item.host_ids.includes(value)),
      );
      if (!stillVisible) setDatastoreFilter("");
    }
  }

  const scopedVms = useMemo(
    () => (hostFilter ? vms.filter((vm) => vm.host_id === hostFilter) : vms),
    [vms, hostFilter],
  );
  const owners = metrics?.owners ?? [];
  const sortedOwners = useMemo(() => sortMonitorOwners(owners, sortKey, sortDir), [owners, sortKey, sortDir]);
  const listedOwners = useMemo(() => sortedOwners.slice(0, topLimit), [sortedOwners, topLimit]);

  const cpuPie = useMemo(() => topOwnersForPie(owners, "cpu_share_pct", topLimit), [owners, topLimit]);
  const memPie = useMemo(() => topOwnersForPie(owners, "memory_share_pct", topLimit), [owners, topLimit]);
  const storagePie = useMemo(() => topOwnersForPie(owners, "storage_share_pct", topLimit), [owners, topLimit]);

  const barData = useMemo(
    () =>
      owners.slice(0, topLimit).map((row) => ({
        owner: row.owner_key,
        cpu: row.cpu_share_pct,
        memory: row.memory_share_pct,
        storage: row.storage_share_pct,
      })),
    [owners, topLimit],
  );

  const currentCpu = latestPct(visibleSeries.length ? visibleSeries : series, "cpu_pct");
  const currentMem = latestPct(visibleSeries.length ? visibleSeries : series, "memory_pct");
  const currentDisk = latestPct(visibleSeries.length ? visibleSeries : series, "disk_pct");

  // Storage reclaim: thick provisioned vs datastore-committed bytes.
  // NOTE: storage_used_bytes = bytes committed on the datastore (VMDK file size),
  // NOT bytes written by the guest OS. True written data requires guest-level metrics.
  // For thick disks provisioned = fully pre-allocated regardless of guest writes.
  //
  // Thin fill ratio: thin VMs' committed/provisioned gives us the best available proxy
  // for how much of a disk's logical size is actually in use. We apply that ratio to
  // thick provisioned to estimate thick "actual usage" and derive a more realistic
  // reclaimable figure when converting thick → thin.
  const storageReclaim = useMemo(() => {
    let thickProvisioned = 0;
    let thickCommitted = 0;
    let thinProvisioned = 0;
    let thinCommitted = 0;
    let unknownCommitted = 0;
    for (const vm of scopedVms) {
      const provisioned = vm.storage_provisioned_bytes ?? 0;
      const committed = vm.storage_used_bytes ?? 0;
      if (vm.disk_provisioning === "thick") {
        thickProvisioned += provisioned;
        thickCommitted += committed;
      } else if (vm.disk_provisioning === "thin" || vm.disk_provisioning === "mixed") {
        thinProvisioned += provisioned;
        thinCommitted += committed;
      } else {
        unknownCommitted += committed;
      }
    }
    // Thin fill ratio = committed / provisioned across all thin VMs (fleet average)
    const thinFillRatio = thinProvisioned > 0 ? thinCommitted / thinProvisioned : null;
    // Estimated thick actual usage = thick provisioned × thin fill ratio
    // Falls back to thickCommitted if no thin data is available
    const thickEstActual = thinFillRatio !== null
      ? Math.round(thickProvisioned * thinFillRatio)
      : thickCommitted;
    // Conservative reclaimable (raw): provisioned minus committed on datastore
    const reclaimableRaw = Math.max(0, thickProvisioned - thickCommitted);
    // Estimated reclaimable after conversion (more realistic): provisioned × (1 - fill ratio)
    const reclaimableEst = Math.max(0, thickProvisioned - thickEstActual);
    const totalCommitted = thickCommitted + thinCommitted + unknownCommitted;
    return {
      thickProvisioned, thickCommitted, thickEstActual,
      thinProvisioned, thinCommitted, thinFillRatio,
      reclaimableRaw, reclaimableEst, totalCommitted,
      // keep reclaimable as the estimated figure for display
      reclaimable: reclaimableEst,
    };
  }, [scopedVms]);

  // Idle VM reclaim: VMs with days_idle >= idleThreshold (candidate for deletion)
  const IDLE_VM_DAYS = idleThreshold;
  const idleVmReclaim = useMemo(() => {
    const idleVms = scopedVms.filter((vm) => vm.days_idle !== null && vm.days_idle >= idleThreshold);
    let thickProvisioned = 0;
    let thickCommitted = 0;
    let thinProvisioned = 0;
    let thinCommitted = 0;
    let vmCount = 0;
    let cpuAllocatedMhz = 0;
    let cpuUsedMhz = 0;
    let memAllocatedMib = 0;
    let memUsedMib = 0;
    for (const vm of idleVms) {
      vmCount++;
      const provisioned = vm.storage_provisioned_bytes ?? 0;
      const committed = vm.storage_used_bytes ?? 0;
      if (vm.disk_provisioning === "thick") {
        thickProvisioned += provisioned;
        thickCommitted += committed;
      } else {
        thinProvisioned += provisioned;
        thinCommitted += committed;
      }
      // cpu_count × 1000 MHz = conservative 1 GHz/vCPU allocation estimate
      cpuAllocatedMhz += vm.cpu_count * 1000;
      cpuUsedMhz += vm.cpu_usage_mhz;
      memAllocatedMib += vm.memory_mib;
      memUsedMib += vm.memory_usage_mib;
    }
    return {
      vmCount,
      thickProvisioned,
      thickCommitted,
      thinProvisioned,
      thinCommitted,
      totalProvisioned: thickProvisioned + thinProvisioned,
      totalCommitted: thickCommitted + thinCommitted,
      cpuAllocatedMhz,
      cpuUsedMhz,
      memAllocatedMib,
      memUsedMib,
    };
  }, [scopedVms, idleThreshold]);

  const idleVmStoragePieData = useMemo(() => {
    const { thickProvisioned, thinCommitted } = idleVmReclaim;
    const { thinFillRatio } = storageReclaim;
    // Use fleet thin fill ratio to estimate thick actual use; fall back to raw committed
    const thickEstActual = thinFillRatio !== null
      ? Math.round(thickProvisioned * thinFillRatio)
      : idleVmReclaim.thickCommitted;
    const thickEstReclaimable = Math.max(0, thickProvisioned - thickEstActual);
    const total = thickEstActual + thinCommitted + thickEstReclaimable;
    if (total === 0) return [];
    const GiB = 1024 * 1024 * 1024;
    const thickLabel = thinFillRatio !== null
      ? `Thick VMs — est. actual (${(thinFillRatio * 100).toFixed(0)}% fill)`
      : "Thick VMs — committed on datastore";
    return [
      { name: thickLabel, value: Math.round(thickEstActual / GiB * 10) / 10, color: "#86c9a3" },
      { name: "Thick VMs — est. reclaimable gap", value: Math.round(thickEstReclaimable / GiB * 10) / 10, color: "#f07178" },
      { name: "Thin VMs — committed on datastore", value: Math.round(thinCommitted / GiB * 10) / 10, color: "#7eb8da" },
    ].filter((d) => d.value > 0);
  }, [idleVmReclaim, storageReclaim]);

  const idleVmCpuPieData = useMemo(() => {
    const { cpuAllocatedMhz, cpuUsedMhz, vmCount } = idleVmReclaim;
    if (vmCount === 0 || cpuAllocatedMhz === 0) return [];
    const reclaimable = Math.max(0, cpuAllocatedMhz - cpuUsedMhz);
    return [
      { name: "Currently in use", value: cpuUsedMhz, color: "#86c9a3" },
      { name: "Reclaimable (idle VMs)", value: reclaimable, color: "#f07178" },
    ].filter((d) => d.value > 0);
  }, [idleVmReclaim]);

  const idleVmMemPieData = useMemo(() => {
    const { memAllocatedMib, memUsedMib, vmCount } = idleVmReclaim;
    if (vmCount === 0 || memAllocatedMib === 0) return [];
    const reclaimable = Math.max(0, memAllocatedMib - memUsedMib);
    return [
      { name: "Currently in use", value: memUsedMib, color: "#7eb8da" },
      { name: "Reclaimable (idle VMs)", value: reclaimable, color: "#f07178" },
    ].filter((d) => d.value > 0);
  }, [idleVmReclaim]);

  const storagePieData = useMemo(() => {
    const { thickEstActual, thinCommitted, reclaimableEst, thinFillRatio } = storageReclaim;
    const total = thickEstActual + thinCommitted + reclaimableEst;
    if (total === 0) return [];
    const GiB = 1024 * 1024 * 1024;
    const label = thinFillRatio !== null
      ? `Est. thick actual use (${(thinFillRatio * 100).toFixed(0)}% fill)`
      : "Committed (thick VMs)";
    return [
      { name: label, value: Math.round(thickEstActual / GiB * 10) / 10, color: "#86c9a3" },
      { name: "Thin provisioned (committed)", value: Math.round(thinCommitted / GiB * 10) / 10, color: "#7eb8da" },
      { name: "Thick est. reclaimable (thick → thin)", value: Math.round(reclaimableEst / GiB * 10) / 10, color: "#f07178" },
    ].filter((d) => d.value > 0);
  }, [storageReclaim]);

  // Idle hosts: hosts where all VMs have been idle ≥ 30 days (or host has no VMs and low usage)
  const idleHosts = useMemo(() => {
    const IDLE_DAYS = 30;
    const pool = hostFilter ? hosts.filter((host) => host.id === hostFilter) : hosts;
    return pool.filter((host) => {
      const hostVms = vms.filter((vm) => vm.host_id === host.id);
      if (hostVms.length === 0) {
        // No VMs — consider idle if CPU usage is very low
        return host.cpu_usage_pct < 5;
      }
      return hostVms.every((vm) => vm.days_idle !== null && vm.days_idle >= IDLE_DAYS);
    });
  }, [hosts, vms, hostFilter]);

  const idleTotals = useMemo(() => {
    const totalCpuMhz = idleHosts.reduce((sum, h) => sum + h.cpu_cores * h.cpu_mhz, 0);
    const totalMemMib = idleHosts.reduce((sum, h) => sum + h.memory_mib, 0);
    const usedCpuMhz = idleHosts.reduce((sum, h) => sum + h.cpu_usage_mhz, 0);
    const usedMemMib = idleHosts.reduce((sum, h) => sum + h.memory_usage_mib, 0);
    return { totalCpuMhz, totalMemMib, usedCpuMhz, usedMemMib };
  }, [idleHosts]);

  // For overall fleet pie: active hosts vs idle hosts CPU & Memory
  const fleetCpuPie = useMemo(() => {
    const idleSet = new Set(idleHosts.map((h) => h.id));
    const activeHosts = hosts.filter((h) => !idleSet.has(h.id));
    const activeCpu = activeHosts.reduce((sum, h) => sum + h.cpu_usage_mhz, 0);
    const idleCpu = idleHosts.reduce((sum, h) => sum + h.cpu_cores * h.cpu_mhz, 0);
    if (activeCpu + idleCpu === 0) return [];
    return [
      { name: "Active hosts CPU", value: activeCpu },
      { name: "Idle host CPU (wasted)", value: idleCpu },
    ];
  }, [hosts, idleHosts]);

  const fleetMemPie = useMemo(() => {
    const idleSet = new Set(idleHosts.map((h) => h.id));
    const activeHosts = hosts.filter((h) => !idleSet.has(h.id));
    const activeMem = activeHosts.reduce((sum, h) => sum + h.memory_usage_mib, 0);
    const idleMem = idleHosts.reduce((sum, h) => sum + h.memory_mib, 0);
    if (activeMem + idleMem === 0) return [];
    return [
      { name: "Active hosts memory", value: activeMem },
      { name: "Idle host memory (wasted)", value: idleMem },
    ];
  }, [hosts, idleHosts]);

  return (
    <div className="stack monitor">
      {error ? <div className="banner bad">{error}</div> : null}

      <div className="monitor-toolbar panel">
        <div className="monitor-controls">
          <label>
            Time range
            <select value={hours} onChange={(event) => changeHours(Number(event.target.value))}>
              {HOUR_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Host
            <select value={hostFilter} onChange={(event) => changeHost(event.target.value)}>
              <option value="">All hosts (cluster view)</option>
              {hosts.map((host) => (
                <option key={host.id} value={host.id}>
                  {host.name}
                  {host.cluster_name ? ` · ${host.cluster_name}` : ""}
                </option>
              ))}
            </select>
          </label>
          <label>
            Storage source
            <select value={datastoreFilter} onChange={(event) => setDatastoreFilter(event.target.value)}>
              <option value="">{hostFilter ? "Average of this host's datastores" : "Average of all datastores"}</option>
              {storageSources.map((store) => (
                <option key={store.id} value={store.id}>
                  {store.name} ({store.usage_pct.toFixed(0)}%)
                </option>
              ))}
            </select>
          </label>
          <label>
            Owner filter
            <select value={ownerFilter} onChange={(event) => onOwnerFilter(event.target.value)}>
              <option value="">All owners (cluster view)</option>
              {owners.map((row) => (
                <option key={row.owner_key} value={row.owner_key}>
                  {row.owner_key} ({row.vm_count} VMs)
                </option>
              ))}
            </select>
          </label>
          <label>
            Pie chart limit
            <select value={topLimit} onChange={(event) => setTopLimit(Number(event.target.value))}>
              <option value={10}>Top 10 + Other</option>
              <option value={20}>Top 20 + Other</option>
              <option value={9999}>Show all</option>
            </select>
          </label>
          <label>
            Idle / reclaim threshold
            <select value={idleThreshold} onChange={(event) => setIdleThreshold(Number(event.target.value))}>
              {IDLE_THRESHOLD_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </label>
        </div>
        {ownerFilter || hostFilter || datastoreFilter || zoom ? (
          <div className="monitor-focus">
            {hostFilter ? (
              <>
                Host <strong>{hosts.find((host) => host.id === hostFilter)?.name || hostFilter}</strong>
              </>
            ) : null}
            {datastoreFilter ? (
              <>
                {hostFilter ? " · " : ""}
                Storage <strong>{storageSources.find((store) => store.id === datastoreFilter)?.name || datastoreFilter}</strong>
              </>
            ) : null}
            {ownerFilter ? (
              <>
                {hostFilter || datastoreFilter ? " · " : ""}
                Focused on <strong>{ownerFilter}</strong>
                <button className="text" onClick={() => onOpenMachines(ownerFilter)}>
                  View machines
                </button>
                <button className="text" onClick={() => onOwnerFilter("")}>
                  Clear owner
                </button>
              </>
            ) : null}
            {zoom ? (
              <>
                <span>
                  Zoomed {formatTooltipTime(zoom.from)} – {formatTooltipTime(zoom.to)}
                </span>
                <button className="text" onClick={zoomOut}>
                  Zoom out
                </button>
                <button className="text" onClick={resetZoom}>
                  Reset zoom
                </button>
              </>
            ) : null}
            {hostFilter || datastoreFilter ? (
              <button
                className="text"
                onClick={() => {
                  setHostFilter("");
                  setDatastoreFilter("");
                }}
              >
                Clear host/storage
              </button>
            ) : null}
          </div>
        ) : (
          <p className="monitor-hint">
            Charts show cluster utilization over the selected window (up to 14 days). Drag across a graph to zoom that range to full width. Filter by host or a specific datastore to isolate utilization.
          </p>
        )}
      </div>

      <div className="tiles monitor-tiles">
        <article>
          <span>CPU</span>
          <strong>{currentCpu.toFixed(0)}%</strong>
          <small>
            {hostFilter ? "Host utilization" : ownerFilter ? "Owner share of cluster" : "Cluster utilization"}
          </small>
        </article>
        <article>
          <span>Memory</span>
          <strong>{currentMem.toFixed(0)}%</strong>
          <small>
            {hostFilter ? "Host utilization" : ownerFilter ? "Owner share of cluster" : "Cluster utilization"}
          </small>
        </article>
        <article>
          <span>Storage</span>
          <strong>{currentDisk.toFixed(0)}%</strong>
          <small>
            {datastoreFilter
              ? "Selected datastore"
              : hostFilter
                ? "Host datastores (avg)"
                : ownerFilter
                  ? "Estimated owner share"
                  : "Datastore utilization"}
          </small>
        </article>
        <article>
          <span>Samples</span>
          <strong>{metrics?.points ?? 0}</strong>
          <small>{hours >= 24 ? `${Math.round(hours / 24)}d window` : `${hours}h window`}</small>
        </article>
      </div>

      <div className="monitor-grid charts-3">
        <UtilLineChart
          title="CPU"
          dataKey="cpu_pct"
          color="#d6f261"
          data={visibleSeries}
          selectFrom={selectFrom}
          selectTo={selectTo}
          onSelectStart={startSelect}
          onSelectMove={moveSelect}
        />
        <UtilLineChart
          title="Memory"
          dataKey="memory_pct"
          color="#86c9a3"
          data={visibleSeries}
          selectFrom={selectFrom}
          selectTo={selectTo}
          onSelectStart={startSelect}
          onSelectMove={moveSelect}
        />
        <UtilLineChart
          title="Storage"
          dataKey="disk_pct"
          color="#7eb8da"
          data={visibleSeries}
          selectFrom={selectFrom}
          selectTo={selectTo}
          onSelectStart={startSelect}
          onSelectMove={moveSelect}
          extra={
            <label className="chart-inline-filter">
              Source
              <select value={datastoreFilter} onChange={(event) => setDatastoreFilter(event.target.value)}>
                <option value="">{hostFilter ? "Host average" : "All datastores (average)"}</option>
                {storageSources.map((store) => (
                  <option key={store.id} value={store.id}>
                    {store.name}
                  </option>
                ))}
              </select>
            </label>
          }
        />
      </div>

      {/* Storage Reclaim Section */}
      {!ownerFilter && storageReclaim.thickProvisioned + storageReclaim.thinProvisioned > 0 ? (
        <>
          <header className="monitor-section-head">
            <div>
              <h2>Storage: thick provisioned vs actual consumption</h2>
              <p>
                Total thick provisioned:{" "}
                <strong>{bytes(storageReclaim.thickProvisioned)}</strong> &nbsp;·&nbsp; Actual consumed:{" "}
                <strong>{bytes(storageReclaim.totalCommitted)}</strong> &nbsp;·&nbsp; Est. reclaimable:{" "}
                <strong style={{ color: "#d6f261" }}>{bytes(storageReclaim.reclaimableEst)}</strong>
                {storageReclaim.thinFillRatio !== null && (
                  <> &nbsp;·&nbsp; Thin fill ratio: <strong style={{ color: "#7eb8da" }}>{(storageReclaim.thinFillRatio * 100).toFixed(1)}%</strong></>
                )}
              </p>
              <p style={{ fontSize: 11, color: "var(--muted)", marginTop: 4 }}>
                ⚠ Thick disks pre-allocate their full logical size regardless of guest writes.
                Since we cannot measure guest-written bytes directly, the estimated reclaimable space uses the
                thin VM fill ratio ({storageReclaim.thinFillRatio !== null ? `${(storageReclaim.thinFillRatio * 100).toFixed(1)}%` : "n/a"}) as a
                proxy for how full thick disks likely are. True written data requires guest-level metrics.
              </p>
            </div>
          </header>
          <div className="monitor-grid charts-1">
            <section className="panel monitor-chart compact">
              <header>
                <h2>Storage allocation breakdown (GiB)</h2>
                <p>Reclaimable space freed by converting thick → thin provisioned disks</p>
              </header>
              <div className="chart-wrap pie">
                {storagePieData.length ? (
                  <ResponsiveContainer width="100%" height={260}>
                    <PieChart>
                      <Pie
                        data={storagePieData}
                        dataKey="value"
                        nameKey="name"
                        cx="50%"
                        cy="50%"
                        innerRadius={60}
                        outerRadius={100}
                        paddingAngle={2}
                        label={({ value }: { value: number }) => `${value} GiB`}
                        labelLine
                      >
                        {storagePieData.map((d) => (
                          <Cell key={d.name} fill={d.color} />
                        ))}
                      </Pie>
                      <Tooltip
                        formatter={(value: number) => [`${value} GiB`, ""]}
                      />
                      <Legend layout="vertical" align="right" verticalAlign="middle" wrapperStyle={{ fontSize: 11 }} />
                    </PieChart>
                  </ResponsiveContainer>
                ) : (
                  <div className="chart-empty">No provisioning data available.</div>
                )}
              </div>
            </section>
          </div>
          {/* Idle VM reclaim: CPU, RAM, Storage */}
          {idleVmReclaim.vmCount > 0 ? (
            <>
            <div className="monitor-grid charts-3" style={{ marginTop: 0 }}>

              {/* CPU reclaimable */}
              <section className="panel monitor-chart compact">
                <header>
                  <h2>CPU reclaimable (&gt;{IDLE_VM_DAYS}d idle)</h2>
                  <p>
                    Allocated: <strong>{idleVmReclaim.cpuAllocatedMhz.toLocaleString()} MHz</strong> &nbsp;·&nbsp;
                    In use: <strong>{idleVmReclaim.cpuUsedMhz.toLocaleString()} MHz</strong> &nbsp;·&nbsp;
                    Reclaimable: <strong style={{ color: "#d6f261" }}>{Math.max(0, idleVmReclaim.cpuAllocatedMhz - idleVmReclaim.cpuUsedMhz).toLocaleString()} MHz</strong>
                  </p>
                  <p style={{ fontSize: 11, color: "var(--muted)", marginTop: 2 }}>Based on 1 GHz/vCPU allocation estimate</p>
                </header>
                <div className="chart-wrap pie">
                  {idleVmCpuPieData.length ? (
                    <ResponsiveContainer width="100%" height={220}>
                      <PieChart>
                        <Pie data={idleVmCpuPieData} dataKey="value" nameKey="name" cx="50%" cy="50%"
                          innerRadius={52} outerRadius={82} paddingAngle={2}
                          label={({ value }: { value: number }) => `${(value / 1000).toFixed(1)} GHz`} labelLine>
                          {idleVmCpuPieData.map((d) => <Cell key={d.name} fill={d.color} />)}
                        </Pie>
                        <Tooltip formatter={(v: number) => [`${v.toLocaleString()} MHz`, ""]} />
                        <Legend layout="horizontal" align="center" verticalAlign="bottom" wrapperStyle={{ fontSize: 10 }} />
                      </PieChart>
                    </ResponsiveContainer>
                  ) : <div className="chart-empty">No CPU data for idle VMs.</div>}
                </div>
              </section>

              {/* RAM reclaimable */}
              <section className="panel monitor-chart compact">
                <header>
                  <h2>RAM reclaimable (&gt;{IDLE_VM_DAYS}d idle)</h2>
                  <p>
                    Allocated: <strong>{gib(idleVmReclaim.memAllocatedMib)}</strong> &nbsp;·&nbsp;
                    In use: <strong>{gib(idleVmReclaim.memUsedMib)}</strong> &nbsp;·&nbsp;
                    Reclaimable: <strong style={{ color: "#d6f261" }}>{gib(Math.max(0, idleVmReclaim.memAllocatedMib - idleVmReclaim.memUsedMib))}</strong>
                  </p>
                  <p style={{ fontSize: 11, color: "var(--muted)", marginTop: 2 }}>Allocated = VM configured memory; in use = active balloon/swap</p>
                </header>
                <div className="chart-wrap pie">
                  {idleVmMemPieData.length ? (
                    <ResponsiveContainer width="100%" height={220}>
                      <PieChart>
                        <Pie data={idleVmMemPieData} dataKey="value" nameKey="name" cx="50%" cy="50%"
                          innerRadius={52} outerRadius={82} paddingAngle={2}
                          label={({ value }: { value: number }) => gib(value)} labelLine>
                          {idleVmMemPieData.map((d) => <Cell key={d.name} fill={d.color} />)}
                        </Pie>
                        <Tooltip formatter={(v: number) => [gib(v as number), ""]} />
                        <Legend layout="horizontal" align="center" verticalAlign="bottom" wrapperStyle={{ fontSize: 10 }} />
                      </PieChart>
                    </ResponsiveContainer>
                  ) : <div className="chart-empty">No memory data for idle VMs.</div>}
                </div>
              </section>

              {/* Storage reclaimable */}
              <section className="panel monitor-chart compact">
                <header>
                  <h2>Storage reclaimable (&gt;{IDLE_VM_DAYS}d idle)</h2>
                  <p>
                    Provisioned: <strong>{bytes(idleVmReclaim.totalProvisioned)}</strong> &nbsp;·&nbsp;
                    Committed: <strong>{bytes(idleVmReclaim.totalCommitted)}</strong>
                  </p>
                  <p style={{ fontSize: 11, color: "var(--muted)", marginTop: 2 }}>
                    Thick = full provisioned freed on delete; thin = committed bytes freed
                  </p>
                </header>
                <div className="chart-wrap pie">
                  {idleVmStoragePieData.length ? (
                    <ResponsiveContainer width="100%" height={220}>
                      <PieChart>
                        <Pie data={idleVmStoragePieData} dataKey="value" nameKey="name" cx="50%" cy="50%"
                          innerRadius={52} outerRadius={82} paddingAngle={2}
                          label={({ value }: { value: number }) => `${value} GiB`} labelLine>
                          {idleVmStoragePieData.map((d) => <Cell key={d.name} fill={d.color} />)}
                        </Pie>
                        <Tooltip formatter={(value: number) => [`${value} GiB`, ""]} />
                        <Legend layout="horizontal" align="center" verticalAlign="bottom" wrapperStyle={{ fontSize: 10 }} />
                      </PieChart>
                    </ResponsiveContainer>
                  ) : <div className="chart-empty">No storage data for idle VMs.</div>}
                </div>
              </section>
            </div>

            <section className="panel">
              <header>
                <h2>Reclaim summary by provisioning type</h2>
                <p>Space freed on datastore if all idle VMs (&gt;{IDLE_VM_DAYS} days) are deleted</p>
              </header>
                <div style={{ padding: "16px 0" }}>
                  {storageReclaim.thinFillRatio !== null && (
                    <p style={{ fontSize: 11, color: "var(--muted)", marginBottom: 8 }}>
                      Fleet thin fill ratio: <strong style={{ color: "#7eb8da" }}>{(storageReclaim.thinFillRatio * 100).toFixed(1)}%</strong>
                      {" "}— applied to thick provisioned size to estimate actual usage.
                      Deleting a thick VM always frees its full provisioned size from the datastore.
                    </p>
                  )}
                  <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse" }}>
                    <thead>
                      <tr style={{ color: "var(--muted)", borderBottom: "1px solid var(--border)" }}>
                        <th style={{ textAlign: "left", padding: "4px 8px" }}>Type</th>
                        <th style={{ textAlign: "right", padding: "4px 8px" }}>VMs</th>
                        <th style={{ textAlign: "right", padding: "4px 8px" }}>Provisioned</th>
                        <th style={{ textAlign: "right", padding: "4px 8px" }}>Committed (DS)</th>
                        <th style={{ textAlign: "right", padding: "4px 8px" }}>Est. actual use</th>
                        <th style={{ textAlign: "right", padding: "4px 8px" }}>Freed on delete</th>
                      </tr>
                    </thead>
                    <tbody>
                      {idleVmReclaim.thickProvisioned > 0 ? (() => {
                        const thickEstActual = storageReclaim.thinFillRatio !== null
                          ? Math.round(idleVmReclaim.thickProvisioned * storageReclaim.thinFillRatio)
                          : idleVmReclaim.thickCommitted;
                        return (
                          <tr style={{ borderBottom: "1px solid var(--border)" }}>
                            <td style={{ padding: "6px 8px" }}><span style={{ color: "#86c9a3" }}>●</span> Thick</td>
                            <td style={{ textAlign: "right", padding: "6px 8px" }}>
                              {scopedVms.filter((v) => v.days_idle !== null && v.days_idle >= IDLE_VM_DAYS && v.disk_provisioning === "thick").length}
                            </td>
                            <td style={{ textAlign: "right", padding: "6px 8px" }}>{bytes(idleVmReclaim.thickProvisioned)}</td>
                            <td style={{ textAlign: "right", padding: "6px 8px" }}>{bytes(idleVmReclaim.thickCommitted)}</td>
                            <td style={{ textAlign: "right", padding: "6px 8px", color: "#86c9a3" }}>{bytes(thickEstActual)}</td>
                            <td style={{ textAlign: "right", padding: "6px 8px", color: "#d6f261", fontWeight: 600 }}>
                              {bytes(idleVmReclaim.thickProvisioned)}
                            </td>
                          </tr>
                        );
                      })() : null}
                      {idleVmReclaim.thinProvisioned > 0 ? (
                        <tr style={{ borderBottom: "1px solid var(--border)" }}>
                          <td style={{ padding: "6px 8px" }}><span style={{ color: "#7eb8da" }}>●</span> Thin / mixed</td>
                          <td style={{ textAlign: "right", padding: "6px 8px" }}>
                            {scopedVms.filter((v) => v.days_idle !== null && v.days_idle >= IDLE_VM_DAYS && v.disk_provisioning !== "thick").length}
                          </td>
                          <td style={{ textAlign: "right", padding: "6px 8px" }}>{bytes(idleVmReclaim.thinProvisioned)}</td>
                          <td style={{ textAlign: "right", padding: "6px 8px" }}>{bytes(idleVmReclaim.thinCommitted)}</td>
                          <td style={{ textAlign: "right", padding: "6px 8px", color: "#7eb8da" }}>{bytes(idleVmReclaim.thinCommitted)}</td>
                          <td style={{ textAlign: "right", padding: "6px 8px", color: "#d6f261", fontWeight: 600 }}>
                            {bytes(idleVmReclaim.thinCommitted)}
                          </td>
                        </tr>
                      ) : null}
                    </tbody>
                    <tfoot>
                      {(() => {
                        const thickEstActual = storageReclaim.thinFillRatio !== null
                          ? Math.round(idleVmReclaim.thickProvisioned * storageReclaim.thinFillRatio)
                          : idleVmReclaim.thickCommitted;
                        return (
                          <tr style={{ borderTop: "2px solid var(--border)" }}>
                            <td style={{ padding: "8px 8px" }}><strong>Total</strong></td>
                            <td style={{ textAlign: "right", padding: "8px 8px" }}><strong>{idleVmReclaim.vmCount}</strong></td>
                            <td style={{ textAlign: "right", padding: "8px 8px" }}><strong>{bytes(idleVmReclaim.totalProvisioned)}</strong></td>
                            <td style={{ textAlign: "right", padding: "8px 8px" }}><strong>{bytes(idleVmReclaim.totalCommitted)}</strong></td>
                            <td style={{ textAlign: "right", padding: "8px 8px", color: "#86c9a3" }}>
                              <strong>{bytes(thickEstActual + idleVmReclaim.thinCommitted)}</strong>
                            </td>
                            <td style={{ textAlign: "right", padding: "8px 8px", color: "#d6f261", fontWeight: 600 }}>
                              <strong>
                                {bytes(idleVmReclaim.thickProvisioned + idleVmReclaim.thinCommitted)}
                              </strong>
                            </td>
                          </tr>
                        );
                      })()}
                    </tfoot>
                  </table>
                </div>
            </section>
            </>
          ) : (
            <p style={{ padding: "8px 0", color: "var(--muted)", fontSize: 13 }}>
              No VMs found idle for {IDLE_VM_DAYS}+ days.
            </p>
          )}
        </>
      ) : null}

      {/* Idle Hosts Section */}
      {!ownerFilter ? (
        <>
          <header className="monitor-section-head">
            <div>
              <h2>Idle hosts (&gt;30 days)</h2>
              <p>
                {idleHosts.length === 0
                  ? "No hosts found with all VMs idle for more than 30 days."
                  : <>
                      <strong>{idleHosts.length}</strong> host{idleHosts.length !== 1 ? "s" : ""} idle &gt;30 days &nbsp;·&nbsp;
                      Reclaimable CPU: <strong style={{ color: "#d6f261" }}>{idleTotals.totalCpuMhz.toLocaleString()} MHz</strong>
                      &nbsp;·&nbsp;
                      Reclaimable Memory: <strong style={{ color: "#86c9a3" }}>{gib(idleTotals.totalMemMib)}</strong>
                      {" "}by shutting them down
                    </>
                }
              </p>
            </div>
          </header>

          {idleHosts.length > 0 ? (
            <>
              <div className="monitor-grid charts-2">
                <section className="panel monitor-chart compact">
                  <header><h2>CPU: active vs idle hosts</h2></header>
                  <div className="chart-wrap pie">
                    {fleetCpuPie.length ? (
                      <ResponsiveContainer width="100%" height={240}>
                        <PieChart>
                          <Pie
                            data={fleetCpuPie}
                            dataKey="value"
                            nameKey="name"
                            cx="50%"
                            cy="50%"
                            innerRadius={52}
                            outerRadius={88}
                            paddingAngle={2}
                            label={({ percent }) => `${(percent * 100).toFixed(0)}%`}
                          >
                            <Cell key="active" fill="#86c9a3" />
                            <Cell key="idle" fill="#f07178" />
                          </Pie>
                          <Tooltip formatter={(v: number) => [`${v.toLocaleString()} MHz`, ""]} />
                          <Legend layout="vertical" align="right" verticalAlign="middle" wrapperStyle={{ fontSize: 11 }} />
                        </PieChart>
                      </ResponsiveContainer>
                    ) : (
                      <div className="chart-empty">No host CPU data.</div>
                    )}
                  </div>
                </section>
                <section className="panel monitor-chart compact">
                  <header><h2>Memory: active vs idle hosts</h2></header>
                  <div className="chart-wrap pie">
                    {fleetMemPie.length ? (
                      <ResponsiveContainer width="100%" height={240}>
                        <PieChart>
                          <Pie
                            data={fleetMemPie}
                            dataKey="value"
                            nameKey="name"
                            cx="50%"
                            cy="50%"
                            innerRadius={52}
                            outerRadius={88}
                            paddingAngle={2}
                            label={({ percent }) => `${(percent * 100).toFixed(0)}%`}
                          >
                            <Cell key="active" fill="#7eb8da" />
                            <Cell key="idle" fill="#f07178" />
                          </Pie>
                          <Tooltip formatter={(v: number) => [gib(v), ""]} />
                          <Legend layout="vertical" align="right" verticalAlign="middle" wrapperStyle={{ fontSize: 11 }} />
                        </PieChart>
                      </ResponsiveContainer>
                    ) : (
                      <div className="chart-empty">No host memory data.</div>
                    )}
                  </div>
                </section>
              </div>

              <section className="panel">
                <header>
                  <h2>Idle host details</h2>
                  <p>All VMs on these hosts have been idle for more than 30 days. Shutting them down saves the resources below.</p>
                </header>
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>Host</th>
                        <th>Cluster</th>
                        <th>VMs</th>
                        <th>CPU capacity</th>
                        <th>CPU usage</th>
                        <th>Memory capacity</th>
                        <th>Memory usage</th>
                      </tr>
                    </thead>
                    <tbody>
                      {idleHosts.map((host) => (
                        <tr key={host.id}>
                          <td><strong>{host.name}</strong></td>
                          <td>{host.cluster_name || "—"}</td>
                          <td>{host.vm_count}</td>
                          <td>{(host.cpu_cores * host.cpu_mhz).toLocaleString()} MHz</td>
                          <td>{host.cpu_usage_mhz.toLocaleString()} MHz ({host.cpu_usage_pct.toFixed(1)}%)</td>
                          <td>{gib(host.memory_mib)}</td>
                          <td>{gib(host.memory_usage_mib)} ({host.memory_usage_pct.toFixed(1)}%)</td>
                        </tr>
                      ))}
                    </tbody>
                    <tfoot>
                      <tr>
                        <td colSpan={3}><strong>Total reclaimable</strong></td>
                        <td><strong>{idleTotals.totalCpuMhz.toLocaleString()} MHz</strong></td>
                        <td><strong>{idleTotals.usedCpuMhz.toLocaleString()} MHz</strong></td>
                        <td><strong>{gib(idleTotals.totalMemMib)}</strong></td>
                        <td><strong>{gib(idleTotals.usedMemMib)}</strong></td>
                      </tr>
                    </tfoot>
                  </table>
                </div>
              </section>
            </>
          ) : null}
        </>
      ) : null}

      {!ownerFilter ? (
        <>
          <header className="monitor-section-head">
            <div>
              <h2>Utilization by owner</h2>
              <p>Share of total CPU, memory, and estimated storage. Click a slice or row to filter or open Machines.</p>
            </div>
          </header>

          <div className="monitor-grid charts-3">
            <OwnerPieChart title="CPU share" data={cpuPie} onSelect={onOwnerFilter} />
            <OwnerPieChart title="Memory share" data={memPie} onSelect={onOwnerFilter} />
            <OwnerPieChart title="Storage share" data={storagePie} onSelect={onOwnerFilter} />
          </div>

          <section className="panel monitor-chart">
            <header>
              <h2>Top owners comparison</h2>
              <p>Percentage of fleet resources by owner</p>
            </header>
            <div className="chart-wrap wide">
              {barData.length ? (
                <ResponsiveContainer width="100%" height={Math.max(260, barData.length * 28)}>
                  <BarChart data={barData} layout="vertical" margin={{ top: 8, right: 16, left: 8, bottom: 8 }}>
                    <CartesianGrid stroke="rgba(154, 165, 140, 0.12)" horizontal={false} />
                    <XAxis type="number" domain={[0, 100]} stroke="var(--muted)" fontSize={11} tickFormatter={(v) => `${v}%`} />
                    <YAxis type="category" dataKey="owner" stroke="var(--muted)" fontSize={11} width={100} />
                    <Tooltip content={<ChartTooltip />} />
                    <Legend />
                    <Bar dataKey="cpu" name="CPU" fill="#d6f261" radius={[0, 4, 4, 0]} />
                    <Bar dataKey="memory" name="Memory" fill="#86c9a3" radius={[0, 4, 4, 0]} />
                    <Bar dataKey="storage" name="Storage" fill="#7eb8da" radius={[0, 4, 4, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              ) : (
                <div className="chart-empty">No owner breakdown available.</div>
              )}
            </div>
          </section>

          <section className="panel">
            <header>
              <h2>Owner resource list</h2>
              <p>Select a user to filter charts or jump to their machines.</p>
            </header>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <SortHeader label="Owner" active={sortKey === "owner_key"} dir={sortDir} onClick={() => pickSort("owner_key")} />
                    <SortHeader label="VMs" active={sortKey === "vm_count"} dir={sortDir} onClick={() => pickSort("vm_count")} />
                    <SortHeader label="CPU share" active={sortKey === "cpu_share_pct"} dir={sortDir} onClick={() => pickSort("cpu_share_pct")} />
                    <SortHeader label="Memory share" active={sortKey === "memory_share_pct"} dir={sortDir} onClick={() => pickSort("memory_share_pct")} />
                    <SortHeader label="Storage est." active={sortKey === "storage_share_pct"} dir={sortDir} onClick={() => pickSort("storage_share_pct")} />
                    <SortHeader label="CPU used" active={sortKey === "cpu_usage_mhz"} dir={sortDir} onClick={() => pickSort("cpu_usage_mhz")} />
                    <SortHeader label="Memory used" active={sortKey === "memory_usage_mib"} dir={sortDir} onClick={() => pickSort("memory_usage_mib")} />
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {listedOwners.map((row) => (
                    <tr key={row.owner_key} className="clickable" onClick={() => onOwnerFilter(row.owner_key)}>
                      <td>
                        <strong>{row.owner_key}</strong>
                      </td>
                      <td>{row.vm_count}</td>
                      <td>{row.cpu_share_pct.toFixed(1)}%</td>
                      <td>{row.memory_share_pct.toFixed(1)}%</td>
                      <td>{row.storage_share_pct.toFixed(1)}%</td>
                      <td>{row.cpu_usage_mhz} MHz</td>
                      <td>{gib(row.memory_usage_mib)}</td>
                      <td>
                        <button
                          className="text"
                          onClick={(event) => {
                            event.stopPropagation();
                            onOpenMachines(row.owner_key);
                          }}
                        >
                          Machines
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </>
      ) : null}
    </div>
  );
});
