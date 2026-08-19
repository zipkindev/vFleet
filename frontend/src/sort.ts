import type { OwnerReport, OwnerUtilization, VirtualMachine } from "./types";
import { diskLabel, powerLabel, storageGib } from "./format";

export type SortDir = "asc" | "desc";

export type OwnerSortKey =
  | "owner_key"
  | "vm_count"
  | "powered_on"
  | "cpu_count"
  | "memory_mib"
  | "idle_candidates";

export type MonitorOwnerSortKey =
  | "owner_key"
  | "vm_count"
  | "cpu_share_pct"
  | "memory_share_pct"
  | "storage_share_pct"
  | "cpu_usage_mhz"
  | "memory_usage_mib";

export type MachineSortKey =
  | "name"
  | "owner_key"
  | "power_state"
  | "cluster_name"
  | "cpu_count"
  | "memory_mib"
  | "storage_provisioned_bytes"
  | "disk_provisioning"
  | "last_activity";

export type MachineFilters = Partial<Record<MachineSortKey, string>>;

export type ReclaimSortKey =
  | "name"
  | "owner_key"
  | "idle_score"
  | "memory_mib"
  | "storage_provisioned_bytes"
  | "disk_provisioning"
  | "days_idle"
  | "reclaim_reason";

export type ReclaimFilters = Partial<Record<ReclaimSortKey, string>>;

export function toggleSortDir<T extends string>(activeKey: T, clickedKey: T, dir: SortDir): SortDir {
  if (activeKey !== clickedKey) return "desc";
  return dir === "desc" ? "asc" : "desc";
}

function compareStrings(a: string, b: string, sign: number): number {
  return sign * a.localeCompare(b, undefined, { sensitivity: "base" });
}

function compareNumbers(a: number, b: number, sign: number): number {
  return sign * (a - b);
}

export function sortOwners(rows: OwnerReport[], key: OwnerSortKey, dir: SortDir): OwnerReport[] {
  const sign = dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    if (key === "owner_key") {
      return compareStrings(a.owner_key, b.owner_key, sign);
    }
    return compareNumbers(a[key], b[key], sign);
  });
}

export function sortMonitorOwners(
  rows: OwnerUtilization[],
  key: MonitorOwnerSortKey,
  dir: SortDir,
): OwnerUtilization[] {
  const sign = dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    if (key === "owner_key") {
      return compareStrings(a.owner_key, b.owner_key, sign);
    }
    return compareNumbers(a[key], b[key], sign);
  });
}

export function sortMachines(vms: VirtualMachine[], key: MachineSortKey, dir: SortDir): VirtualMachine[] {
  const sign = dir === "asc" ? 1 : -1;
  return [...vms].sort((a, b) => {
    switch (key) {
      case "name":
        return compareStrings(a.name, b.name, sign);
      case "owner_key":
        return compareStrings(a.owner_key, b.owner_key, sign);
      case "power_state":
        return compareStrings(powerLabel(a.power_state), powerLabel(b.power_state), sign);
      case "cluster_name":
        return compareStrings(`${a.cluster_name} ${a.host_name}`, `${b.cluster_name} ${b.host_name}`, sign);
      case "cpu_count":
        return compareNumbers(a.cpu_count, b.cpu_count, sign);
      case "memory_mib":
        return compareNumbers(a.memory_mib, b.memory_mib, sign);
      case "storage_provisioned_bytes":
        return compareNumbers(a.storage_provisioned_bytes, b.storage_provisioned_bytes, sign);
      case "disk_provisioning":
        return compareStrings(diskLabel(a.disk_provisioning), diskLabel(b.disk_provisioning), sign);
      case "last_activity": {
        const missing = dir === "desc" ? -Infinity : Infinity;
        const aTs = a.last_activity ? Date.parse(a.last_activity) : missing;
        const bTs = b.last_activity ? Date.parse(b.last_activity) : missing;
        return compareNumbers(Number.isNaN(aTs) ? missing : aTs, Number.isNaN(bTs) ? missing : bTs, sign);
      }
    }
  });
}

