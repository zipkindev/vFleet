import { useEffect, useMemo, useState } from "react";
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
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { fetchMetrics } from "./api";
import { gib } from "./format";
import { sortMonitorOwners, toggleSortDir, type MonitorOwnerSortKey, type SortDir } from "./sort";
import type { MetricsResponse, OwnerUtilization } from "./types";

const HOUR_OPTIONS = [
  { label: "1 hour", value: 1 },
  { label: "6 hours", value: 6 },
  { label: "24 hours", value: 24 },
  { label: "7 days", value: 168 },
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

type Props = {
  ownerFilter: string;
  onOwnerFilter: (owner: string) => void;
  onOpenMachines: (owner: string) => void;
};

function formatAxisTime(ts: string) {
  const date = new Date(ts);
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

function topOwnersForPie(
  owners: OwnerUtilization[],
  metric: "cpu_share_pct" | "memory_share_pct" | "storage_share_pct",
  limit = 20,
): PieRow[] {
  const sorted = [...owners].sort((a, b) => b[metric] - a[metric]);
  const top = sorted.slice(0, limit);
  const rest = sorted.slice(limit);
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
}: {
  title: string;
  dataKey: "cpu_pct" | "memory_pct" | "disk_pct";
  color: string;
  data: MetricsResponse["series"];
}) {
  const chartData = data.map((row) => ({
    ts: row.ts,
    value: row[dataKey],
  }));

  return (
    <section className="panel monitor-chart">
      <header>
        <h2>{title}</h2>
        <p>Utilization over time</p>
      </header>
      <div className="chart-wrap">
        {chartData.length ? (
          <ResponsiveContainer width="100%" height={220}>
            <AreaChart data={chartData} margin={{ top: 8, right: 12, left: -8, bottom: 0 }}>
              <defs>
                <linearGradient id={`grad-${dataKey}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor={color} stopOpacity={0.45} />
                  <stop offset="95%" stopColor={color} stopOpacity={0.02} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="rgba(154, 165, 140, 0.15)" vertical={false} />
              <XAxis
                dataKey="ts"
                tickFormatter={formatAxisTime}
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
  return (
    <section className="panel monitor-chart compact">
      <header>
        <h2>{title}</h2>
      </header>
      <div className="chart-wrap pie">
        {data.length ? (
          <ResponsiveContainer width="100%" height={240}>
            <PieChart>
              <Pie
                data={data}
                dataKey="value"
                nameKey="name"
                cx="50%"
                cy="50%"
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
              <Legend layout="vertical" align="right" verticalAlign="middle" wrapperStyle={{ fontSize: 11 }} />
            </PieChart>
          </ResponsiveContainer>
        ) : (
          <div className="chart-empty">No owner data yet.</div>
        )}
      </div>
    </section>
  );
}

export function MonitoringView({ ownerFilter, onOwnerFilter, onOpenMachines }: Props) {
  const [hours, setHours] = useState(24);
  const [metrics, setMetrics] = useState<MetricsResponse | null>(null);
  const [error, setError] = useState("");
  const [topLimit, setTopLimit] = useState(20);
  const [sortKey, setSortKey] = useState<MonitorOwnerSortKey>("cpu_share_pct");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  function pickSort(key: MonitorOwnerSortKey) {
    setSortDir((dir) => toggleSortDir(sortKey, key, dir));
    setSortKey(key);
  }

  async function load() {
    try {
      const payload = await fetchMetrics({ hours, owner: ownerFilter || undefined });
      setMetrics(payload);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load metrics");
    }
  }

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 8000);
    return () => window.clearInterval(timer);
  }, [hours, ownerFilter]);

  const series = metrics?.series ?? [];
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

  const currentCpu = latestPct(series, "cpu_pct");
  const currentMem = latestPct(series, "memory_pct");
  const currentDisk = latestPct(series, "disk_pct");

  return (
    <div className="stack monitor">
      {error ? <div className="banner bad">{error}</div> : null}

      <div className="monitor-toolbar panel">
        <div className="monitor-controls">
          <label>
            Time range
            <select value={hours} onChange={(event) => setHours(Number(event.target.value))}>
              {HOUR_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
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
        </div>
        {ownerFilter ? (
          <div className="monitor-focus">
            Focused on <strong>{ownerFilter}</strong>
            <button className="text" onClick={() => onOpenMachines(ownerFilter)}>
              View machines
            </button>
            <button className="text" onClick={() => onOwnerFilter("")}>
              Clear
            </button>
          </div>
        ) : (
          <p className="monitor-hint">Charts reflect overall cluster utilization. Filter by owner to drill into a single user.</p>
        )}
      </div>

      <div className="tiles monitor-tiles">
        <article>
          <span>CPU</span>
          <strong>{currentCpu.toFixed(0)}%</strong>
          <small>{ownerFilter ? "Owner share of cluster" : "Cluster utilization"}</small>
        </article>
        <article>
          <span>Memory</span>
          <strong>{currentMem.toFixed(0)}%</strong>
          <small>{ownerFilter ? "Owner share of cluster" : "Cluster utilization"}</small>
        </article>
        <article>
          <span>Storage</span>
          <strong>{currentDisk.toFixed(0)}%</strong>
          <small>{ownerFilter ? "Estimated owner share" : "Datastore utilization"}</small>
        </article>
        <article>
          <span>Samples</span>
          <strong>{metrics?.points ?? 0}</strong>
          <small>{hours >= 24 ? `${Math.round(hours / 24)}d window` : `${hours}h window`}</small>
        </article>
      </div>

      <div className="monitor-grid charts-3">
        <UtilLineChart title="CPU" dataKey="cpu_pct" color="#d6f261" data={series} />
        <UtilLineChart title="Memory" dataKey="memory_pct" color="#86c9a3" data={series} />
        <UtilLineChart title="Storage" dataKey="disk_pct" color="#7eb8da" data={series} />
      </div>

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
}
