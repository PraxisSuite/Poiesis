[← Back to README](../README.md)

# Deploying Virtual Machines

### About

`deploy_vm.py` is an interactive wizard that provisions, configures, and onboards a new QEMU virtual machine in your Proxmox cluster in a single guided session. It handles everything from cloud image selection and VM creation through post-deployment configuration, DNS registration, and Ansible inventory registration.

The wizard can be driven entirely interactively, pre-filled from a deployment JSON file, or run fully non-interactively in `--silent` mode for automated pipelines.

## Table of Contents

- [CLI Options](#cli-options)
- [Interactive Mode](#interactive-mode)
- [Wizard Navigation](#wizard-navigation)
- [Deploy from File](#deploy-from-file)
- [Silent Mode](#silent-mode)
- [Validate Mode](#validate-mode)
- [Dry-Run Mode](#dry-run-mode)
- [TTL / Expiry](#ttl--expiry)
- [VLAN Check Behavior](#vlan-check-behavior)
- [CPU Baseline Check (RHEL 10 family)](#cpu-baseline-check-rhel-10-family)
- [FreeBSD Deployments (serial-console firstboot)](#freebsd-deployments-serial-console-firstboot)
- [Preflight Behavior](#preflight-behavior)
- [Walkthrough: VM Prompt Order](#walkthrough-vm-prompt-order)
- [The 7 VM Deployment Steps](#the-7-vm-deployment-steps)

---

## CLI Options

The following command line options are available:

| Option | Description |
|---|---|
| `--deploy-file FILE` | Load a deployment JSON to pre-fill all prompts (or drive `--silent`) |
| `--silent` | Non-interactive: use all values from `--deploy-file` without prompting. Requires `--deploy-file`. |
| `--validate` | Parse and validate `config.yaml` (and deploy file if given) then exit. No Proxmox connection. |
| `--dry-run` | Validate config and deploy file, then print a full step-by-step plan without making any changes |
| `--preflight` | Run all preflight connectivity and dependency checks then exit |
| `--yolo` | Run preflight but continue through warnings without prompting. Fatal failures still block the deploy. |
| `--ttl TTL` | Set a TTL for this deployment (e.g. `7d`, `24h`, `2w`, `30m`). Stores `ttl` and `expires_at` in the deployment JSON. |
| `--config FILE` | Use an alternate config file instead of the default `config.yaml` in the project root |

---

## Interactive Mode

Runs the full interactive wizard. All prompts have defaults sourced from `config.yaml`. See [Walkthrough: VM Prompt Order](#walkthrough-vm-prompt-order) for the full sequence.

```bash
python3 deploy_vm.py
```

---

## Wizard Navigation

The interactive wizard supports back-navigation at every prompt — you can move backward through the wizard to review or change an earlier answer without starting over.

| Prompt type | How to go back |
|---|---|
| Text input | Press **ESC** |
| Selection list | Arrow up to **← Go Back** and press **Enter** |

Going back restores the value you previously entered for that prompt, not the config default. ESC at the first prompt exits with `Aborted.` Ctrl+C exits immediately from any prompt.

---

## Deploy from File

```bash
python3 deploy_vm.py --deploy-file deployments/vms/myvm.json
```

Loads a previously saved deployment JSON and pre-fills all prompts with its values. You can review and edit each value before confirming. Useful for redeploying a VM with the same or similar configuration.

**Required fields in a hand-written deploy file:**

| Field | Example | Notes |
|---|---|---|
| `hostname` | `"myserver"` | Short name, no domain suffix |
| `ip_address` | `"dhcp"` or `"192.168.1.100"` | Use `"dhcp"` for DHCP, or a static IPv4 address |
| `prefix_len` | `"24"` | Required only when `ip_address` is a static IP |
| `cpus` | `2` | |
| `memory_gb` | `4` | |
| `disk_gb` | `40` | |
| `vlan` | `220` | |
| `storage` | `"local-lvm"` | |
| `password` | `"changeme"` | |
| `node` | `"proxmox01"` | Used by `--dry-run` display; wizard re-prompts at deploy time |

Optional fields: `package_profile`, `extra_packages`, `ttl`.

---

## Silent Mode
Skips all interactive prompts and deploys using the values in the file. `--silent` requires `--deploy-file`. Any preflight warning or fatal failure causes an immediate exit with a non-zero status code — there are no prompts to continue or retry.

```bash
python3 deploy_vm.py --deploy-file deployments/vms/myvm.json --silent
```

---

## Validate Mode
Parses and validates `config.yaml` (and the deployment file if provided) without connecting to Proxmox or making any changes. Exits 0 on success, 1 on error.

```bash
python3 deploy_vm.py --validate
python3 deploy_vm.py --validate --deploy-file deployments/vms/myvm.json
```

Use `--validate` when you want a simple **pass/fail answer**: "Is my deployment file correct?" It is well suited for CI pipelines where you just need an exit code, not a plan.

---

## Dry-Run Mode
Does everything `--validate` does, then goes further — prints a full human-readable summary of what *would* happen: hostname, node, image, resources, packages, tags, and each numbered step in order. No Proxmox connection, no Ansible, no changes made.

```bash
python3 deploy_vm.py --dry-run
python3 deploy_vm.py --dry-run --deploy-file deployments/vms/myvm.json
```

Use `--dry-run` when you want to **see the plan**: "What will actually happen when I deploy this?" It is the more useful option for day-to-day use before committing to a deployment.

---

## TTL / Expiry

Pass `--ttl` to mark a deployment as temporary:

```bash
./deploy_vm.py --deploy-file deployments/vms/staging-vm.json --ttl 24h
```

The TTL is stored in the deployment JSON as two fields:

```json
"ttl": "24h",
"expires_at": "2026-03-07T10:00:00.000000+00:00"
```

`expires_at` is calculated at deploy time as `now() + TTL`. Deployments without an `expires_at` field are ignored by `expire.py`.

**TTL format:**
The TTL format uses common abbreviations for units of time, they are:

| Unit | Meaning | Example |
|---|---|---|
| `m` | minutes | `30m` |
| `h` | hours | `24h` |
| `d` | days | `7d` |
| `w` | weeks | `2w` |

See [expiry.md](expiry.md) for the full TTL management workflow.

---

## VLAN Check Behavior

Before creating the VM, the script verifies that the requested VLAN exists on the target node. For traditional VLAN bridges, it looks for a `vmbr0.220`-style interface in the node's network list. For VLAN-aware bridges, the check passes for any VLAN tag — the bridge accepts all tags and relies on upstream switch configuration to enforce VLAN membership.

> **Note:** If your VM gets an IP but it is on the wrong network, verify the VLAN is trunked on the physical port connected to the Proxmox node. The VLAN check cannot detect upstream switch misconfigurations.

---

## CPU Baseline Check (RHEL 10 family)

Immediately after the VLAN check, `deploy_vm.py` verifies that the target node's CPU supports every instruction set required by the requested `cpu_type` model.

**Why this matters:** Red Hat raised the baseline microarchitecture for RHEL 10 to **`x86-64-v3`** (Haswell-era — requires AVX2, BMI1/2, FMA, MOVBE, LZCNT). Rocky 10, AlmaLinux 10, and CentOS Stream 10 inherit that requirement. Deploying one of these cloud images to a Proxmox node with a pre-Haswell CPU (Sandy/Ivy Bridge Xeons, older Atoms, etc.) produces a kernel that hits an illegal-instruction fault during boot, causing the VM to reboot-loop indefinitely — the symptoms look like a network problem, but it's a CPU compatibility issue. See [BUG-006 in known-bugs.md](../known-bugs.md) for the full diagnosis story.

**How to use it:**

For RHEL-10-family deploys, set `cpu_type` in the deployment JSON:

```json
{
  "type": "vm",
  "hostname": "rocky10-test",
  "node": "proxmox01",
  "cpu_type": "x86-64-v3",
  ...
}
```

The check runs before any expensive SFTP/import work. If the node lacks a required flag, the deploy aborts immediately with output like:

```
✗ Node proxmoxb01 cannot run cpu=x86-64-v3: CPU is missing flag(s) avx2, bmi1, bmi2, fma, movbe, abm
  CPU on proxmoxb01: Intel(R) Xeon(R) CPU E5-2640 v2 @ 2.00GHz
  Choose a different node, or set a lower `cpu_type` in the deployment file
  (e.g. "cpu_type": "x86-64-v2-AES").
```

**Supported cpu_type values:** `x86-64-v2`, `x86-64-v2-AES` (Poiesis's default), `x86-64-v3`, `x86-64-v4`. Specific CPU model names (`host`, `Haswell`, `Skylake`, etc.) are accepted but skip the flag-level check — the operator is presumed to know what they're requesting.

**Default:** The global default lives at `vm.cpu_type` in `config.yaml` (ships as `x86-64-v2-AES`). The per-deployment `cpu_type` JSON field always wins when present.

---

## FreeBSD Deployments (serial-console firstboot)

FreeBSD VM deployments require special handling because the FreeBSD project's `BASIC-CLOUDINIT-*` qcow images don't actually ship full `cloud-init` — they ship `nuageinit`, a limited cloud-init alternative that can't parse Proxmox's NoCloud-format static IP / password / SSH key. So a fresh FreeBSD boot ignores everything Proxmox cloud-init sends and lands at a passwordless root console with a DHCP-assigned IP.

Poiesis handles this transparently:

1. **`.xz` decompression** — `qm importdisk` doesn't auto-decompress; Poiesis runs `xz -dkf` on the cached `.qcow2.xz` before importing, keeping the original `.xz` so re-deploys stay warm.
2. **`modules/freebsd_firstboot.py`** — after Step 3 (VM start), Poiesis attaches to the VM's serial console via `qm terminal <vmid>` (paramiko-driven SSH session on the Proxmox node) and:
   - Logs in as root (no password — FreeBSD cloud-image default)
   - Sets hostname + root password (via `pw usermod -h 0`)
   - Installs the deploy SSH public key into `/root/.ssh/authorized_keys` (chunked write — FreeBSD's serial tty has a ~256-char input-line limit, so the ~370-char key body is split across short `echo -n … >> file` writes)
   - Writes `/etc/ssh/sshd_config.d/00-poiesis.conf` with `PermitRootLogin yes` (FreeBSD's stock sshd refuses root by default) and ensures the main `sshd_config` has the `Include` directive
   - Writes `/etc/rc.conf` with the static IP (`ifconfig_vtnet0`), gateway, removes the DHCP default, disables nuageinit
   - Writes `/etc/resolv.conf` with the configured DNS servers
   - Restarts networking (`service netif restart && service routing restart`)
   - Installs `python311` via pkg and symlinks `/usr/local/bin/python3 -> python3.11` so Ansible's interpreter-discovery succeeds

3. **Ansible post-deploy** then runs normally — the playbook has FreeBSD-aware variants for `chpasswd` (uses `pw`), skips the cloud-init-result wait task (nuageinit doesn't create `/run/cloud-init/result.json`), and `ansible/vars/FreeBSD.yml` uses FreeBSD pkg names instead of Linux ones — notably `7-zip` (not `p7zip`), no `ethtool` (Linux-only, no equivalent), no `bc` (already in FreeBSD base, not a separate pkg), and `vim` (not the RHEL-10 name `vim-enhanced`).

**Hard requirements for FreeBSD deployments:**

- **Static IP only.** Set `ip_address`, `prefix_len`, `gateway` in the deployment JSON. DHCP isn't supported because the cloud image doesn't ship qemu-guest-agent, so Poiesis has no way to discover a DHCP-assigned IP. The firstboot module will refuse to proceed if `ip_address` is `"dhcp"`.
- **Catalog entries must use `BASIC-CLOUDINIT-*` variants** (e.g. `FreeBSD-15.0-RELEASE-amd64-BASIC-CLOUDINIT-ufs.qcow2.xz`). Plain `-ufs.qcow2.xz` doesn't even ship nuageinit; it'd be unreachable. The shipped `cloud-images.yaml` already points at the right variants.
- **Detection is by filename** — `deploy_vm.py` runs the FreeBSD firstboot path when `cloud_image_filename` starts with `FreeBSD-`.

**Typical timing:** ~8 min total for a fresh FreeBSD deploy on a `proxmoxb`-class node. ~30s decompression, ~1 min VM boot, ~1.5 min firstboot (dominated by `pkg install python311` + dependencies), ~5 min Ansible (mostly the standard package install + `pkg upgrade`).

See [known-bugs.md BUG-010](../known-bugs.md) for the full diagnosis of all seven issues that had to be fixed to get FreeBSD working.

---

## Preflight Behavior

`deploy_vm.py` runs a preflight check suite automatically at the start of every deployment — before any prompts are shown or Proxmox resources are created.

Run standalone to check your environment and exit without deploying:

```bash
./deploy_vm.py --preflight
./deploy_vm.py --preflight --deploy-file deployments/vms/myvm.json
```

Add `--deploy-file` to also check the DNS hostname and static IP. Add `--yolo` to continue through warnings and only block on fatal failures.

See [preflight.md](preflight.md) for the full list of checks, fatal vs warning behavior, and flag interactions.

---

## Walkthrough: VM Prompt Order

Resource questions come **before** node selection so that nodes without enough capacity for the requested resources are filtered out of the list.

**1. Hostname**
```
Hostname for the new VM:
(short name only — domain suffix from config will be appended in inventory)
> myvm
```
The short hostname only — no domain suffix. The domain from `node_domain` (under the `proxmox:` block in `config.yaml`) is appended automatically when registering DNS and inventory records.

**2. vCPUs / Memory / Disk / VLAN / Password**
```
Number of vCPUs:                     [2]
Memory (GB):                         [4]
Disk size (GB):                      [100]
VLAN tag (bridge: vmbr0.<vlan>):     [220]
Root / admin user password:          [changeme]
```
Defaults are sourced from the `defaults:` block in `config.yaml`. The password is set for both `root` and the secondary admin user via cloud-init.

> **Note:** These resource values are asked *before* node selection so that nodes which cannot satisfy the request are automatically hidden from the node list.

**3. IP address** (static or DHCP)
```
IP address for VM:
(e.g. 10.20.20.200  —  leave blank for DHCP)
>
```
Leave blank to use DHCP. The VM boots with `ip=dhcp` in cloud-init, then the QEMU guest agent is polled until it reports the assigned address.

**4. Prefix length and gateway** (skipped if DHCP)
```
Prefix length (subnet mask bits):    [24]
Gateway:                             [192.168.1.1]
```
The gateway default is derived automatically from the static IP address entered above.

**5. Node selection** (filtered by requested resources)
```
Select Proxmox node (★ = most free RAM; 2 node(s) hidden — over resource threshold):
  ★ proxmox03  —  RAM: [54.2 GB free / 128.0 GB]  CPU: [18%]
    proxmox02  —  RAM: [28.4 GB free / 64.0 GB]   CPU: [12%]
```
Only nodes with enough headroom are shown. The star (★) marks the node with the most free RAM. Nodes are hidden if allocating the requested resources would push them above `cpu_threshold` or `ram_threshold` (set in `config.yaml` under `defaults`).

**6. Cloud image storage** (ISO-capable datastores only)
```
Select storage for cloud image:
  Net-Images  (1.8 TB free / 2.0 TB)
  local       (42.3 GB free / 118.0 GB)
```
Only datastores with ISO content type enabled are shown, along with their free space. A warning is printed if `local` is selected, noting that it is shared with OS and possibly VM disks.

**7. Image selection** (two-level browser)
```
Select image from Net-Images:
  noble-server-cloudimg-amd64.img   (623 MB)
  ─── Download from catalog ───
  Download: Ubuntu 24.04 LTS (Noble Numbat)
  Download: Ubuntu 22.04 LTS (Jammy Jellyfish)
  Download: Debian 12 (Bookworm)
  ← Back to storage selection
```
Images already present on the selected storage are listed first with their size. Selecting a "Download:" entry fetches the image to `{storage_path}/cloud-images/` on the node before importing. The Back option returns to the storage picker.

**8. Storage pool** — Only shown if more than one storage pool is available on the selected node. Determines where the VM's root disk is created.
```
Select storage pool for VM disk:
  local-lvm
  ceph-pool
```

**9. Package profile** — Optional. Select a role-based package set or skip for a minimal baseline install. Profiles are named groups of packages defined in `config.yaml` under `package_profiles`. Each profile also applies one or more Proxmox tags to the VM so it is easy to identify its role at a glance. Selecting a profile installs its packages during the Ansible post-deploy step, after the baseline tools. Install order: baseline → profile packages → any extra packages entered at the prompt. Select `[none]` to skip and get only the baseline install.
```
Package profile (optional):
  [none]
  web-server
  database
  docker-host
  monitoring-node
  dev-tools
  nfs-server
```

**10. Confirmation summary and pre-creation resource check** — Displays a full summary of all selected values before anything is created. A final resource check is run against the selected node. Confirm to proceed or abort to cancel without making any changes.
```
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃                      Deployment Summary                       ┃
┡━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ VMID            │ 115                                         │
│ Hostname        │ myvm                                        │
│ Node            │ proxmox03                                   │
│ Image           │ Net-Images:noble-server-cloudimg-amd64.img  │
│ Machine         │ q35 / SeaBIOS / x86-64-v2-AES / virtio-... │
│ vCPUs           │ 2                                           │
│ Memory          │ 4 GB (4096 MB)                              │
│ Disk            │ 100 GB  →  local-lvm  (scsi0)               │
│ Network         │ vmbr0.220  (DHCP — IP assigned at boot)     │
│ SSH key         │ ~/.ssh/id_rsa.pub                           │
│ Tags            │ auto-deploy                                 │
│ TTL             │ (none)                                      │
│ Users           │ root, admin (same password)                 │
│ Timezone        │ America/Chicago                             │
│ NTP             │ pool.ntp.org                                │
│ SNMP            │ community='your-community' (rw) on :161     │
└─────────────────┴─────────────────────────────────────────────┘

Proceed with deployment? [Y/n]
```

---

## The 7 VM Deployment Steps

**Step 1** — Creates the VM via the Proxmox API with the configured resources, VLAN tag, machine type, BIOS, CPU model, and storage controller. A VMID is selected automatically from the next available ID in the cluster.

```
─── Step 1/7: Creating QEMU VM ───
  Creating VM 115 (myvm) on proxmox03...
  ✓ VM 115 created
```

**Step 2** — Downloads the cloud image if not already present on the target node, imports it as the VM's boot disk, attaches a cloud-init drive, resizes the disk to the requested size, and writes the network and user configuration. A serial console is configured automatically — required for Ubuntu cloud images.

```
─── Step 2/7: Importing cloud image and configuring VM ───
  Attaching disk and configuring cloud-init...
  Attached unused0 as scsi0
  Added cloud-init drive (ide2)
  Resized scsi0 to 100 GB
  Cloud-init: DHCP + SSH key + guest-agent snippet
  ✓ VM configured
```

> **Under The Hood**
> The cloud image is stored at `{storage_path}/cloud-images/` on the Proxmox node — not in `template/iso/` — so it stays invisible to the Proxmox GUI's ISO picker. The disk is imported with `qm importdisk` over SSH, then a serial console (`serial0=socket`, `vga=serial0`) is set automatically.

**Step 3** — Starts the VM and waits for it to come online.

```
─── Step 3/7: Starting VM ───
  Starting VM...
  ✓ VM started
```

**Step 4** — For DHCP deployments, polls the QEMU guest agent until it reports a non-loopback IPv4 address (cloud-init installs the guest agent during first boot, which may take 2–4 minutes). For static IP deployments, polls TCP port 22 until SSH accepts connections.

```
─── Step 4/7: Discovering DHCP IP via guest agent ───
  (cloud-init installs qemu-guest-agent during first boot — this may take 2–4 min)
  Waiting for guest agent to report IP (up to 5 min)...
  ✓ DHCP assigned IP: 192.168.1.115
  Waiting for SSH on 192.168.1.115...
  ✓ SSH is up on 192.168.1.115
```

**Step 5** — Runs the post-deploy Ansible playbook (`ansible/post-deploy-vm.yml`) against the new VM. The playbook waits for `/run/cloud-init/result.json` before making any changes — this file is written when all cloud-init first-boot stages complete, ensuring the OS is fully initialized before Ansible touches it.

What Ansible does, in order:

1. **Baseline install** — packages every VM gets regardless of profile: `curl`, `wget`, `vim`, `git`, `htop`, `net-tools`, `openssh-server`, and others defined in the playbook
2. **OS upgrade** — runs a full package upgrade (`apt upgrade`, `dnf upgrade`, etc.) so the VM starts life fully patched
3. **Profile packages** — installs the package set from the profile selected at the prompt (e.g. `docker-ce` and friends for `docker-host`, `nginx` and `certbot` for `web-server`). Skipped if `[none]` was selected
4. **Extra packages** — any additional packages entered at the extra packages prompt, installed after the profile
5. **Users** — creates the secondary admin user and sets passwords for both `root` and the admin user
6. **NTP** — configures chrony with the servers from `config.yaml`
7. **SNMP** — configures `snmpd` with the community, location, and contact from `config.yaml`
8. **Timezone** — sets the system timezone

Skipped entirely if `ansible.enabled` is `false` in `config.yaml`.

```
─── Step 5/7: Running post-deployment configuration (Ansible) ───
  Running: ansible-playbook -i ... ansible/post-deploy-vm.yml
  ✓ Post-deployment configuration complete
```

**Step 6** — DNS pre-check and registration. Before writing any records, the configured DNS server is queried directly for the hostname. If a record already exists:
- **Same IP:** idempotent notice — continues, warns the existing host may be orphaned
- **Different IP:** shows both IPs and prompts: **[O]verwrite**, **[S]kip DNS**, **[A]bort**
- **Multiple records:** shows all existing records with count, then prompts

In `--silent` mode, existing records are overwritten automatically with a logged warning. A and PTR records are written to the BIND zone files and `rndc reload` is called. If the reverse zone file does not exist, the PTR record is skipped gracefully. Skipped if `dns.enabled` is `false` in `config.yaml`.

```
─── Step 6/7: Registering DNS records ───
  Registering myvm.example.com → 192.168.1.115 on 10.0.0.10...
  ✓ DNS registered: myvm.example.com → 192.168.1.115 (+ PTR)
```

**Step 7** — Adds the new host to the Ansible inventory file on the configured inventory server. Also runs `ssh-keyscan` and `ssh-copy-id` from the inventory server to the new VM so Ansible can connect with key-based auth immediately. Skipped if `ansible_inventory.enabled` is `false` in `config.yaml`.

```
─── Step 7/7: Updating Ansible inventory ───
  Connecting to dev.example.com to update inventory...
  ✓ Inventory updated on dev.example.com
```

A history log entry is written to `deployments/history.log` on completion.

Once all steps complete, a deployment summary is printed:

```
  Hostname   :  myvm
  IP Address :  192.168.1.115  (DHCP-assigned)
  VMID       :  115  (on proxmox03)
  SSH        :  ssh root@192.168.1.115
               ssh admin@192.168.1.115

  Tagged 'auto-deploy' with specs note in Proxmox.
  Added to Ansible inventory group [Linux].
```

---

[← Back to README](../README.md)
