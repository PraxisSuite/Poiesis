[← Back to README](../../README.md)

# Poiesis — Architecture Overview

### About

Poiesis is a command-line tool for deploying and managing **LXC containers**, **QEMU VMs**, and **F5 BIG-IP appliances** in a Proxmox VE homelab cluster. It handles the full lifecycle: creation, post-deploy configuration, DNS registration, inventory registration, TTL-based expiry, and decommission. Three different resource types share one pipeline architecture; the per-type differences live in dedicated modules and Ansible playbooks.

---

## High-Level Flow

```
User runs deploy.py (or deploy_lxc.py / deploy_vm.py wizard directly)
        │
        ├─ 1. Preflight checks (API, SSH, Ansible, DNS, inventory reachable)
        ├─ 2. Interactive prompts (or --deploy-file + --silent)
        ├─ 3. Create resource in Proxmox via API
        ├─ 4. Configure cloud-init / bootstrap SSH
        │    └─ FreeBSD: serial-console firstboot module replaces cloud-init
        │       (writes /etc/rc.conf, password, SSH key, python install)
        │    └─ BIG-IP : serial-console firstboot drives login + password +
        │       tmsh mgmt IP/DNS + license activation
        ├─ 5. Run Ansible post-deploy playbook (Linux only — skipped on BIG-IP)
        ├─ 6. Register DNS A + PTR records on BIND server
        ├─ 7. Register host in Ansible inventory ([Linux] or [BIGIPs] group)
        └─ 8. Write deployment JSON to deployments/{lxc,vms,bigip}/

User runs decomm.py (or decomm_lxc.py / decomm_vm.py wizard directly)
        │
        ├─ 1. Read deployment JSON (--deploy-file or interactive selection)
        ├─ 2. Confirm destruction (typed challenge — randomized case)
        ├─ 3. BIG-IP only: revoke F5 license via tmsh first (so the key
        │      returns to the activation pool)
        ├─ 4. Stop and destroy resource via Proxmox API
        ├─ 5. Remove DNS records from BIND
        ├─ 6. Remove from Ansible inventory
        └─ 7. Report deployment file path (--purge to delete it)
```

---

## Scripts

**Primary entry points:**

| Script | Purpose |
|---|---|
| `deploy.py` | Single-file or batch deploy. Auto-dispatches to `deploy_lxc.py` / `deploy_vm.py` / `deploy_bigip.py` by the JSON's `type` field. **Required entry point for BIG-IP deploys.** Batch mode with `--batch`, `--batch-dir`, `--parallel`, `--stagger`, `--ttl`. |
| `decomm.py` | Single-file or batch decommission. Auto-dispatches by `type`. **Required entry point for BIG-IP decomms** (revokes license first). |
| `deploy_lxc.py` | LXC container wizard. Interactive or file-driven; supports DHCP and static IP. |
| `deploy_vm.py` | QEMU VM wizard via cloud-init. Multi-OS Ansible post-deploy. Has FreeBSD serial-console firstboot path. |
| `decomm_lxc.py` | LXC decommission, interactive or file-driven. |
| `decomm_vm.py` | VM decommission. |
| `configure.py` | Interactive wizard to build / edit / validate `config.yaml`. |
| `draft-deployment.py` | Build a deployment JSON without deploying — full LXC or VM wizard, supports batch generation (`How many of these to create?`), custom save location, and a sticky-answers loop for back-to-back drafts. |
| `cleanup_tagged.py` | Cluster-wide tag-based cleanup: scan, interactive keep / promote / retag / decomm, plan files. |
| `expire.py` | TTL-based expiry management — `--check`, `--reap`, `--renew`. |

**Helper scripts (called by the dispatchers — not normally invoked directly):**

| Script | Purpose |
|---|---|
| `deploy_bigip.py` | F5 BIG-IP qcow upload + VM create + serial-console first-boot + license activation. Called by `deploy.py`. |
| `decomm_bigip.py` | F5 BIG-IP license revoke + destroy + DNS / inventory cleanup. Called by `decomm.py`. |

---

## Shared Library (`modules/`)

All scripts import from `modules/`. The most-used module is `modules/lib.py`, which is the single source of truth for:

- Proxmox API connection and failover (`connect_proxmox`)
- Config loading and validation (`load_config`, `validate_config`)
- Node selection with resource filtering (`get_nodes_with_load`, `prompt_node_selection`)
- DNS add/remove wrappers (via Ansible playbooks)
- Ansible inventory add/remove wrappers
- Resource destruction (`stop_and_destroy`)
- Decommission pipeline (`decomm_resource`) — used by `cleanup_tagged.py` and `expire.py`
- Action list processing (`process_action_list`) — used by `cleanup_tagged.py` and `expire.py`
- Post-deploy health check (`health_check`)
- TTL parsing (`parse_ttl`)
- Deployment file helpers (`list_deployment_files`, `load_deployment_json`)
- Interactive confirmation (`confirm_destruction`)

Specialized modules in `modules/`:

| Module | Purpose |
|---|---|
| `modules/proxmox.py` | Proxmox API helpers, cloud-image download / `.xz` auto-decompression, qcow import, guest-agent IP discovery, write_guest_agent_snippet (init-system-aware vendor-data for qga install). |
| `modules/validation.py` | Deployment JSON + config validation, VLAN existence check, `check_node_cpu_baseline` preflight (catches RHEL-10-on-pre-Haswell mismatches). |
| `modules/preflight.py` | Standalone preflight check suite (API, SSH, Ansible, DNS, inventory reachability + auth). |
| `modules/bigip.py` | BIG-IP qcow resolve/extract, SFTP upload to Proxmox node, multi-NIC attach. |
| `modules/bigip_firstboot.py` | BIG-IP serial-console automation: login → password change → tmsh mgmt IP/DNS → license activation. |
| `modules/freebsd_firstboot.py` | FreeBSD serial-console automation: passwordless root login → write `/etc/rc.conf` + sshd drop-in → install python311 + symlink — required because the BASIC-CLOUDINIT image ships nuageinit (not real cloud-init). |
| `modules/ansible.py` | Ansible integration helpers (inventory write, post-deploy playbook invocation). |
| `modules/bind.py` | BIND DNS A/PTR registration + removal helpers. |
| `modules/ui.py` | Interactive wizard primitives — `pt_text`, `select_nav`, `checkbox_nav`, `run_wizard_steps` (with ESC-to-go-back). |
| `modules/io.py` | I/O helpers — deployment file write, history log append. |

**Why this layout matters:** `cleanup_tagged.py` and `expire.py` reuse the exact same decommission pipeline as `decomm_lxc.py`, `decomm_vm.py`, and `decomm_bigip.py`. Any fix or improvement to the decomm flow is automatically inherited by all five consumers. Similarly, the serial-console expect/automation pattern is shared between BIG-IP and FreeBSD firstboot — both follow the same `paramiko-SSH → qm terminal → SerialExpect` shape.

---

## External Systems

```
Poiesis (local machine)
    │
    ├──── Proxmox API (:8006) ─────────── create/destroy VMs and LXC
    │
    ├──── Proxmox node SSH (:22) ─────── cloud image downloads, snippets, bootstrap
    │
    ├──── Deployed host SSH (:22) ──────── Ansible post-deploy configuration
    │
    ├──── BIND DNS server SSH (:22) ──── A and PTR record registration/removal
    │
    └──── Ansible inventory server SSH (:22) ── known_hosts, ssh-copy-id, inventory file
```

All external connections use SSH key auth (via the key specified in `config.proxmox.ssh_key`).
No passwords are transmitted over the network except as Ansible `extra-vars` to the
post-deploy playbook.

---

## Deployment Files as State

Deployment JSON files in `deployments/lxc/` and `deployments/vms/` are the only
persistent state Poiesis maintains. There is no database.

- Deploy scripts write the file at completion.
- Decomm scripts read the file to drive cleanup. Optionally delete it with `--purge`.
- `expire.py` scans all files for `expires_at` to find expiring/expired deployments.
- `cleanup_tagged.py` uses the Proxmox API (not deployment files) to find tagged resources,
  but falls back to deployment files for IP information during DNS cleanup.

---

## Tagging Convention

Every resource deployed by Poiesis is tagged `auto-deploy` in Proxmox. Additional tags
come from the selected package profile (e.g. `WWW`, `DB`, `Docker`).

The `auto-deploy` tag is what `cleanup_tagged.py` uses to find managed resources. If
a resource is promoted (tag removed), it is no longer visible to cleanup and expiry tools.

---

## TTL and Expiry

Deployments can be given a TTL at deploy time (`--ttl 7d`). This stores `ttl` and
`expires_at` (ISO 8601 UTC) in the deployment file.

`expire.py` scans all deployment files for `expires_at`:
- `--check` — reports expired and expiring-soon resources. No Proxmox connection needed.
- `--reap` — decommissions all expired resources using the same pipeline as the decomm scripts.
- `--renew HOSTNAME --ttl Xd` — extends a deployment by updating `expires_at` in the file.

Deployments without `expires_at` are ignored by `expire.py`.

---

## Provider Architecture (aspirational)

DNS and inventory integrations are *designed* around a provider model, but the dispatch logic isn't fully implemented yet:

| Integration | Current provider | Status |
|---|---|---|
| DNS | `bind` | Only BIND is actually wired up. The `dns.provider` config key exists but the code always calls the BIND-flavored Ansible playbooks regardless of value. |
| Inventory | `flat_file` | Same — only flat-file inventory writes to a remote host's `hosts` file works today. |

The full provider plugin architecture (drop-in modules under `modules/providers/` with concrete classes per backend) is documented in [`Feature-ideas.md` → Provider Plugin Architecture](../../Feature-ideas.md). Until that lands, treat the `provider:` key as forward-looking — only the default values do anything.

---

## Further Reading

- `docs/specs/config-schema.md` — full `config.yaml` field reference
- `docs/specs/deployment-file.md` — LXC and VM deployment JSON schema
- `docs/specs/cleanup-action-list.md` — `--list-file` format for `cleanup_tagged.py`
- `docs/specs/cloud-images.md` — `cloud-images.yaml` catalog format
- `docs/integrations/ansible.md` — Ansible post-deploy and inventory integration
- `docs/integrations/bind-dns.md` — BIND DNS registration and removal
- `docs/specs/poreia-snapshot-manifest.md` — Poreia archive manifest spec (future integration)

---

[← Back to README](../../README.md)
