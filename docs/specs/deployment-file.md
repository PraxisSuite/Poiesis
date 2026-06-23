[← Back to README](../../README.md)

# Deployment Files — Schema Reference

### About

Deployment files are JSON records written to `deployments/lxc/` (LXC containers) or
`deployments/vms/` (QEMU VMs) when a resource is successfully deployed. They serve as the
authoritative record of what was deployed, where, and how — and are the primary input for
decommission, expiry, renewal, and re-deploy operations.

**Why they matter:** Without a deployment file, Poiesis has no record of a resource.
Decommission scripts read the file to know which node to contact, what VMID to destroy,
which IP to clean from DNS, and which hostname to remove from inventory. `expire.py` scans
these files for `expires_at` to manage TTLs. Pre-building a deployment file and passing it
with `--deploy-file` allows silent/automated deployments.

---

## File Naming and Location

```
deployments/
├── lxc/
│   └── <hostname>.json        ← one file per LXC container
└── vms/
    └── <hostname>.json        ← one file per VM
```

Files are named after the container/VM hostname (short name, no domain suffix).
All deployment files are excluded from git via `.gitignore`. Example files
(`example-deployment.json`, `example-vm-deployment.json`) are explicitly tracked as
reference.

---

## LXC Deployment File

Written by `deploy_lxc.py`. Read by `decomm_lxc.py`, `expire.py`, and `cleanup_tagged.py`.

### Full Example

```json
{
  "hostname": "my-example-server",
  "fqdn": "my-example-server.lees-family.io",
  "node": "proxmoxb01",
  "vmid": 142,
  "template_volid": "local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst",
  "template_name": "ubuntu-24.04-standard_24.04-2_amd64.tar.zst",
  "cpus": 2,
  "memory_gb": 4.0,
  "disk_gb": 100,
  "storage": "local-lvm",
  "vlan": 220,
  "bridge": "vmbr0",
  "password": "changeme",
  "ip_address": "10.220.220.150",
  "assigned_ip": "10.220.220.150",
  "prefix_len": "24",
  "deployed_at": "2026-03-05 22:45:00",
  "ttl": "7d",
  "expires_at": "2026-03-12T22:45:00.000000+00:00",
  "preflight": true
}
```

### Field Reference

| Field | Required | Auto-populated | Description |
|---|---|---|---|
| `hostname` | ✓ | — | Short hostname (no domain suffix). Used as the resource name in Proxmox, DNS, and inventory. |
| `fqdn` | ✓ | ✓ | Fully qualified domain name. Constructed from `hostname` + domain suffix from config. |
| `node` | ✓ | — | Proxmox node name where the container lives. Must match a node returned by the Proxmox API. |
| `vmid` | ✓ | ✓ | Proxmox VMID assigned at creation time. Auto-assigned by Proxmox; recorded here for all future operations. |
| `template_volid` | ✓ | — | Full Proxmox volume ID of the LXC template. e.g. `local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst`. Used by `deploy_lxc.py` when re-deploying from this file. |
| `template_name` | ✓ | — | Filename portion of the template. Used for display and matching available templates. |
| `cpus` | ✓ | — | Number of vCPUs allocated to the container. |
| `memory_gb` | ✓ | — | RAM allocated in GB. Converted to MB internally for the Proxmox API. |
| `disk_gb` | ✓ | — | Root disk size in GB. |
| `storage` | ✓ | — | Proxmox storage pool for the container root disk. e.g. `local-lvm`, `Net-Images`. |
| `vlan` | ✓ | — | VLAN tag applied to the network interface. |
| `bridge` | ✓ | — | Proxmox bridge the container is attached to. Combined with vlan as `vmbr0.220`. |
| `password` | ✓ | — | Root and secondary user password set at deploy time. Stored here for reference and re-deploy. |
| `ip_address` | ✓ | — | IP address assigned at deploy time. Either a static IP (`10.220.220.150`) or `dhcp`. |
| `assigned_ip` | ✓ | ✓ | The actual IP address the container is reachable on. For static deployments, same as `ip_address`. For DHCP, populated after the DHCP lease is discovered. Used by DNS removal during decommission — this is the authoritative IP for cleanup. |
| `prefix_len` | ✓ | — | Network prefix length (subnet mask bits). e.g. `24` for /24. |
| `deployed_at` | ✓ | ✓ | Timestamp when the deployment completed. Format: `YYYY-MM-DD HH:MM:SS`. |
| `ttl` | optional | — | Time-to-live for this deployment. Accepted formats: `30m`, `24h`, `7d`, `2w`. Set with `--ttl` at deploy time. If present, `expires_at` is also set. Deployments without `ttl` are not tracked by `expire.py`. |
| `expires_at` | optional | ✓ | ISO 8601 UTC timestamp when this deployment expires. Calculated from `deployed_at` + `ttl`. Scanned by `expire.py --check` and `expire.py --reap`. |
| `preflight` | optional | — | Whether to run preflight checks before re-deploying from this file. Default: `true`. Set `false` with `--yolo` or to skip checks for a known-good re-deploy. |

