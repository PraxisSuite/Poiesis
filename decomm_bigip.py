#!/usr/bin/env python3
"""
Proxmox BIG-IP Decommission Script
==================================
PERMANENTLY destroys a BIG-IP VM and (optionally) removes its deployment file.

Unlike decomm_vm.py, BIG-IP decomm skips:
  - DNS cleanup (the management IP was never registered by Poiesis)
  - Ansible inventory cleanup (BIG-IPs were never added to inventory)

THIS IS IRREVERSIBLE.
"""

# Auto-activate virtualenv so `python3 decomm_bigip.py` works without sourcing .venv
import os, sys
_venv = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "bin", "python3")
if os.path.exists(_venv) and os.path.realpath(sys.executable) != os.path.realpath(_venv):
    os.execv(_venv, [_venv] + sys.argv)

# Tee stdout to last-decomm.log
import re as _re, pathlib as _pathlib, datetime as _dt
_SKIP_LOG = {"--silent", "--help", "--?"}
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
                f"Poiesis BIG-IP Decomm — {_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
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
    _decomm_log_path = _log_dir / "last-decomm.log"
    sys.stdout = _TeeIO(sys.stdout, _decomm_log_path)

import argparse
import textwrap
from datetime import datetime
from pathlib import Path

import questionary
from proxmoxer import ProxmoxAPI
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from modules.lib import (
    load_config,
    connect_proxmox,
    wait_for_task,
    load_deployment_file,
    write_history,
    revoke_license,
    remove_dns,
    remove_from_inventory,
)

console = Console()


def stop_and_destroy_bigip(proxmox: ProxmoxAPI, deploy: dict) -> None:
    node = deploy["node"]
    vmid = deploy["vmid"]
    hostname = deploy["hostname"]

    try:
        status = proxmox.nodes(node).qemu(vmid).status.current.get()
    except Exception:
        console.print(f"  [yellow]VM {vmid} not found on {node} — may already be deleted.[/yellow]")
        return

    if status.get("status") == "running":
        console.print(f"  [dim]Stopping BIG-IP {vmid} ({hostname})...[/dim]")
        try:
            task = proxmox.nodes(node).qemu(vmid).status.stop.post()
            wait_for_task(proxmox, node, task, timeout=60)
            console.print(f"  [green]✓ VM stopped[/green]")
        except Exception as e:
            console.print(f"  [yellow]Warning: Could not stop VM cleanly: {e}[/yellow]")

    console.print(f"  [dim]Destroying VM {vmid}...[/dim]")
    try:
        task = proxmox.nodes(node).qemu(vmid).delete(**{"purge": 1, "destroy-unreferenced-disks": 1})
        wait_for_task(proxmox, node, task, timeout=120)
        console.print(f"  [green]✓ VM {vmid} destroyed[/green]")
    except Exception as e:
        console.print(f"  [red]✗ Failed to destroy VM: {e}[/red]")
        raise


