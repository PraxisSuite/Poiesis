#!/usr/bin/env python3
"""
Proxmox BIG-IP Deploy
=====================
Deploy F5 BIG-IP appliances on Proxmox from a licensed .qcow2 image staged in
appliance-images/. Unlike deploy_vm.py, this pipeline:

  - Skips cloud-init entirely (BIG-IP self-configures via console / iControl REST)
  - Skips Ansible post-deploy
  - Skips DNS registration (management IP not known until first boot)
  - Skips Ansible inventory update
  - Pins machine type to pc-i440fx-8.0 (F5 hard requirement on Proxmox)
  - Defaults NIC model to vmxnet3 (F5's traditional pNIC driver)
  - Supports multi-NIC: net0 is always management, additional NICs are data plane

Currently file-driven only (--deploy-file required). Build a deployment JSON by
copying deployments/bigip/example-bigip-deployment.json and editing.

Requirements:
  pip install -r requirements.txt
  Licensed BIG-IP .qcow2 (or .qcow2.zip — auto-extracted) in appliance-images/
  SSH key authorized on all Proxmox nodes (root@proxmoxXX)
"""

# Auto-activate virtualenv so `python3 deploy_bigip.py` works without sourcing .venv
import os, sys
_venv = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "bin", "python3")
if os.path.exists(_venv) and os.path.realpath(sys.executable) != os.path.realpath(_venv):
    os.execv(_venv, [_venv] + sys.argv)

# ── Deployment log tee ────────────────────────────────────────────────────────
# Mirror the pattern from deploy_vm.py so logs/last-deployment.log captures the run.
import re as _re, pathlib as _pathlib, datetime as _dt
_SKIP_LOG = {"--validate", "--dry-run", "--preflight", "--help", "--?"}
if not any(a in sys.argv for a in _SKIP_LOG):
    _ANSI = _re.compile(r'\x1b(?:\[[0-9;?]*[a-zA-Z]|\][^\x07]*\x07|.)')
    _CR   = _re.compile(r'\r(?!\n)')
    def _clean(s):
        return _CR.sub('', _ANSI.sub('', s))
    class _TeeIO:
        def __init__(self, stream, path):
            self._stream = stream
            self._file   = open(path, "w")
            self._file.write(
                f"Poiesis BIG-IP Deploy — {_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Command: {' '.join(sys.argv)}\n\n"
            )
        def write(self, data):
            self._stream.write(data)
            if not self._file.closed:
                self._file.write(_clean(data))
        def flush(self):
            self._stream.flush()
            if not self._file.closed:
                self._file.flush()
        def isatty(self):   return self._stream.isatty()
        def fileno(self):   return self._stream.fileno()
    _log_dir = _pathlib.Path(__file__).parent / "logs"
    _log_dir.mkdir(exist_ok=True)
    _deploy_log_path = _log_dir / "last-deployment.log"
    sys.stdout = _TeeIO(sys.stdout, _deploy_log_path)
# ─────────────────────────────────────────────────────────────────────────────

import argparse
import textwrap
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from modules.lib import (
    load_config,
    connect_proxmox,
    validate_bigip_deployment,
    check_bridges_exist,
    load_deployment_file,
    write_deployment_file,
    get_next_vmid,
    create_vm,
    start_vm,
    parse_ttl,
    expires_at_from_ttl,
    write_history,
    apply_tag_colors,
    resolve_qcow_for_deploy,
    import_qcow_to_node,
    attach_bigip_disk_and_nics,
    firstboot_configure,
    find_appliance_images,
    APPLIANCE_DIR,
    run_ansible_add_dns,
    run_ansible_inventory_update_bigip,
)

console = Console()

# F5 hard requirement on Proxmox. Override at your own risk.
BIGIP_MACHINE_TYPE = "pc-i440fx-8.0"
BIGIP_DEFAULT_BIOS = "seabios"
BIGIP_DEFAULT_SCSIHW = "virtio-scsi-pci"
BIGIP_DEFAULT_NIC_MODEL = "vmxnet3"