---

## VM Deployment File

Written by `deploy_vm.py`. Read by `decomm_vm.py`, `expire.py`, and `cleanup_tagged.py`.

### Full Example

```json
{
  "type": "vm",
  "hostname": "my-example-vm",
  "fqdn": "my-example-vm.lees-family.io",
  "node": "proxmoxb01",
  "vmid": 200,
  "cloud_image_storage": "local",
  "cloud_image_filename": "noble-server-cloudimg-amd64.img",
  "cloud_image_url": "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
  "image_refresh": false,
  "cpus": 2,
  "memory_gb": 4.0,
  "disk_gb": 100,
  "storage": "local-lvm",
  "vlan": 220,
  "bridge": "vmbr0",
  "password": "changeme",
  "ip_address": "dhcp",
  "assigned_ip": "10.220.220.200",
  "prefix_len": "24",
  "gateway": "10.220.220.1",
  "deployed_at": "2026-03-06 10:00:00",
  "ttl": "1d",
  "expires_at": "2026-03-07T10:00:00.000000+00:00",
  "preflight": true
}
```

### Field Reference

Fields shared with LXC (same meaning): `hostname`, `fqdn`, `node`, `vmid`, `cpus`,
`memory_gb`, `disk_gb`, `storage`, `vlan`, `bridge`, `password`, `ip_address`,
`assigned_ip`, `prefix_len`, `deployed_at`, `ttl`, `expires_at`, `preflight`.

VM-specific fields:

| Field | Required | Auto-populated | Description |
|---|---|---|---|
| `type` | ✓ | ✓ | Always `"vm"`. Distinguishes VM files from LXC files when both are processed together (e.g. by `expire.py`). LXC files do not have this field. |
| `cloud_image_storage` | ✓ | — | Proxmox storage where the cloud image is cached. e.g. `local`. Must support `iso` content type. |
| `cloud_image_filename` | ✓ | — | Filename of the cloud image on the Proxmox node. e.g. `noble-server-cloudimg-amd64.img`. `.qcow2.xz` files (FreeBSD) are auto-decompressed on the Proxmox node before `qm importdisk` — the catalog filename keeps the `.xz` suffix matching the upstream download. When this field starts with `FreeBSD-`, `deploy_vm.py` runs the [serial-console firstboot](../deploy-vm.md#freebsd-deployments-serial-console-firstboot) module between Step 3 and Step 4 to configure network, password, SSH key, and Python (FreeBSD's `BASIC-CLOUDINIT` images don't ship real cloud-init). |
| `cloud_image_url` | ✓ | — | Download URL for the cloud image. Used if `image_refresh: true` or the image is not cached. |
| `image_refresh` | optional | — | If `true`, re-download the cloud image before deploying even if the cached file exists. Default: `false`. |
| `gateway` | optional* | — | Default gateway for static IP deployments. Not set for DHCP deployments. *Required for FreeBSD deployments (which cannot use DHCP — see `cloud_image_filename` note above).* |
| `cpu_type` | optional | — | Per-deployment override for the QEMU `-cpu` model. When absent, `deploy_vm.py` uses `vm.cpu_type` from `config.yaml` (default `x86-64-v2-AES`). **Required for RHEL 10 family cloud images** (Rocky 10, AlmaLinux 10, CentOS Stream 10) — Red Hat raised the baseline microarchitecture for RHEL 10 to `x86-64-v3`, so these images kernel-panic on hosts running anything lower. Common values: `x86-64-v2-AES` (default), `x86-64-v3`, `x86-64-v4`, `host`, or a specific CPU model name (`Haswell`, `Skylake`, …). The preflight `check_node_cpu_baseline` verifies the target node's CPU has every flag the requested type needs and fails fast with a clear error if it doesn't. See [BUG-006 in known-bugs.md](../../known-bugs.md) for the full background. |

---

## BIG-IP Deployment File

Written by `deploy_bigip.py`. Read by `decomm_bigip.py`, `cleanup_tagged.py`.

BIG-IP deploys differ structurally from VMs: no cloud-init, the image is a licensed local
qcow rather than a public download, multi-NIC by default, pinned machine type, automated
first-boot login + password change + management IP + DNS + NTP + license activation via
serial console.

### Full Example

```json
{
  "type": "bigip",
  "hostname": "bigip-example",
  "fqdn": "bigip-example.example.com",
  "node": "proxmox01",
  "vmid": null,
  "qcow_filename": "BIGIP-17.5.0-0.0.13.ALL-scsi.qcow2",
  "machine_type": "pc-i440fx-8.0",
  "bios": "seabios",
  "scsi_controller": "virtio-scsi-pci",
  "cpus": 4,
  "memory_gb": 16.0,
  "disk_gb": 82,
  "storage": "local-lvm",
  "password": "CHANGE_ME_BEFORE_DEPLOY",
  "mgmt_ip": "10.0.0.40",
  "mgmt_prefix_len": 24,
  "mgmt_gateway": "10.0.0.1",
  "mgmt_dns": "10.0.0.2 10.0.0.3",
  "registration_key": "XXXXX-XXXXX-XXXXX-XXXXX-XXXXXXX",
  "nics": [
    {"bridge": "vmbr0", "vlan": 100, "model": "vmxnet3"},
    {"bridge": "vmbr0", "vlan": 200, "model": "vmxnet3"},
    {"bridge": "vmbr0", "vlan": 300, "model": "vmxnet3"},
    {"bridge": "vmbr0", "vlan": 220, "model": "vmxnet3"},
    {"bridge": "vmbr1", "model": "vmxnet3"}
  ],
  "deployed_at": "2026-05-18 11:14:43",
  "ttl": "30d",
  "expires_at": "2026-06-17T11:14:43.000000+00:00",
  "preflight": true
}
```

### Field Reference

Shared with VM/LXC (same meaning): `hostname`, `fqdn`, `node`, `vmid`, `cpus`, `memory_gb`,
`disk_gb`, `storage`, `deployed_at`, `ttl`, `expires_at`, `preflight`.

BIG-IP-specific fields:

| Field | Required | Default | Description |
|---|---|---|---|
| `type` | ✓ | — | Must be `"bigip"`. Distinguishes BIG-IP files from VM/LXC when batch-dispatched. |
| `qcow_filename` | ✓ | — | Filename of the qcow inside `appliance-images/`. Can name a `.qcow2` or `.qcow2.zip` — a zip is auto-extracted before deploy (and the zip deleted on success). Glob is `BIGIP*.qcow2` / `BIGIP*.qcow2.zip`. `--validate` confirms the file (or its matching `.zip`) is actually staged and lists what's available if not. |
| `machine_type` | optional | `pc-i440fx-8.0` | Proxmox machine type. **F5 requires `pc-i440fx-8.0` on Proxmox.** Override at your own risk; a warning is emitted if you pick something else. |
| `bios` | optional | `seabios` | BIOS for the VM. |
| `scsi_controller` | optional | `virtio-scsi-pci` | SCSI controller model. |
| `password` | ✓ | — | The new password to set on the `root` and `admin` users during first-boot. Replaces the BIG-IP factory default of `default`. Stored plaintext like LXC/VM deployments — use a config-management secret in production. |
| `mgmt_ip` | ✓ | — | Static IPv4 address to assign to the BIG-IP management interface (net0). |
| `mgmt_prefix_len` | ✓ | — | Network prefix length for the management interface (e.g. `24`). |
| `mgmt_gateway` | optional | — | Default route gateway for the management interface. Recommended — license activation needs outbound HTTPS. |
| `mgmt_dns` | optional | — | Space-separated list of DNS server IPs, or a JSON array. **If omitted, no DNS is configured on the BIG-IP** and license activation will fail unless DNS is already reachable some other way. Example: `"192.168.1.4 192.168.1.5"`. |
| `registration_key` | ✓ | — | F5 license registration key (5 segments, hyphen-separated). Used at first-boot to activate via `tmsh install sys license`. |
| `nics` | ✓ | — | List of NIC objects (must be non-empty). `nics[0]` is always the management interface. |
| `nics[].bridge` | ✓ | — | Proxmox bridge name (e.g. `vmbr0`, `vmbr1`). Verified to exist on the target node before any disk upload. |
| `nics[].vlan` | optional | none | VLAN tag (integer 1–4094). Omit or set to `null` for an untagged interface on that bridge. |
| `nics[].model` | optional | `vmxnet3` | NIC model. F5 traditionally uses `vmxnet3` (carryover from the VMware era); `virtio` also works. |

### Deploy Pipeline (7 steps)

`deploy_bigip.py` performs the following, in order, after preflight + bridge verification:

1. **Create VM** with the pinned machine type, BIOS, SCSI controller, serial0 console, and
   all NICs from `nics[]`.
2. **Upload qcow** to the target Proxmox node via SFTP, then `qm importdisk` to the chosen
   storage (e.g. `local-lvm`). Temporary upload is cleaned up after import.
3. **Attach disk** as `scsi0`, resize to `disk_gb`, and finalize NIC configuration.
4. **Start VM**, then save the deployment file with the assigned VMID.
5. **First-boot configuration** via the Proxmox node's serial console:
   - Wait for the BIG-IP login prompt (up to 6 min for first boot)
   - Log in as `root` / `default`, change to the configured `password`
   - Set `admin` password to match (via `passwd admin`)
   - Wait for mcpd to be stably running (polls every 60s, requires 2 consecutive healthy
     checks, up to 30 min)
   - Configure management IP, prefix, gateway via `tmsh`
   - Configure DNS + NTP (if provided) via `tmsh`
   - Wait for mcpd stably running again
   - Install + activate license via `tmsh install sys license registration-key`
   - Wait for mcpd stably running after license install (license operations restart mcpd)
   - Verify license is active
6. **Register DNS** A + PTR records via the existing BIND-via-Ansible flow.
7. **Update Ansible inventory** — adds the BIG-IP to the `[BIGIPs]` group on the development
   server using a BIG-IP-specific playbook (`update-bigip-inventory.yml`). No SSH key copy
   is performed (BIG-IPs are managed via iControl REST, not SSH). Entry format:
   ```
   <hostname> ansible_host=<fqdn> ansible_python_interpreter=/usr/bin/python3 \
              license_key=<key> bigip_user=admin bigip_password=<password>
   ```

### Decomm Pipeline (5 steps)

`decomm_bigip.py` performs:

1. **Revoke license** via serial console: attaches, sends Ctrl-C to clear any stuck prompt,
   logs in (if needed using the post-firstboot password from the JSON), waits for mcpd
   stable, sends `tmsh revoke sys license`, answers the `Y/N` confirmation, waits for mcpd
   stable, verifies revoked. **Skipped if `--force-decomm` is passed.**
2. **Remove DNS records** (A + PTR) via the BIND-via-Ansible flow. Non-fatal.
3. **Remove from Ansible inventory.** Non-fatal.
4. **Destroy VM** in Proxmox (stop + delete with purge + destroy unreferenced disks).
5. **Deployment file** — by default kept on disk (use `--purge` to delete).

If revocation fails, the script aborts before destroying the VM so the operator can fix it
manually. Use `--force-decomm` to skip revocation when the VM is hung/unreachable or the
license has already been revoked manually (the F5 key will stay consumed if you bypass
revocation).

### Operational Notes

- **Tags applied in Proxmox:** `auto-deploy;bigip`.
- **F5 hard requirement:** `machine_type` must be `pc-i440fx-8.0` (the default). Other
  machine types will boot but TMOS is not supported on them.
- **Bridge preflight:** every NIC's bridge must exist on the target node — this is verified
  before any 8 GB qcow upload starts.
- **Image auto-extract:** if `qcow_filename` resolves to a `.qcow2.zip` file, Poiesis extracts
  it in place and deletes the zip on success (one-time per image per host).
- **License activation requires DNS + outbound HTTPS** from the management IP to F5's
  activation servers. If you omit `mgmt_dns`, ensure DNS is reachable some other way.
- **Parallel deploys to two different nodes** can occasionally stall in paramiko's SFTP
  layer. If you regularly deploy 2+ at once, use a generous `--stagger` and treat hangs
  as a known issue.

---

## Using Deployment Files as Input (`--deploy-file`)

Both `deploy_lxc.py` and `deploy_vm.py` accept `--deploy-file <path>` to pre-fill prompts
from an existing deployment file. Combined with `--silent`, this enables fully automated
re-deployments with no user interaction.

**Required fields for `--deploy-file`:** `hostname`, `node`, `cpus`, `memory_gb`,
`disk_gb`, `storage`, `vlan`, `bridge`, `password`.

**Optional but recommended:** `ip_address`, `vmid` (if you want the same VMID),
`ttl`, `preflight`.

**Fields that are always auto-populated and should not be hand-crafted:**
`vmid` (if not specified), `fqdn`, `assigned_ip`, `deployed_at`, `expires_at`.

---

## Cleanup Action List File (`--list-file`)

Used by `cleanup_tagged.py --list-file`. A separate format — see
`docs/specs/cleanup-action-list.md`.

---

[← Back to README](../../README.md)
