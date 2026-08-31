import type { VirtualMachine } from "./types";

/** Local UI cap for one reviewed batch. Not a vSphere or vMotion limit. */
export const BATCH_LIMIT = 50;

export const BATCH_LIMIT_HINT =
  "vFleet reviews at most 50 VMs per batch so one request cannot stall the local relay. Disk conversions are still queued as independent persistent jobs. This is not a vSphere or vMotion cap; extra VMs stay selected for a second batch.";

export function batchTone(count: number): "ok" | "full" | "over" {
  if (count > BATCH_LIMIT) return "over";
  if (count === BATCH_LIMIT) return "full";
  return "ok";
}

/** Higher = keep in the first batch. Defers idle, powered-off, and small VMs. */
export function impactScore(vm: VirtualMachine): number {
  const power = vm.power_state === "POWERED_ON" ? 400 : vm.power_state === "SUSPENDED" ? 120 : 10;
  const live = vm.cpu_usage_mhz + vm.memory_usage_mib / 4;
  const size = vm.cpu_count * 8 + vm.memory_mib / 256 + vm.storage_provisioned_bytes / (8 * 1024 ** 3);
  const idle = vm.power_state === "POWERED_ON" ? vm.idle_score : 0;
  return power + live + size - idle;
}

export function splitBatch(vms: VirtualMachine[], limit = BATCH_LIMIT): { keep: VirtualMachine[]; defer: VirtualMachine[] } {
  const ranked = [...vms].sort((a, b) => {
    const delta = impactScore(b) - impactScore(a);
    if (delta !== 0) return delta;
    return a.name.localeCompare(b.name);
  });
  return { keep: ranked.slice(0, limit), defer: ranked.slice(limit) };
}
