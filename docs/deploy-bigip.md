[← Back to README](../README.md)

# BIG-IP deployment (via `deploy.py` / `decomm.py`)

> **Entry points are `deploy.py` and `decomm.py`.** They auto-dispatch by the JSON's
> `type` field, so a single BIG-IP deploys with
> `python3 deploy.py --deploy-file deployments/bigip/<host>.json` and decomms with the
> matching `decomm.py` call.
>
> **`deploy_bigip.py` and `decomm_bigip.py` are helper scripts — the implementation that
> `deploy.py` / `decomm.py` call under the hood.** Don't call them directly unless you
> have a specific reason; the dispatcher form supports batch, parallel, `--ttl`, and is
> what every other Poiesis doc and example uses. The two forms are functionally
> identical for BIG-IP (no interactive mode exists), so there's no feature you gain by
> bypassing the dispatcher — just a smaller blast radius if the project changes how
> dispatching works.

Provisions an F5 BIG-IP appliance VM on Proxmox from a licensed `.qcow2` image staged in
`appliance-images/`, then drives first-boot all the way to a fully licensed, network-
configured, DNS-registered, inventory-tracked appliance.

BIG-IP deploys are **file-driven only** — there is no interactive wizard, because the
deployment shape (multi-NIC, pinned machine type, no cloud-init, F5-specific first-boot
sequence) doesn't fit the LXC/VM wizard pattern.

---

## What it does (7 steps)

1. **Resolve the qcow** — globs `appliance-images/BIGIP*.qcow2` and `BIGIP*.qcow2.zip`. If
   only a `.zip` is present, it is auto-extracted into the same folder and the zip is
   deleted on success.
2. **Create the VM** — machine type `pc-i440fx-8.0`, SeaBIOS, `virtio-scsi-pci`, serial
   console enabled, and N NICs from the `nics[]` array (default model `vmxnet3`). Bridge
   existence on the target node is verified before this step so a missing bridge fails
   fast (no wasted 8 GB upload).
3. **Upload qcow + import disk** — paramiko SFTP to the Proxmox node, then `qm importdisk`
   onto the chosen storage as an unused disk; temporary copy in `/tmp/` is cleaned up.
4. **Attach disk + configure NICs + start** — disk attached as `scsi0`, resized to
   `disk_gb`, boot order set; each NIC configured per the `nics[]` array. Deployment file
   is saved with the assigned VMID before first-boot begins, so a partial deploy can still
   be cleanly decommissioned.
