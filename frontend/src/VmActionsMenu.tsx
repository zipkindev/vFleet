import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { ActionName, ConnectionInfo, VirtualMachine } from "./types";

type MenuAction = {
  id: string;
  label: string;
  danger?: boolean;
  disabled?: boolean;
  hint?: string;
  onClick: () => void;
};

type MenuPos = {
  top: number;
  left: number;
  maxHeight: number;
};

type VmActionsMenuProps = {
  vm: VirtualMachine;
  connection: ConnectionInfo | null;
  onConsole: (vm: VirtualMachine) => void;
  onOpenVcenter: (vm: VirtualMachine) => void;
  onAction: (vm: VirtualMachine, action: ActionName) => void;
  onMigrate: (vm: VirtualMachine) => void;
  onRename: (vm: VirtualMachine) => void;
  onDrsOverride: (vm: VirtualMachine) => void;
  onDiskConvert: (vm: VirtualMachine) => void;
  onToolsDeploy: (vm: VirtualMachine) => void;
  onHardware: (vm: VirtualMachine) => void;
  onStorageReconcile: (vm: VirtualMachine) => void;
};

function powerOn(vm: VirtualMachine) {
  return vm.power_state === "POWERED_OFF" || vm.power_state === "SUSPENDED";
}

function poweredOn(vm: VirtualMachine) {
  return vm.power_state === "POWERED_ON";
}

function menuPosition(button: HTMLElement, menu: HTMLElement | null): MenuPos {
  const rect = button.getBoundingClientRect();
  const pad = 8;
  const gap = 6;
  const width = menu?.offsetWidth || 228;
  const height = menu?.offsetHeight || 0;
  const spaceBelow = window.innerHeight - rect.bottom - pad;
  const spaceAbove = rect.top - pad;
  const preferBelow = spaceBelow >= 240 || spaceBelow >= spaceAbove;
  const maxHeight = Math.max(160, preferBelow ? spaceBelow - gap : spaceAbove - gap);
  const shown = height > 0 ? Math.min(height, maxHeight) : Math.min(360, maxHeight);
  let left = rect.left;
  if (left + width > window.innerWidth - pad) left = rect.right - width;
  left = Math.max(pad, Math.min(left, window.innerWidth - width - pad));
  const top = preferBelow ? rect.bottom + gap : rect.top - gap - shown;
  return { top, left, maxHeight };
}