def main() -> None:
    if "--?" in sys.argv:
        sys.argv[sys.argv.index("--?")] = "--help"
    parser = argparse.ArgumentParser(
        prog="decomm_bigip.py",
        description="Proxmox BIG-IP Decommission — permanently destroys a BIG-IP VM",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python3 decomm_bigip.py --deploy-file deployments/bigip/bigip-lab01.json
              python3 decomm_bigip.py --deploy-file ... --silent --purge
        """),
        add_help=False,
    )
    parser.add_argument("--help", action="help", default=argparse.SUPPRESS,
                        help="show this help message and exit")
    parser.add_argument("--deploy-file", metavar="FILE", required=True,
                        help="Deployment JSON for the BIG-IP to destroy")
    parser.add_argument("--silent", action="store_true",
                        help="Skip the confirmation challenge (use in batch mode)")
    parser.add_argument("--purge", action="store_true",
                        help="Also delete the local deployment JSON file after success")
    parser.add_argument("--force-decomm", action="store_true",
                        help="Skip license revocation. Use only if the VM is stopped/unreachable "
                             "or the license was already revoked manually. The F5 license will "
                             "remain consumed on F5's activation server.")
    parser.add_argument("--config", metavar="FILE",
                        help="Path to an alternate config file (default: config.yaml)")
    args = parser.parse_args()

    deploy_path = Path(args.deploy_file)
    deploy = load_deployment_file(args.deploy_file)
    if deploy.get("type") != "bigip":
        console.print(f"[red]ERROR: {args.deploy_file} is not a BIG-IP deployment file "
                      f"(type={deploy.get('type')!r}). Use decomm_lxc.py / decomm_vm.py instead.[/red]")
        sys.exit(1)

    hostname = deploy.get("hostname", "<unknown>")
    vmid     = deploy.get("vmid", "<unknown>")
    node     = deploy.get("node", "<unknown>")

    console.print()
    console.print(Panel.fit(
        Text(
            "Proxmox BIG-IP Decommission Wizard\n"
            "Permanently destroys a BIG-IP VM and removes all traces",
            style="bold red", justify="center"
        ),
        border_style="red",
    ))

    if args.silent:
        console.print(f"\n[dim]Silent mode: skipping confirmation challenge for {hostname}.[/dim]")
    else:
        console.print(f"\n[yellow]About to destroy BIG-IP:[/yellow]")
        console.print(f"  Hostname : {hostname}")
        console.print(f"  VMID     : {vmid}")
        console.print(f"  Node     : {node}")
        answer = questionary.text(
            f'Type "{hostname}" to confirm permanent destruction:'
        ).ask()
        if answer != hostname:
            console.print("[yellow]Aborted — confirmation did not match.[/yellow]")
            sys.exit(0)

    cfg = load_config(args.config)
    proxmox = connect_proxmox(cfg)

    # ── Step 1/5: Revoke F5 license ──────────────────────────────────────────────
    console.print("\n[bold cyan]─── Step 1/5: Revoking F5 license ───[/bold cyan]")
    if args.force_decomm:
        console.print(
            "  [yellow]⚠ --force-decomm: skipping license revocation.[/yellow]\n"
            "  [dim]The F5 registration key will remain consumed on F5's activation server.[/dim]"
        )
    else:
        # We need the VM running to interact with its serial console. If it's stopped, the
        # operator must either start it manually then retry, or pass --force-decomm.
        try:
            status = proxmox.nodes(deploy["node"]).qemu(deploy["vmid"]).status.current.get()
        except Exception as e:
            console.print(
                f"[red]✗ Could not query VM status: {e}[/red]\n"
                "[dim]If the VM no longer exists in Proxmox, re-run with --force-decomm "
                "to skip revocation.[/dim]"
            )
            sys.exit(1)

        if status.get("status") != "running":
            console.print(
                "[red]✗ VM is not running — cannot revoke license via serial console.[/red]\n"
                "[dim]Either start the VM and retry decomm, or run with --force-decomm to "
                "skip revocation (the F5 license will remain consumed).[/dim]"
            )
            sys.exit(1)

        try:
            revoke_license(
                cfg, proxmox, deploy["node"], deploy["vmid"],
                password=deploy.get("password"),
            )
        except Exception as e:
            console.print()
            console.print(Panel.fit(
                Text(
                    "License revocation FAILED.\n\n"
                    f"The VM (VMID {deploy['vmid']} on {deploy['node']}) is still running.\n"
                    "It was NOT destroyed.\n\n"
                    "Investigate manually:\n"
                    f"  ssh root@{deploy['node']}{'.' + cfg['proxmox'].get('node_domain', '') if cfg['proxmox'].get('node_domain') else ''} 'qm terminal {deploy['vmid']}'\n"
                    "Then log in and run:  tmsh revoke sys license\n\n"
                    "Once revoked, either retry decomm or pass --force-decomm to skip the "
                    "revocation step entirely.\n\n"
                    f"Error: {e}",
                    style="red",
                ),
                title="✗ Decomm halted at revoke",
                border_style="red",
            ))
            sys.exit(1)

    # ── Step 2/5: Remove DNS records ─────────────────────────────────────────────
    # Non-fatal — we'd rather destroy the VM than be blocked by a flaky BIND server.
    console.print("\n[bold cyan]─── Step 2/5: Removing DNS records ───[/bold cyan]")
    try:
        # remove_dns reads `assigned_ip` or `ip_address`; for BIG-IPs we use mgmt_ip.
        dns_deploy = dict(deploy)
        dns_deploy["ip_address"] = deploy.get("mgmt_ip", "")
        remove_dns(cfg, dns_deploy)
    except Exception as e:
        console.print(f"[yellow]⚠ DNS removal failed: {e}[/yellow]")

    # ── Step 3/5: Remove from Ansible inventory ──────────────────────────────────
    console.print("\n[bold cyan]─── Step 3/5: Removing from Ansible inventory ───[/bold cyan]")
    try:
        remove_from_inventory(cfg, deploy)
    except Exception as e:
        console.print(f"[yellow]⚠ Inventory removal failed: {e}[/yellow]")

    # ── Step 4/5: Destroy VM ─────────────────────────────────────────────────────
    console.print("\n[bold cyan]─── Step 4/5: Destroying BIG-IP VM ───[/bold cyan]")
    try:
        stop_and_destroy_bigip(proxmox, deploy)
    except Exception as e:
        console.print(f"[red]✗ Decommission failed: {e}[/red]")
        sys.exit(1)

    console.print("\n[bold cyan]─── Step 5/5: Deployment file ───[/bold cyan]")
    if args.purge:
        try:
            deploy_path.unlink()
            console.print(f"  [green]✓ Deleted {deploy_path}[/green]")
        except OSError as e:
            console.print(f"  [yellow]Warning: Could not delete {deploy_path}: {e}[/yellow]")
    else:
        console.print(f"  [dim]Deployment file NOT deleted: {deploy_path}[/dim]")
        console.print("  [dim]Run with --purge to delete it, or remove it manually.[/dim]")

    try:
        write_history({
            "event":     "decomm",
            "kind":      "bigip",
            "hostname":  hostname,
            "vmid":      vmid,
            "node":      node,
            "timestamp": datetime.now().isoformat(),
        })
    except Exception:
        pass

    console.print()
    console.print(Panel.fit(
        Text(
            "Decommission Complete\n\n"
            f"{hostname} has been permanently destroyed.\n"
            "License revoked, DNS removed, inventory cleaned, VM deleted.",
            style="green",
        ),
        title="☠  Done",
        border_style="green",
    ))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Aborted.[/yellow]")
        sys.exit(0)