export function sortReclaimVms(vms: VirtualMachine[], key: ReclaimSortKey, dir: SortDir): VirtualMachine[] {
  const sign = dir === "asc" ? 1 : -1;
  return [...vms].sort((a, b) => {
    switch (key) {
      case "name":
        return compareStrings(a.name, b.name, sign);
      case "owner_key":
        return compareStrings(a.owner_key, b.owner_key, sign);
      case "idle_score":
        return compareNumbers(a.idle_score, b.idle_score, sign);
      case "memory_mib":
        return compareNumbers(a.memory_mib, b.memory_mib, sign);
      case "storage_provisioned_bytes":
        return compareNumbers(a.storage_provisioned_bytes, b.storage_provisioned_bytes, sign);
      case "disk_provisioning":
        return compareStrings(diskLabel(a.disk_provisioning), diskLabel(b.disk_provisioning), sign);
      case "days_idle": {
        const missing = dir === "desc" ? -Infinity : Infinity;
        return compareNumbers(a.days_idle ?? missing, b.days_idle ?? missing, sign);
      }
      case "reclaim_reason":
        return compareStrings(a.reclaim_reason ?? "", b.reclaim_reason ?? "", sign);
    }
  });
}

function machineCellText(vm: VirtualMachine, key: MachineSortKey): string {
  switch (key) {
    case "name":
      return `${vm.name} ${vm.ip_address ?? ""} ${vm.guest_os}`;
    case "owner_key":
      return `${vm.owner_key} ${vm.owner_source}`;
    case "power_state":
      return powerLabel(vm.power_state);
    case "cluster_name":
      return `${vm.cluster_name} ${vm.host_name}`;
    case "cpu_count":
      return `${vm.cpu_count} ${vm.cpu_usage_pct.toFixed(0)} ${vm.cpu_usage_mhz}`;
    case "memory_mib":
      return `${(vm.memory_mib / 1024).toFixed(1)} ${vm.memory_usage_pct.toFixed(0)}`;
    case "storage_provisioned_bytes":
      return storageGib(vm.storage_provisioned_bytes).toFixed(1);
    case "disk_provisioning":
      return diskLabel(vm.disk_provisioning);
    case "last_activity":
      return `${vm.last_activity ?? ""} ${vm.last_activity_source}`;
  }
}

function reclaimCellText(vm: VirtualMachine, key: ReclaimSortKey): string {
  switch (key) {
    case "name":
      return `${vm.name} ${vm.cluster_name}`;
    case "owner_key":
      return vm.owner_key;
    case "idle_score":
      return String(vm.idle_score);
    case "memory_mib":
      return `${vm.cpu_count} vCPU ${vm.cpu_usage_pct.toFixed(0)}% ${(vm.memory_mib / 1024).toFixed(1)} GiB ${vm.memory_usage_pct.toFixed(0)}%`;
    case "storage_provisioned_bytes":
      return storageGib(vm.storage_provisioned_bytes).toFixed(1);
    case "disk_provisioning":
      return diskLabel(vm.disk_provisioning);
    case "days_idle":
      return vm.days_idle != null ? `${vm.days_idle.toFixed(0)}d` : "unknown";
    case "reclaim_reason":
      return vm.reclaim_reason ?? "";
  }
}

export function matchesColumnFilter(haystack: string, query: string): boolean {
  const raw = query.trim();
  if (!raw) return true;
  const compare = raw.match(/^(>=|<=|!=|>|<|=)\s*(-?\d+(?:\.\d+)?)\s*$/);
  if (compare) {
    const op = compare[1];
    const target = Number(compare[2]);
    const numbers = [...haystack.matchAll(/-?\d+(?:\.\d+)?/g)].map((match) => Number(match[0]));
    if (!numbers.length) return false;
    return numbers.some((value) => {
      if (op === ">") return value > target;
      if (op === "<") return value < target;
      if (op === ">=") return value >= target;
      if (op === "<=") return value <= target;
      if (op === "!=") return value !== target;
      return value === target;
    });
  }
  return haystack.toLowerCase().includes(raw.toLowerCase());
}

export function filterMachines(vms: VirtualMachine[], filters: MachineFilters): VirtualMachine[] {
  const active = (Object.entries(filters) as [MachineSortKey, string][]).filter(([, value]) => value.trim());
  if (!active.length) return vms;
  return vms.filter((vm) => active.every(([key, query]) => matchesColumnFilter(machineCellText(vm, key), query)));
}

export function filterReclaimVms(vms: VirtualMachine[], filters: ReclaimFilters): VirtualMachine[] {
  const active = (Object.entries(filters) as [ReclaimSortKey, string][]).filter(([, value]) => value.trim());
  if (!active.length) return vms;
  return vms.filter((vm) => active.every(([key, query]) => matchesColumnFilter(reclaimCellText(vm, key), query)));
}