def _run_validate(args) -> None:
    """Validate config + deployment file and exit."""
    if not args.deploy_file:
        console.print("[red]ERROR: --validate requires --deploy-file[/red]")
        sys.exit(1)
    errors = validate_bigip_deployment(Path(args.deploy_file))
    if errors:
        console.print(f"[red]✗ {args.deploy_file} — invalid[/red]")
        for e in errors:
            console.print(f"  [red]→ {e}[/red]")
        sys.exit(1)
    console.print(f"[green]✓ {args.deploy_file} is valid[/green]")
    sys.exit(0)


def _save_bigip_deployment_file(d: dict, vmid: int, cfg: dict, ttl: str = "") -> Path:
    """Write/update the bigip deployment JSON with the assigned vmid + deployed_at."""
    out = dict(d)
    out["type"] = "bigip"
    out["vmid"] = vmid
    out["deployed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if ttl:
        out["ttl"] = ttl
        out["expires_at"] = expires_at_from_ttl(ttl)
    domain = cfg["proxmox"].get("node_domain", "")
    if domain and not out.get("fqdn"):
        out["fqdn"] = f"{out['hostname']}.{domain}"
    return write_deployment_file(out, out["hostname"], "bigip", cfg)


def _print_summary(d: dict, machine: str, bios: str, scsihw: str,
                   nic_default_model: str, qcow_path: Path) -> None:
    nics = d["nics"]
    nic_lines = []
    for i, n in enumerate(nics):
        model = n.get("model") or nic_default_model
        vlan  = f" tag={n['vlan']}" if n.get("vlan") is not None else " (untagged)"
        role  = " ← management" if i == 0 else ""
        nic_lines.append(f"net{i}: {model} on {n['bridge']}{vlan}{role}")

    tbl = Table(title="BIG-IP Deployment Summary", show_header=False, border_style="dim")
    tbl.add_column("Field", style="dim")
    tbl.add_column("Value")
    tbl.add_row("Hostname", d["hostname"])
    tbl.add_row("Node",     d["node"])
    tbl.add_row("Image",    qcow_path.name)
    tbl.add_row("Machine",  f"{machine} / {bios} / {scsihw}")
    tbl.add_row("vCPUs",    str(d["cpus"]))
    tbl.add_row("Memory",   f"{d['memory_gb']} GB ({int(float(d['memory_gb']) * 1024)} MB)")
    tbl.add_row("Disk",     f"{d['disk_gb']} GB → {d['storage']} (scsi0)")
    tbl.add_row("NICs",     "\n".join(nic_lines))
    console.print(tbl)


def main() -> None:
    if "--?" in sys.argv:
        sys.argv[sys.argv.index("--?")] = "--help"
    parser = argparse.ArgumentParser(
        prog="deploy_bigip.py",
        description="Proxmox BIG-IP Deploy — provision F5 BIG-IP appliances from a qcow image",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python3 deploy_bigip.py --deploy-file deployments/bigip/bigip-lab01.json
              python3 deploy_bigip.py --validate --deploy-file deployments/bigip/bigip-lab01.json
              python3 deploy_bigip.py --deploy-file ... --ttl 30d

            Stage the qcow in appliance-images/ first. BIGIP*.qcow2.zip files are
            auto-extracted before deploy.
        """),
        add_help=False,
    )
    parser.add_argument("--help", action="help", default=argparse.SUPPRESS,
                        help="show this help message and exit")
    parser.add_argument("--deploy-file", metavar="FILE", required=True,
                        help="JSON deployment file (required — see deployments/bigip/example-*.json)")
    parser.add_argument("--validate", action="store_true",
                        help="Validate the deployment file and exit")
    parser.add_argument("--silent", action="store_true",
                        help="Non-interactive mode (no-op — BIG-IP deploys are always file-driven)")
    parser.add_argument("--ttl", metavar="TTL",
                        help="Time-to-live (e.g. 7d, 24h, 2w, 30m) — stored as expires_at")
    parser.add_argument("--config", metavar="FILE",
                        help="Path to an alternate config file (default: config.yaml in project root)")

    args = parser.parse_args()

    if args.validate:
        _run_validate(args)

    ttl = None
    if args.ttl:
        try:
            parse_ttl(args.ttl)
            ttl = args.ttl
        except ValueError as e:
            console.print(f"[red]ERROR: {e}[/red]")
            sys.exit(1)

    errors = validate_bigip_deployment(Path(args.deploy_file))
    if errors:
        console.print(f"[red]✗ Invalid deployment file: {args.deploy_file}[/red]")
        for e in errors:
            console.print(f"  [red]→ {e}[/red]")
        sys.exit(1)

    d = load_deployment_file(args.deploy_file)
    cfg = load_config(args.config)

    console.print()
    console.print(Panel.fit(
        Text("Proxmox BIG-IP Deploy\ngithub.com: PraxisSuite/Poiesis", style="bold"),
        border_style="cyan",
    ))
    console.print(f"\n[dim]Silent mode — deploying from: {args.deploy_file}[/dim]\n")

    # Resolve the qcow (auto-extract zip if needed)
    try:
        qcow_path = resolve_qcow_for_deploy(d["qcow_filename"])
    except RuntimeError as e:
        console.print(f"[red]✗ {e}[/red]")
        available = find_appliance_images()
        if available:
            console.print("[dim]Available in appliance-images/:[/dim]")
            for p in available:
                console.print(f"  [dim]- {p.name}[/dim]")
        else:
            console.print(f"[dim]No BIGIP*.qcow2 / .qcow2.zip files found in {APPLIANCE_DIR}[/dim]")
        sys.exit(1)

    # If the resolved filename differs from what the deployment file referenced (e.g.
    # the zip extracted to a different stem), record the actual name so future redeploys
    # from this file find it directly.
    d["qcow_filename"] = qcow_path.name

    machine = d.get("machine_type") or BIGIP_MACHINE_TYPE
    bios    = d.get("bios")         or BIGIP_DEFAULT_BIOS
    scsihw  = d.get("scsi_controller") or BIGIP_DEFAULT_SCSIHW
    nic_default_model = BIGIP_DEFAULT_NIC_MODEL

    if machine != BIGIP_MACHINE_TYPE:
        console.print(
            f"[yellow]⚠ Non-standard machine_type '{machine}' — "
            f"BIG-IP on Proxmox normally requires '{BIGIP_MACHINE_TYPE}'.[/yellow]"
        )

    _print_summary(d, machine, bios, scsihw, nic_default_model, qcow_path)

    proxmox = connect_proxmox(cfg)

    # Preflight: every NIC's bridge must exist on the target node — fail fast before the
    # 8 GB upload starts. (qm importdisk would have wasted the upload otherwise.)
    requested_bridges = sorted({n["bridge"] for n in d["nics"]})
    missing = check_bridges_exist(proxmox, d["node"], requested_bridges)
    if missing:
        console.print(f"[red]✗ Bridge(s) missing on {d['node']}: {', '.join(missing)}[/red]")
        console.print(
            f"[dim]The deploy needs: {', '.join(requested_bridges)}.\n"
            f"Either pick a node that has them, or update the nics[] array in "
            f"{args.deploy_file} to use bridges that exist on {d['node']}.[/dim]"
        )
        sys.exit(1)
    console.print(f"  [green]✓ Bridges verified on {d['node']}: {', '.join(requested_bridges)}[/green]")

    next_vmid = d.get("vmid") or get_next_vmid(proxmox)

    # Build NIC config strings (net0..netN)
    nic_kwargs = {}
    for i, n in enumerate(d["nics"]):
        model = n.get("model") or nic_default_model
        parts = [f"{model}", f"bridge={n['bridge']}"]
        if n.get("vlan") is not None:
            parts.append(f"tag={n['vlan']}")
        parts.append("firewall=0")
        nic_kwargs[f"net{i}"] = f"{parts[0]},{','.join(parts[1:])}"

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    description = textwrap.dedent(f"""\
        Auto-deployed by deploy_bigip.py
        ─────────────────────────────────────
        Created    : {now_str}
        Node       : {d['node']}
        Image      : appliance-images/{qcow_path.name}
        Machine    : {machine} / {bios} / {scsihw}
        vCPUs      : {d['cpus']}
        Memory     : {d['memory_gb']} GB
        Disk       : {d['disk_gb']} GB ({d['storage']}) scsi0
        NICs       : {len(d['nics'])} (net0 = management)
        ─────────────────────────────────────
        BIG-IP self-configures: connect to net0 IP after first boot.
    """)

    create_params = {
        "vmid":        next_vmid,
        "name":        d["hostname"],
        "cores":       int(d["cpus"]),
        "sockets":     1,
        "memory":      int(float(d["memory_gb"]) * 1024),
        "machine":     machine,
        "bios":        bios,
        "scsihw":      scsihw,
        "serial0":     "socket",
        "onboot":      1,
        "tags":        ";".join(["auto-deploy", "bigip"]),
        "description": description,
        **nic_kwargs,
    }

    console.print()
    console.print("[bold green]─── Step 1/7: Creating VM ───[/bold green]")
    try:
        next_vmid = create_vm(proxmox, d["node"], create_params)
    except Exception as e:
        console.print(f"[red]✗ VM creation failed: {e}[/red]")
        sys.exit(1)

    # Apply tag color for 'bigip' (non-fatal)
    try:
        apply_tag_colors(proxmox, {"bigip": ("000000", "FF6600")})
    except Exception:
        pass

    console.print()
    console.print("[bold green]─── Step 2/7: Uploading qcow and importing as scsi0 ───[/bold green]")
    try:
        import_qcow_to_node(cfg, proxmox, d["node"], next_vmid, qcow_path, d["storage"])
    except Exception as e:
        console.print(f"[red]✗ qcow import failed: {e}[/red]")
        sys.exit(1)

    console.print()
    console.print("[bold green]─── Step 3/7: Attaching disk and configuring NICs ───[/bold green]")
    try:
        attach_bigip_disk_and_nics(
            proxmox, d["node"], next_vmid,
            disk_storage=d["storage"], disk_gb=int(d["disk_gb"]),
            nics=d["nics"], default_nic_model=nic_default_model,
        )
    except Exception as e:
        console.print(f"[red]✗ Disk/NIC config failed: {e}[/red]")
        sys.exit(1)

    console.print()
    console.print("[bold green]─── Step 4/7: Starting VM ───[/bold green]")
    try:
        start_vm(proxmox, d["node"], next_vmid)
    except Exception as e:
        console.print(f"[red]✗ VM start failed: {e}[/red]")
        sys.exit(1)

    # Persist the deployment file with the actual vmid BEFORE first-boot so that if
    # the (slow, network-dependent) license activation fails, the user can still
    # cleanly decomm via decomm_bigip.py.
    saved_path = _save_bigip_deployment_file(d, next_vmid, cfg, ttl=ttl or "")

    console.print()
    console.print("[bold green]─── Step 5/7: First-boot configuration (password + mgmt IP + license) ───[/bold green]")
    # DNS/NTP defaults come from config.yaml if present, else hardcoded sensible defaults.
    cfg_defaults = cfg.get("defaults", {})
    cfg_ntp = cfg.get("ntp", {})
    raw_dns = d.get("mgmt_dns") or cfg_defaults.get("nameserver", "8.8.8.8 1.1.1.1")
    dns_servers = raw_dns.split() if isinstance(raw_dns, str) else list(raw_dns)
    ntp_servers = list(cfg_ntp.get("servers", ["pool.ntp.org"]))

    try:
        firstboot_configure(
            cfg, proxmox, d["node"], next_vmid,
            new_password=d["password"],
            mgmt_ip=d["mgmt_ip"],
            mgmt_prefix_len=int(d["mgmt_prefix_len"]),
            mgmt_gateway=d.get("mgmt_gateway", ""),
            registration_key=d["registration_key"],
            dns_servers=dns_servers,
            ntp_servers=ntp_servers,
        )
    except Exception as e:
        console.print()
        console.print(Panel.fit(
            Text(
                "First-boot configuration FAILED.\n\n"
                f"The VM (VMID {next_vmid} on {d['node']}) is still running.\n"
                "It was NOT auto-decommissioned.\n\n"
                "Investigate the failure manually:\n"
                f"  • Console: open VM {next_vmid} in the Proxmox UI and check serial output\n"
                f"  • Re-attach:  ssh root@{d['node']}{'.' + cfg['proxmox'].get('node_domain', '') if cfg['proxmox'].get('node_domain') else ''} 'qm terminal {next_vmid}'\n"
                "  • Common causes:\n"
                "      - BIG-IP image does not emit on ttyS0 (try qm sendkey or VGA console)\n"
                "      - License activation rejected (bad/used registration key)\n"
                "      - No outbound network from mgmt IP to activate.f5.com\n\n"
                f"Deployment file: {saved_path}\n"
                "Once the issue is fixed, you can either finish setup manually or run\n"
                "decomm_bigip.py against the deployment file to clean it up.\n\n"
                f"Error: {e}",
                style="red",
            ),
            title="✗ Deploy halted at first-boot",
            border_style="red",
        ))
        sys.exit(1)

    # ── Step 6/7: DNS registration ──────────────────────────────────────────────
    # Non-fatal: a DNS failure is annoying but doesn't break the BIG-IP. We warn and continue.
    console.print()
    console.print("[bold green]─── Step 6/7: Registering DNS records ───[/bold green]")
    try:
        run_ansible_add_dns(cfg, d["hostname"], d["mgmt_ip"])
    except Exception as e:
        console.print(f"[yellow]⚠ DNS registration failed: {e}[/yellow]")
        console.print(f"[dim]Add manually: {d['fqdn']} A {d['mgmt_ip']}[/dim]")

    # ── Step 7/7: Ansible inventory ────────────────────────────────────────────
    # Non-fatal: same reasoning as DNS.
    console.print()
    console.print("[bold green]─── Step 7/7: Updating Ansible inventory ───[/bold green]")
    try:
        run_ansible_inventory_update_bigip(
            cfg,
            hostname=d["hostname"],
            fqdn=d.get("fqdn") or f"{d['hostname']}.{cfg['proxmox'].get('node_domain', '')}",
            license_key=d["registration_key"],
            admin_user="admin",
            admin_password=d["password"],
            group="BIGIPs",
        )
    except Exception as e:
        console.print(f"[yellow]⚠ Inventory update failed: {e}[/yellow]")

    try:
        write_history({
            "event":     "deploy",
            "kind":      "bigip",
            "hostname":  d["hostname"],
            "vmid":      next_vmid,
            "node":      d["node"],
            "timestamp": datetime.now().isoformat(),
        })
    except Exception:
        pass

    console.print()
    console.print(Panel.fit(
        Text(
            "Deployment Complete!\n\n"
            f"Hostname   :  {d['hostname']}\n"
            f"VMID       :  {next_vmid}  (on {d['node']})\n"
            f"NICs       :  {len(d['nics'])} (net0 = management)\n"
            f"Mgmt URL   :  https://{d['mgmt_ip']}/   (admin / your password)\n"
            f"SSH        :  ssh root@{d['mgmt_ip']}\n\n"
            "Password changed, mgmt IP configured, license activated.\n"
            "DNS A/PTR records registered. Added to Ansible inventory [BIGIPs].\n"
            f"\nDeployment file: {saved_path}",
            style="green",
        ),
        title="✓ All Done",
        border_style="green",
    ))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Aborted.[/yellow]")
        sys.exit(0)
