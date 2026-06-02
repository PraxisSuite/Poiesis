"""
Poiesis.bigip — BIG-IP appliance deployment helpers.

BIG-IP (F5 TMOS) deploys differ from generic cloud-init VMs:
  - Image is a licensed .qcow2 (or .qcow2.zip) staged locally in appliance-images/
  - No cloud-init: the appliance self-configures via console / iControl REST / DO/AS3
  - Multi-NIC by default; first NIC is always management
  - Machine type is pinned to pc-i440fx-8.0 (hard F5 requirement on Proxmox)
  - NIC model defaults to vmxnet3 (F5's traditional pNIC driver from the VMware era)
"""

import os
import zipfile
from pathlib import Path

import paramiko
from proxmoxer import ProxmoxAPI
from rich.console import Console

from modules.proxmox import node_ssh_host, run_ssh_cmd

console = Console()

_ROOT = Path(__file__).parent.parent
APPLIANCE_DIR = _ROOT / "appliance-images"


def find_appliance_images(prefix: str = "BIGIP") -> list[Path]:
    """List BIGIP*.qcow2 and BIGIP*.qcow2.zip files in appliance-images/, sorted by name."""
    if not APPLIANCE_DIR.is_dir():
        return []
    qcows = sorted(APPLIANCE_DIR.glob(f"{prefix}*.qcow2"))
    zips  = sorted(APPLIANCE_DIR.glob(f"{prefix}*.qcow2.zip"))
    return qcows + zips


def extract_qcow_zip(zip_path: Path) -> Path:
    """Extract a .qcow2.zip into appliance-images/, delete the zip on success, return the .qcow2 path.

    Raises RuntimeError if the zip doesn't contain a single .qcow2 or extraction fails.
    """
    console.print(f"  [dim]Extracting {zip_path.name}...[/dim]")
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            qcow_members = [n for n in zf.namelist() if n.lower().endswith(".qcow2")]
            if len(qcow_members) != 1:
                raise RuntimeError(
                    f"Expected exactly one .qcow2 in {zip_path.name}, found {len(qcow_members)}"
                )
            member = qcow_members[0]
            zf.extract(member, APPLIANCE_DIR)
            extracted = APPLIANCE_DIR / member
            if not extracted.exists():
                raise RuntimeError(f"Extraction reported success but {extracted} is missing")
    except zipfile.BadZipFile as e:
        raise RuntimeError(f"Bad zip file {zip_path.name}: {e}") from e

    console.print(f"  [green]✓ Extracted to {extracted.name}[/green]")
    try:
        zip_path.unlink()
        console.print(f"  [dim]Deleted {zip_path.name}[/dim]")
    except OSError as e:
        console.print(f"  [yellow]⚠ Could not delete zip: {e}[/yellow]")
    return extracted


def resolve_qcow_for_deploy(qcow_filename: str) -> Path:
    """Find the named qcow in appliance-images/. If a .qcow2.zip with that name is present
    instead, auto-extract it (deleting the zip on success). Returns the resolved .qcow2 path.

    qcow_filename can be the bare .qcow2 name or the .qcow2.zip name — either resolves to
    the extracted .qcow2 path.
    """
    if not APPLIANCE_DIR.is_dir():
        raise RuntimeError(f"appliance-images/ directory not found at {APPLIANCE_DIR}")

    direct = APPLIANCE_DIR / qcow_filename
    if direct.exists() and qcow_filename.endswith(".qcow2"):
        return direct

    # If they referenced the .zip directly, or the qcow doesn't exist but a matching zip does
    zip_name = qcow_filename if qcow_filename.endswith(".zip") else f"{qcow_filename}.zip"
    zip_path = APPLIANCE_DIR / zip_name
    if zip_path.exists():
        return extract_qcow_zip(zip_path)

    if direct.exists():
        return direct

    raise RuntimeError(
        f"Could not resolve qcow '{qcow_filename}' in {APPLIANCE_DIR}. "
        f"Checked direct file and {zip_name}."
    )