export function VmActionsMenu({
  vm,
  connection,
  onConsole,
  onOpenVcenter,
  onAction,
  onMigrate,
  onRename,
  onDrsOverride,
  onDiskConvert,
  onToolsDeploy,
  onHardware,
  onStorageReconcile,
}: VmActionsMenuProps) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<MenuPos | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const btnRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const live = connection?.mode !== "demo" && Boolean(connection?.connected) && !connection?.stale;
  const demoHint = connection?.mode === "demo" ? "Connect to vSphere first" : connection?.stale ? "Reconnect to vSphere" : "vSphere unavailable";

  useLayoutEffect(() => {
    if (!open) return;
    const button = btnRef.current;
    if (!button) return;

    function place() {
      const target = btnRef.current;
      if (!target) return;
      setPos(menuPosition(target, menuRef.current));
    }

    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    function onDown(event: MouseEvent) {
      const node = event.target as Node;
      if (wrapRef.current?.contains(node) || menuRef.current?.contains(node)) return;
      setOpen(false);
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  function run(action: () => void) {
    setOpen(false);
    action();
  }

  const consoleItems: MenuAction[] = [
    {
      id: "console",
      label: "Open console (VMRC)",
      disabled: !live,
      hint: live ? "Launches VMware Remote Console if installed" : demoHint,
      onClick: () => run(() => onConsole(vm)),
    },
    {
      id: "vcenter",
      label: "Open in vCenter",
      disabled: connection?.mode !== "vcenter" || !connection.host,
      hint: connection?.mode !== "vcenter" ? demoHint : "Opens the VM console page in vCenter UI",
      onClick: () => run(() => onOpenVcenter(vm)),
    },
  ];

  const powerItems: MenuAction[] = [
    {
      id: "start",
      label: "Power on",
      disabled: !live || !powerOn(vm),
      hint: !live ? demoHint : powerOn(vm) ? undefined : "Already running",
      onClick: () => run(() => onAction(vm, "start")),
    },
    {
      id: "shutdown",
      label: "Guest shutdown",
      disabled: !live || !poweredOn(vm),
      hint: !live ? demoHint : poweredOn(vm) ? "Graceful stop via VMware Tools" : "VM is not powered on",
      onClick: () => run(() => onAction(vm, "shutdown")),
    },
    {
      id: "reboot",
      label: "Guest reboot",
      disabled: !live || !poweredOn(vm),
      hint: !live ? demoHint : poweredOn(vm) ? "Graceful reboot via Tools" : "VM is not powered on",
      onClick: () => run(() => onAction(vm, "reboot")),
    },
    {
      id: "reset",
      label: "Reset",
      danger: true,
      disabled: !live || !poweredOn(vm),
      hint: !live ? demoHint : poweredOn(vm) ? "Hard reset" : "VM is not powered on",
      onClick: () => run(() => onAction(vm, "reset")),
    },
    {
      id: "power_off",
      label: "Hard power off",
      danger: true,
      disabled: !live || !poweredOn(vm),
      hint: !live ? demoHint : poweredOn(vm) ? "Immediate power off" : "VM is not powered on",
      onClick: () => run(() => onAction(vm, "power_off")),
    },
    {
      id: "suspend",
      label: "Suspend",
      disabled: !live || !poweredOn(vm),
      hint: !live ? demoHint : poweredOn(vm) ? "Pause the VM" : "VM is not powered on",
      onClick: () => run(() => onAction(vm, "suspend")),
    },
  ];

  const manageItems: MenuAction[] = [
    {
      id: "hardware",
      label: "Configure hardware",
      disabled: !live || !connection?.capabilities?.vm_hardware,
      hint: !live ? demoHint : "Set CPU, memory, disk capacity, or mounted ISO",
      onClick: () => run(() => onHardware(vm)),
    },
    {
      id: "deploy_tools",
      label: "Deploy VMware Tools automatically",
      disabled: !live || !poweredOn(vm),
      hint: !live ? demoHint : poweredOn(vm) ? "Windows via WinRM or Linux via SSH" : "Power on the VM first",
      onClick: () => run(() => onToolsDeploy(vm)),
    },
    {
      id: "mount_tools",
      label: "Mount VMware Tools installer",
      disabled: !live || !poweredOn(vm),
      hint: !live
        ? demoHint
        : poweredOn(vm)
          ? "Connects the host-provided Tools ISO; start setup inside the guest"
          : "Power on the VM first",
      onClick: () => run(() => onAction(vm, "mount_tools")),
    },
    {
      id: "rename",
      label: "Rename",
      disabled: !live,
      hint: !live ? demoHint : "Change the VM display name in vSphere",
      onClick: () => run(() => onRename(vm)),
    },
    {
      id: "disk_convert",
      label: "Convert disk provisioning",
      disabled: !live || !connection?.capabilities?.disk_convert,
      hint: !live ? demoHint : "Review disks and queue a thick/thin conversion",
      onClick: () => run(() => onDiskConvert(vm)),
    },
    {
      id: "storage_reconcile",
      label: "Reconcile storage and inventory",
      disabled: !live || !connection?.capabilities?.datastores,
      hint: !live ? demoHint : "Find preserved conversion sources and unattached datastore artifacts",
      onClick: () => run(() => onStorageReconcile(vm)),
    },
    {
      id: "migrate",
      label: "Migrate / Clone",
      disabled: !live || !connection?.capabilities?.migrate,
      hint: !live ? demoHint : "Relocate via vMotion, or clone onto a host when swap/vMotion cannot",
      onClick: () => run(() => onMigrate(vm)),
    },
    {
      id: "drs_override",
      label: "Pin on current host (DRS override)",
      disabled: !live || !connection?.capabilities?.drs || vm.drs_override || !vm.cluster_id,
      hint: !live
        ? demoHint
        : vm.drs_override
          ? "DRS override already applied"
          : !vm.cluster_id
            ? "VM is not in a cluster"
            : "Locks this VM on its current host so DRS will not auto-migrate it",
      onClick: () => run(() => onDrsOverride(vm)),
    },
    {
      id: "destroy",
      label: "Delete from disk",
      danger: true,
      disabled: !live,
      hint: !live ? demoHint : "Permanently deletes the VM and its disks",
      onClick: () => run(() => onAction(vm, "destroy")),
    },
  ];

  function renderGroup(items: MenuAction[]) {
    return items.map((item) => (
      <button
        key={item.id}
        type="button"
        role="menuitem"
        className={`row-menu-item${item.danger ? " danger" : ""}`}
        disabled={item.disabled}
        title={item.hint}
        onClick={item.onClick}
      >
        {item.label}
      </button>
    ));
  }

  const menu =
    open && pos
      ? createPortal(
          <div
            ref={menuRef}
            className="row-menu-popover"
            role="menu"
            style={{ top: pos.top, left: pos.left, maxHeight: pos.maxHeight }}
          >
            {renderGroup(consoleItems)}
            <div className="row-menu-sep" role="separator" />
            {renderGroup(powerItems)}
            <div className="row-menu-sep" role="separator" />
            {renderGroup(manageItems)}
          </div>,
          document.body,
        )
      : null;

  return (
    <div className="row-actions" ref={wrapRef}>
      <button
        ref={btnRef}
        type="button"
        className={`row-menu-btn${open ? " active" : ""}`}
        aria-haspopup="menu"
        aria-label={`Actions for ${vm.name}`}
        aria-expanded={open}
        onClick={() =>
          setOpen((value) => {
            if (value) return false;
            if (btnRef.current) setPos(menuPosition(btnRef.current, null));
            return true;
          })
        }
      >
        Actions ▾
      </button>
      {menu}
    </div>
  );
}