5. **First-boot configuration** via the Proxmox node's serial console:
   - Log in as `root` / `default`, change to the configured `password`
   - Set `admin` password to match (`passwd admin`)
   - Wait for `mcpd` (BIG-IP's management control plane) to be stably running
   - Configure mgmt IP / prefix / gateway via `tmsh`
   - Configure DNS + NTP (if provided in the deployment file) via `tmsh`
   - Wait for mcpd to stabilize again
   - `tmsh install sys license registration-key …` — talks to F5's activation server
   - Wait for mcpd to stabilize after the license-driven restart
   - Verify license is active
6. **Register DNS** — A + PTR records on the configured BIND server (same flow as LXC/VM
   deploys).
7. **Update Ansible inventory** — adds the BIG-IP to the `[BIGIPs]` group on the
   development server. Entry format:
   ```
   <hostname> ansible_host=<fqdn> ansible_python_interpreter=/usr/bin/python3 \
              license_key=<key> bigip_user=admin bigip_password=<password>
   ```

Total elapsed: roughly 20–30 minutes — most of it is the 8 GB SFTP, the qcow import, and
the mcpd-stability waits during license activation.

---

## What `decomm_bigip.py` does (5 steps)

1. **Revoke license** via serial console: log in, `tmsh revoke sys license`, answer the
   `Y/N` confirmation, wait for mcpd to recover, verify revoked. **The F5 key returns to
   your activation pool.** Bypass with `--force-decomm` if the VM is hung/unreachable or
   you've already revoked manually.
2. **Remove DNS records** (A + PTR). Non-fatal.
3. **Remove from Ansible inventory.** Non-fatal.
4. **Destroy VM** (stop + delete with purge).
5. **Deployment file** — kept on disk by default. Pass `--purge` to delete.

If license revocation fails, the script **stops before destroying the VM** so the operator
can investigate. You don't lose the appliance to a transient F5 outage.

---

## Prerequisites

- A licensed BIG-IP `.qcow2` (or `.qcow2.zip`) from F5 staged in `appliance-images/`.
  See [`appliance-images/README.md`](../appliance-images/README.md) for how to get it.
- SSH key authorized as `root` on every Proxmox node.
- A deployment file at `deployments/bigip/<hostname>.json` — copy
  `deployments/bigip/example-bigip-deployment.json` and edit the placeholders.

The deployment file must include:

| Field | Notes |
|---|---|
| `hostname`, `fqdn`, `node` | Identifying info |
| `qcow_filename` | Filename in `appliance-images/`; can name the `.zip` or `.qcow2` |
| `password` | The new password to set on `root` + `admin` during first-boot |
| `mgmt_ip`, `mgmt_prefix_len`, `mgmt_gateway` | Static management network config |
| `mgmt_dns` | Optional, but **required for license activation** to resolve `activate.f5.com` unless your network already has DNS reachable from the mgmt subnet |
| `registration_key` | F5 license key |
| `nics[]` | At least one NIC; first is mgmt |

Full schema reference: [`docs/specs/deployment-file.md`](specs/deployment-file.md).

---

## CLI Options

The tables below document the helper scripts' own flags. **Most users won't need them
— call `deploy.py` / `decomm.py` instead** (see [Usage](#usage) above). These tables
exist so that, if you do call the helpers directly (or read their `--help` output),
you know exactly what each flag does.

### `deploy_bigip.py`

| Option | Description |
|---|---|
| `--deploy-file FILE` | **Required.** Path to the BIG-IP deployment JSON. There is no interactive wizard for BIG-IP — every deploy must reference a file. |
| `--validate` | Validate the deployment file and exit. No Proxmox connection. Checks JSON shape, required fields (`hostname`, `node`, `qcow_filename`, `storage`, `cpus`, `memory_gb`, `disk_gb`, `nics[]`, `password`, `mgmt_ip`, `mgmt_prefix_len`, `mgmt_dns`, `registration_key`), and confirms `qcow_filename` resolves to a real `.qcow2` (or matching `.qcow2.zip`) in `appliance-images/`. |
| `--silent` | Accepted for compatibility with the batch dispatcher (`deploy.py` invokes type-specific scripts in silent mode). A no-op for `deploy_bigip.py` — BIG-IP deploys are already entirely file-driven and never prompt. |
| `--ttl TTL` | Time-to-live for this deployment (e.g. `7d`, `24h`, `2w`, `30m`). Stores `expires_at` in the deployment JSON for use with `expire.py`. |
| `--config FILE` | Use an alternate config file instead of the default `config.yaml` in the project root. |
| `--help`, `--?` | Show help and exit. |

### `decomm_bigip.py`

| Option | Description |
|---|---|
| `--deploy-file FILE` | **Required.** Path to the BIG-IP deployment JSON to destroy. |
| `--silent` | Skip the typed-hostname confirmation challenge. Use only in batch / automated contexts; combine with `--deploy-file` (which is already required). |
| `--purge` | Also delete the local deployment JSON file after the VM is destroyed. |
| `--force-decomm` | Skip the F5 license-revocation step. Use only when the VM is stopped / unreachable or the license was already revoked manually — the registration key will remain consumed in your F5 activation pool. The script otherwise refuses to proceed if the VM isn't running, because revocation drives the BIG-IP serial console. |
| `--config FILE` | Use an alternate config file instead of the default `config.yaml` in the project root. |
| `--help`, `--?` | Show help and exit. |

---

## Usage

Use `deploy.py` / `decomm.py` — they auto-dispatch BIG-IP JSON files to the helper
scripts described below.

```bash
# Deploy
python3 deploy.py --deploy-file deployments/bigip/bigip-prod01.json

# Validate the deployment file (no Proxmox connection).
# Also confirms qcow_filename actually resolves in appliance-images/ —
# accepts either a bare .qcow2 or a matching .qcow2.zip.
python3 deploy.py --deploy-file deployments/bigip/bigip-prod01.json --validate

# With TTL — sets expires_at for expire.py to manage
python3 deploy.py --deploy-file deployments/bigip/bigip-prod01.json --ttl 30d

# Batch deploy (parallel + staggered start to avoid SFTP contention)
python3 deploy.py --batch deployments/bigip/bigip-prod01.json deployments/bigip/bigip-prod02.json --parallel 2 --stagger 30

# Decommission (license revoke + DNS removal + inventory cleanup + destroy)
python3 decomm.py --deploy-file deployments/bigip/bigip-prod01.json

# Force decomm — skip license revocation (use only when VM hung or already revoked)
python3 decomm.py --deploy-file deployments/bigip/bigip-prod01.json --force-decomm
```

### Direct invocation of the helper scripts (for reference only)

Calling `deploy_bigip.py` / `decomm_bigip.py` directly is supported but **not the
recommended path**. It gives you no extra capability over the dispatcher form above —
both go through the exact same code. Prefer the `deploy.py` / `decomm.py` invocations.

```bash
python3 deploy_bigip.py --deploy-file deployments/bigip/bigip-prod01.json
python3 decomm_bigip.py --deploy-file deployments/bigip/bigip-prod01.json
```

---

## After deployment

When deploy completes successfully, the BIG-IP is:

- Booted and licensed
- Reachable at `https://<mgmt_ip>/` with `admin` / `<your password>`
- SSH-reachable at `ssh root@<mgmt_ip>` with the same password
- Registered in DNS (A + PTR via BIND)
- Listed in the `[BIGIPs]` group of the Ansible inventory with credentials + license key

`net0` is always the management interface. The remaining NICs are presented to BIG-IP as
data plane interfaces in the order they appear in `nics[]` — TMOS will pick them up as
unconfigured data plane and you assign them to VLANs / self-IPs / trunks from the BIG-IP
side.

---

## Operational notes

- **F5 hard requirement:** `machine_type` must be `pc-i440fx-8.0` (the default in
  `deploy_bigip.py`). Override at your own risk.
- **License activation requires DNS + outbound HTTPS** from the management IP to F5's
  activation server. If the mgmt subnet can't reach the internet, activation will fail
  with `Cannot determine IP for license server.`
- **Parallel deploys** can occasionally stall in paramiko's SFTP layer when uploading two
  8 GB qcows to two different nodes from the same client. Use `--parallel 2 --stagger 30`
  and treat hangs as a known issue (kill the stuck process, retry that one solo).
- **mcpd patience** — the script polls `tmsh show sys mcp-state` every 60s for up to 30
  minutes and requires 2 consecutive healthy checks before proceeding. License operations
  restart mcpd; this is expected and the script waits it out.
- **The deployment file is saved after VM start, before first-boot** — so if first-boot
  fails (bad license key, DNS unreachable, etc.) the operator can still cleanly decomm via
  `decomm.py --deploy-file …`.

---

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `Bridge(s) missing on <node>` | A NIC's bridge doesn't exist on the target node | Pick a node with that bridge, or change `nics[].bridge` |
| `dig activate.f5.com` returns no A record | `mgmt_dns` not set or DNS server unreachable from mgmt subnet | Add `mgmt_dns` to the JSON; verify VLAN routing |
| `Cannot determine IP for license server` | mgmt IP can't reach activate.f5.com over HTTPS | Open outbound 443 from mgmt subnet; check firewall |
| `License install was REJECTED — mcpd became unavailable` | mcpd restarted mid-`tmsh install` | Re-run the deploy; the next run will wait for mcpd before retrying |
| `Could not detect login prompt or shell on the serial console` | Stale `qm terminal` process on the Proxmox node holding the serial socket | Kill the stale process on the Proxmox node; the script's cleanup logic prevents this for new runs |

---

[← Back to README](../README.md)