def import_qcow_to_node(cfg: dict, proxmox: ProxmoxAPI, node_name: str, vmid: int,
                        local_qcow_path: Path, disk_storage: str) -> None:
    """Upload the local qcow to the Proxmox node via SFTP, run qm importdisk, then remove the
    temp upload.  After import the disk appears as unused0 in the VM config."""
    pve = cfg["proxmox"]
    ssh_host = node_ssh_host(cfg, node_name)
    ssh_key  = os.path.expanduser(pve.get("ssh_key", "~/.ssh/id_rsa"))

    remote_path = f"/tmp/poiesis-bigip-{vmid}-{local_qcow_path.name}"

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(ssh_host, username="root", key_filename=ssh_key, timeout=30)
    except paramiko.AuthenticationException as e:
        raise RuntimeError(
            f"SSH key auth to {ssh_host} failed. "
            f"Ensure {ssh_key} is authorized on the node."
        ) from e

    try:
        size_bytes = local_qcow_path.stat().st_size
        size_mb    = size_bytes / (1024 * 1024)
        console.print(
            f"  [dim]Uploading {local_qcow_path.name} ({size_mb:.0f} MB) "
            f"to {node_name}:{remote_path}...[/dim]"
        )
        with console.status(f"[bold green]Uploading qcow to {node_name}..."):
            sftp = ssh.open_sftp()
            try:
                sftp.put(str(local_qcow_path), remote_path)
            finally:
                sftp.close()
        console.print(f"  [green]✓ Uploaded[/green]")

        console.print(f"  [dim]Importing disk into VM {vmid} on storage '{disk_storage}'...[/dim]")
        exit_code, out, err = run_ssh_cmd(ssh, f"qm importdisk {vmid} {remote_path} {disk_storage}")
        if exit_code != 0:
            raise RuntimeError(f"qm importdisk failed (exit {exit_code}): {err or out}")
        console.print(f"  [green]✓ Disk imported[/green]")
    finally:
        # Best-effort cleanup of the staged upload — don't fail the deploy if it lingers
        try:
            run_ssh_cmd(ssh, f"rm -f {remote_path}")
        except Exception:
            pass
        ssh.close()


def build_nic_config(nic: dict, default_model: str = "vmxnet3",
                     default_firewall: bool = False) -> str:
    """Render a single nics[] entry as a Proxmox netN config string.

    nic: {"bridge": "vmbr0", "vlan": 100, "model": "vmxnet3"}
         vlan is optional (omit/null = untagged); model defaults to vmxnet3.
    """
    model    = nic.get("model") or default_model
    bridge   = nic["bridge"]
    parts    = [f"{model}", f"bridge={bridge}"]
    vlan     = nic.get("vlan")
    if vlan is not None:
        parts.append(f"tag={vlan}")
    parts.append(f"firewall={1 if default_firewall else 0}")
    # First part doesn't use a "key=" — it's just the model
    return f"{parts[0]},{','.join(parts[1:])}"


def attach_bigip_disk_and_nics(proxmox: ProxmoxAPI, node_name: str, vmid: int,
                                disk_storage: str, disk_gb: int,
                                nics: list[dict],
                                default_nic_model: str = "vmxnet3") -> None:
    """Attach the imported disk as scsi0, configure all NICs, set boot order, resize disk.

    Unlike configure_vm_disk_and_cloudinit (the cloud-init VM equivalent), this does NOT
    add an ide2 cloud-init drive — BIG-IP has its own first-boot mechanism.
    """
    vm_config = proxmox.nodes(node_name).qemu(vmid).config.get()
    unused_disk = None
    for key in sorted(vm_config.keys()):
        if key.startswith("unused"):
            unused_disk = vm_config[key]
            break
    if not unused_disk:
        raise RuntimeError("Imported disk not found in VM config (no unused0 key)")

    proxmox.nodes(node_name).qemu(vmid).config.put(scsi0=unused_disk)
    console.print(f"  [dim]Attached {unused_disk} as scsi0[/dim]")

    proxmox.nodes(node_name).qemu(vmid).config.put(boot="order=scsi0")

    proxmox.nodes(node_name).qemu(vmid).resize.put(disk="scsi0", size=f"{int(disk_gb)}G")
    console.print(f"  [dim]Resized scsi0 to {disk_gb} GB[/dim]")

    for i, nic in enumerate(nics):
        net_str = build_nic_config(nic, default_model=default_nic_model)
        proxmox.nodes(node_name).qemu(vmid).config.put(**{f"net{i}": net_str})
        vlan_str = f" tag={nic.get('vlan')}" if nic.get("vlan") is not None else " (untagged)"
        role     = " (management)" if i == 0 else ""
        console.print(f"  [dim]net{i}: {nic['bridge']}{vlan_str}{role}[/dim]")
