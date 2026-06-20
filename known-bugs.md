---
# About This Document
---

This file tracks known bugs and issues in Poiesis. It serves as both an open task list
and a historical record of what broke and how it was fixed.

**This is not a replacement for GitHub Issues.** Bugs here are either found by people
actively developing the tools, or are issues that were opened on GitHub and accepted for
work. For user-reported bugs or feature requests, open an issue at:
https://github.com/PraxisSuite/Poiesis/issues

**Conventions:**
- Bug IDs are sequential and never reused. Next ID: **BUG-009**.
- New bugs go into the `# Known Bugs / Issues` section with the next available ID.
- When a bug is fixed, move it to the **top** of the `# FIXED Bugs / Issues` section
  (most recently fixed first) and update Status, Date Fixed, and add a Fix Applied section.
- Severity levels: `Critical` (blocks deployment), `High` (data loss risk),
  `Medium` (functional breakage, workaround exists), `Low` (cosmetic).

---
# Example Bug Record
---

## BUG-000 — Short description of the problem

**Status:** Open | Fixed
**Severity:** Critical | High | Medium | Low
**Affected script:** `script-name.py` → `modules/lib.py` (`function_name`)
**First observed:** YYYY-MM-DD, during what test or operation
**Date Added:** YYYY-MM-DD
**Date Fixed:** YYYY-MM-DD | N/A

### Symptom

What the user sees. Include exact error messages or output snippets.

### Root Cause

What is actually wrong in the code. If unknown, label as `(suspected)` and describe
the theory. Include file and function references.

### To Investigate

- Bullet list of specific things to check if the root cause is not yet confirmed.
- When the bug is fixed, rename this section to `### To Investigate (FIXED: See Fix Applied below)` and leave the original verbiage intact.

### Fix Applied

What was changed and in which file/function. Include the commit or date if known.

### Workaround

If the bug is open, describe any workaround. If none exists, state that explicitly.


---
# Known Bugs / Issues
---


---
# FIXED Bugs / Issues
---

## BUG-008 — RHEL 10 family (Rocky 10, AlmaLinux 10) Ansible post-deploy fails on renamed packages + missing CRB repo on Rocky 10

**Status:** Fixed
**Severity:** High (blocks every Rocky 10 / Alma 10 deploy at the "Install all standard tools and utilities" task)
**Affected script:** `deploy_vm.py` / `deploy_lxc.py` → `ansible/vars/RedHat.yml`, `ansible/tasks/pre-install-RedHat.yml`
**First observed:** 2026-06-19 / 2026-06-20, validating BUG-006's CPU baseline fix with test-rocky10 / test-alma10 on `proxmox01`. Surfaced as soon as the boot-loop was unblocked.
**Date Added:** 2026-06-20
**Date Fixed:** 2026-06-20

### Symptom

Two distinct failures on the same Ansible task:

**Variant A — package not found (Alma 10 and Rocky 10):**

```
TASK [Install all standard tools and utilities] ********************************
fatal: [<ip>]: FAILED! => {"failures": ["No package arp-scan available."],
  "msg": "Failed to install some of the specified packages", "rc": 1}
```

**Variant B — depsolve failure (Rocky 10 only, after Variant A is fixed):**

```
TASK [Install all standard tools and utilities] ********************************
fatal: [<ip>]: FAILED! => {"msg": "Depsolve Error occurred:
  Problem 1: nothing provides python3.12dist(pathspec) >= 0.5.3 needed by
    yamllint-1.37.1-1.el10_2.noarch from epel
  Problem 2: nothing provides python3-setuptools-wheel needed by
    python3-virtualenv-20.31.2-4.el10_2.noarch from epel
    nothing provides python3-wheel-wheel needed by
    python3-virtualenv-20.31.2-4.el10_2.noarch from epel"}
```

### Root Cause

Two related changes Red Hat made for RHEL 10:

1. **Package renames and removals in the base/EPEL repos:**
   - `vim` → `vim-enhanced` (the meta-package was dropped)
   - `iotop` → `iotop-c` (the original Python iotop was replaced by a C rewrite)
   - `p7zip` → `7zip` (renamed when the EPEL 10 packager switched to the upstream-blessed name)
   - `arp-scan` — removed from EPEL 10 entirely (no replacement)
   - `snmp-mibs-downloader` — has never existed on RHEL family

2. **CRB (CodeReady Builder) is required for transitive deps but isn't enabled
   by default on Rocky 10.** CRB holds `python3-pathspec`, `python3-setuptools-wheel`,
   `python3-wheel-wheel`, etc. — the helpers that EPEL packages like `yamllint`
   and `python3-virtualenv` transitively depend on. **AlmaLinux 10 enables CRB by
   default in its cloud image; Rocky 10 does not.** So Alma 10 hides the issue;
   Rocky 10 exposes it.

   On RHEL 8 the equivalent repo is called `powertools` instead of `crb`.

Confirmed by `dnf repolist --enabled` on both VMs side-by-side: Alma 10 shows
`crb` in the list, Rocky 10 doesn't.

### Fix Applied

Two coordinated changes:

**`ansible/vars/RedHat.yml`:**
```yaml
- nano
- vim-enhanced       # was `vim`
...
- 7zip               # was `p7zip`
...
- htop
- iotop-c            # was `iotop`
...
# arp-scan removed — gone from EPEL 10
```

**`ansible/tasks/pre-install-RedHat.yml`** — added CRB/PowerTools enablement
between `epel-release` and the package install task:
```yaml
- name: Ensure dnf-plugins-core (provides config-manager) is installed
  dnf: { name: dnf-plugins-core, state: present }
  when: ansible_distribution != 'Fedora'

- name: Enable CRB repo (RHEL 9+ / Alma / Rocky / CentOS Stream)
  command: dnf config-manager --set-enabled crb
  failed_when: false
  when: ansible_distribution != 'Fedora' and ansible_distribution_major_version|int >= 9

- name: Enable PowerTools repo (RHEL 8 only)
  command: dnf config-manager --set-enabled powertools
  failed_when: false
  when: ansible_distribution != 'Fedora' and ansible_distribution_major_version|int == 8
```

`failed_when: false` makes both no-ops on systems where the repo is already
enabled or doesn't exist under that name.

### Compatibility risk

The package renames (`vim-enhanced`, `iotop-c`, `7zip`) are assumed to work on
Rocky 8/9 too — `vim-enhanced` has been the canonical name since RHEL 7;
`iotop-c` was added to EPEL 9 in 2022; `7zip` was added to EPEL 9 in 2024.
**Rocky 8 EPEL only has `p7zip`** (not `7zip`) — Rocky 8 deploys may now break.
If/when that comes up, split the package list with `ansible_distribution_major_version`
conditionals. Filed as a follow-up risk rather than blocking this fix.

### Validation

Both `test-rocky10` and `test-alma10` deployed cleanly end-to-end on `proxmox01`
with `cpu_type: x86-64-v3` after these fixes. Rocky 10 took 8m 29s; Alma 10
took 9m 51s.

---

## BUG-007 — openSUSE Leap 16 deploys fail: `nmap`, `dstat`, `python3-yamllint`, `python3-jsonschema`, `python3-virtualenv` not in `repo-oss`

**Status:** Fixed
**Severity:** High (blocks every openSUSE Leap 16 deploy at Ansible "Install all standard tools and utilities")
**Affected script:** `deploy_vm.py` / `deploy_lxc.py` → `ansible/vars/Suse.yml`
**First observed:** 2026-06-19, test-leap16 redeploy after BUG-005 fix (the sshd_config drop-in fix unblocked the playbook, surfacing this next failure)
**Date Added:** 2026-06-19
**Date Fixed:** 2026-06-19

### Symptom

```
TASK [Install all standard tools and utilities] ********************************
fatal: [<ip>]: FAILED! => {
  "msg": "No provider of '+nmap' found.\nNo provider of '+dstat' found.",
  "rc": 104
}
```

VM (or LXC) creates and Ansible runs through the sshd-config drop-in cleanly,
then dies on the standard-packages install.

### Root Cause

openSUSE Leap 16's Cloud Minimal VM image enables only two repositories by
default: `repo-oss` and `repo-openh264`. The following packages used to be in
`repo-oss` on Leap 15.x but are no longer present on Leap 16:

- `nmap` — moved to the `network:utilities` OBS sub-repo
- `dstat` (and its modern replacement `dool`) — removed entirely from `repo-oss`
- `python3-yamllint` — not packaged in `repo-oss` on Leap 16
- `python3-jsonschema` — same
- `python3-virtualenv` — same (use `python3 -m venv` or `pip install virtualenv`)

`iperf3` was already documented in the YAML as missing for the same reason.

Confirmed by `zypper se --match-exact -t package <name>` from inside the live
test-leap16 VM after the partial deploy.

### Fix Applied

`ansible/vars/Suse.yml` — removed the five missing packages and replaced each
with a comment explaining what's gone and how to install equivalents if needed.
Kept the rest of the package list intact since everything else verified present
on Leap 16.

```yaml
# nmap removed — not in repo-oss on Leap 16; install from network:utilities
# OBS repo manually if needed.
# python3-yamllint, python3-jsonschema removed — not packaged in repo-oss
# on Leap 16. Install via `pip install yamllint jsonschema` if needed.
# dstat (and replacement dool) removed — neither is in repo-oss on Leap 16.
# sar/iostat from sysstat cover most of dstat's use cases.
# python3-virtualenv removed — not in repo-oss on Leap 16; use
# `python3 -m venv` or `pip install virtualenv` instead.
```

Tumbleweed users who actually need these tools can install manually or enable
the relevant OBS repos. Removing them from the default install set means a
clean baseline deploy across both Leap 16 and Tumbleweed.

---

## BUG-006 — Rocky 10 / AlmaLinux 10 boot-loop on pre-Haswell hosts (`x86-64-v3` baseline)

**Status:** Fixed
**Severity:** High (silent — VM appears stuck at DHCP discovery; real failure is unsupported-instruction kernel panic at boot)
**Affected script:** `deploy_vm.py` → `modules/validation.py` (`check_node_cpu_baseline`)
**First observed:** 2026-06-19, batch-deploying test VMs for every newly added cloud image (test-rocky10 VMID 148, test-alma10 VMID 149 on `proxmoxb01`)
**Date Added:** 2026-06-19
**Date Fixed:** 2026-06-19

### Symptom

VM is created successfully, started, but never finishes booting:

```
─── Step 4/7: Discovering DHCP IP via guest agent ───
✗ Guest agent did not report a non-loopback IP within Ns.
```

The NIC's MAC never appears in the Proxmox bridge FDB and SSH is unreachable.
The serial console responds intermittently (it's actively reboot-looping). On
the Proxmox host, attempting to upgrade the VM's CPU to `x86-64-v3` produces:

```
kvm: warning: host doesn't support requested feature: CPUID[...].fma
kvm: warning: host doesn't support requested feature: CPUID[...].avx2
...
kvm: Host doesn't support requested features
start failed: QEMU exited with code 1
```

### Root Cause

**Red Hat raised the baseline microarchitecture for RHEL 10 to `x86-64-v3`.**
That floor inherits to Rocky 10, AlmaLinux 10, and CentOS Stream 10. Required
CPU features: AVX2, BMI1, BMI2, FMA, MOVBE, LZCNT — all of which arrived on
Intel Haswell (2013) and AMD Excavator (2015).

Poiesis's default `cpu_type` in `config.yaml` is `x86-64-v2-AES` and several
Proxmox nodes in this cluster (the proxmoxb01–03 / b06 Sandy/Ivy Bridge Xeons)
predate Haswell. So:
- Even setting `cpu_type: x86-64-v3` in a deployment wouldn't help — KVM
  refuses to start the VM because the host CPU genuinely lacks those features.
- Leaving `cpu_type: x86-64-v2-AES` (the default) lets the VM start, but Rocky
  10's kernel hits an illegal-instruction fault during boot and reboot-loops.

I initially mis-diagnosed this as "DHCP discovery timeout too low" and bumped
the QGA timeout from 300s to 1200s. That was wrong: the deploy isn't slow, the
VM never boots. The mis-diagnosis is preserved here intentionally — see
`feedback_check_console_first.md` in agent memory.

### Diagnosis path (for future reference)

When a Poiesis deploy fails at "Step 4/7: Discovering DHCP IP" and the VM
appears unreachable, before chasing network theories:

```
ssh root@<proxmox-node> "exec 3<>/var/run/qemu-server/<vmid>.serial; \
  timeout 15 cat <&3 > /tmp/boot.out & sleep 14; cat /tmp/boot.out"
ssh root@<proxmox-node> "qm config <vmid> | grep ^cpu"
ssh root@<proxmox-node> "cat /proc/cpuinfo | grep -m1 flags | grep -oE '(avx2|bmi[12]|fma)'"
```

If serial shows a boot loop and the host lacks AVX2/BMI/FMA, you're hitting
this bug.

### Fix Applied

Two changes:

1. **Per-deployment `cpu_type` override.** `deploy_vm.py` now reads `cpu_type`
   from the deployment JSON if present and falls back to the `vm:` config
   default only when the JSON doesn't specify. Lets the operator pin a higher
   microarchitecture for RHEL-10-family deploys without changing the global
   default for everything else.

2. **`check_node_cpu_baseline()` preflight.** New function in
   `modules/validation.py`, called from `deploy_vm.py` right after the
   VLAN-existence check (before any SFTP-heavy import). Queries
   `/nodes/{node}/status` for the CPU flags Proxmox exposes (`cpuinfo.flags`)
   and verifies the host has every flag the requested `cpu_type` needs:
   - `x86-64-v2`: `sse4_2`, `popcnt`
   - `x86-64-v2-AES`: + `aes`
   - `x86-64-v3`: + `avx2`, `bmi1`, `bmi2`, `fma`, `movbe`, `lzcnt`
   - `x86-64-v4`: + `avx512f`
   - `host` / specific CPU model names (`Haswell`, etc.): skipped — operator
     is presumed to know what they're doing.

   If the chosen node lacks any required flag, deploy fails fast with a clear
   error naming the missing flags, the node's CPU model, and a suggestion to
   either change `cpu_type` or pick a different node. **No wasted SFTP, no
   wasted disk import, no orphaned VM.**

Also reverted the bogus 1200s → 600s on the QGA timeout (and the misleading
user-facing copy that promised 20 min). 600s is the right ceiling for the
slowest *legitimate* cases (Rocky 8 first-boot `dnf upgrade` per CLAUDE.md);
RHEL 10 deploys that genuinely take longer than that have a real problem and
should fail loudly rather than burning more cycles.

### Cluster compatibility

For reference (recorded at fix time, 2026-06-19):

| Node | CPU | `x86-64-v3`? |
|---|---|---|
| proxmox01, proxmox02, proxmox03 | Intel i5-10600 / 6500T / 7500T | ✓ |
| proxmoxg03, proxmoxg04 | Xeon E5-2680 v4 (Broadwell) | ✓ |
| proxmoxb01, proxmoxb02, proxmoxb03 | Xeon E5-2640 / v2 (Sandy/Ivy Bridge) | ✗ |

RHEL 10 family (Rocky 10, Alma 10, CentOS Stream 10) deploys must target one
of the v3-capable nodes and explicitly set `"cpu_type": "x86-64-v3"` in the
deployment JSON.

---

## BUG-005 — openSUSE Leap 16 (and Ubuntu 24.04 cloud) ignore sshd_config edits

**Status:** Fixed
**Severity:** High (Leap 16 deploys fail loudly; Ubuntu 24.04 deploys *silently* don't actually enable password auth)
**Affected script:** `deploy_vm.py` / `deploy_lxc.py` → `ansible/post-deploy-vm.yml`, `ansible/post-deploy.yml`
**First observed:** 2026-06-19, test-leap16 deploy (VMID 153) failed with `"Destination /etc/ssh/sshd_config does not exist !"`. Also retrospectively explains why password-auth health checks have been flaky on Ubuntu 24.04.
**Date Added:** 2026-06-19
**Date Fixed:** 2026-06-19

### Symptom

On openSUSE Leap 16 the Ansible post-deploy fails at:

```
TASK [Allow root login via password in sshd_config] *****************************
fatal: [<ip>]: FAILED! => {"changed": false,
  "msg": "Destination /etc/ssh/sshd_config does not exist !", "rc": 257}
```

On Ubuntu 24.04 cloud the task *succeeds* but doesn't actually do anything useful:
the cloud image ships `/etc/ssh/sshd_config.d/60-cloudimg-settings.conf` containing
`PasswordAuthentication no`, and OpenSSH's drop-in semantics make the *first*
match for each directive win. Our edit to the main `/etc/ssh/sshd_config` is
discarded silently.

### Root Cause

The post-deploy playbooks used `lineinfile: path=/etc/ssh/sshd_config` to enable
`PermitRootLogin yes` and `PasswordAuthentication yes`. Two problems:

1. **Leap 16** doesn't ship `/etc/ssh/sshd_config` as a file at all — sshd is
   configured exclusively via drop-ins in `/etc/ssh/sshd_config.d/`. The
   `lineinfile` task with `create: no` (default) fails.
2. **Ubuntu 24.04 cloud images** ship a drop-in (`60-cloudimg-settings.conf`)
   that lexically precedes any change we make to the base file. Because sshd
   uses first-match-wins for these directives, our edit is shadowed.

The original BUG-001 fix worked around the symptom on Ubuntu (switched the health
check to key-based auth) but never fixed the underlying ineffective edit.

### Fix Applied

Both `ansible/post-deploy-vm.yml` and `ansible/post-deploy.yml` — replaced the
two `lineinfile` tasks with: (a) a `file: state=directory` to ensure
`/etc/ssh/sshd_config.d/` exists, then (b) a `copy:` task that writes a
low-numbered drop-in:

```yaml
- name: Write Poiesis sshd drop-in (root login + password auth)
  copy:
    dest: /etc/ssh/sshd_config.d/00-poiesis.conf
    content: |
      PermitRootLogin yes
      PasswordAuthentication yes
    mode: '0644'
```

`00-` is intentional — drop-ins are processed alphabetically, and first match
wins. `00-poiesis.conf` preempts Ubuntu cloud's `60-cloudimg-settings.conf` and
anything else the distro ships. Works uniformly on Debian/Ubuntu, RHEL family,
and openSUSE.

### Side benefit

Password auth now actually works on Ubuntu 24.04 cloud VMs, which it didn't
before despite the playbook reporting `changed=1` on every run.

---

## BUG-004 — Debian 13 (Trixie) deploys fail: `snmp-mibs-downloader` removed

**Status:** Fixed
**Severity:** High (blocks every Debian 13 deploy at Ansible "Install packages")
**Affected script:** `deploy_vm.py` / `deploy_lxc.py` → `ansible/vars/Debian.yml`
**First observed:** 2026-06-19, test-debian13 deploy (VMID 148)
**Date Added:** 2026-06-19
**Date Fixed:** 2026-06-19

### Symptom

```
TASK [Install packages] ********************************************************
fatal: [<ip>]: FAILED! => {"changed": false,
  "msg": "No package matching 'snmp-mibs-downloader' is available"}
```

VM (or LXC) is created, network up, cloud-init done — Ansible dies on package
install. Deployment is left half-finished (no DNS, no inventory, no VMID saved
to the JSON for clean decomm).

### Root Cause

`ansible/vars/Debian.yml` listed `snmp-mibs-downloader` in the standard package
set. The package was removed from the Debian 13 (Trixie) archive entirely — it
was a non-free helper that downloaded SMI MIB files from the IANA/IETF, and the
Debian SNMP maintainers removed it during the Trixie freeze. (Debian 12 and
Ubuntu still ship it, but only via non-free/multiverse, and it was never strictly
required by anything else on the system.)

### Fix Applied

`ansible/vars/Debian.yml` — removed `snmp-mibs-downloader` from the packages
list. `snmpd` and `snmp` (snmpwalk/snmpget tools) still install normally; only
the optional MIB downloader is gone. If anyone actually needs MIB files later,
they can install equivalent packages manually (`snmp-mibs` from the older
archive, or build from upstream).

---

## BUG-003 — Fedora VM deploys fail at Ansible "Install EPEL release" task

**Status:** Fixed
**Severity:** High (blocks every Fedora VM deploy at the post-deploy Ansible step)
**Affected script:** `deploy_vm.py` → `ansible/tasks/pre-install-RedHat.yml`
**First observed:** 2026-06-19, deploying a Fedora 43 VM (JEM-fedora01) via `deploy.py --deploy-file`
**Date Added:** 2026-06-19
**Date Fixed:** 2026-06-19

### Symptom

Ansible post-deploy fails on the very first OS-family pre-install task:

```
TASK [Install EPEL release] ****************************************************
fatal: [<ip>]: FAILED! => {"changed": false,
  "failures": ["No package epel-release available."],
  "msg": "Failed to install some of the specified packages", "rc": 1}
```

VM is created, SSH is up, cloud-init done, hostname set, user created, timezone set
— then Ansible dies here. DNS registration and inventory updates never run. The VM
exists in Proxmox but is not registered in Poiesis (deployment JSON written without
the VMID either, so decomm can't find it cleanly).

### Root Cause

`ansible/tasks/pre-install-RedHat.yml` runs for every host where
`ansible_os_family == 'RedHat'`. Fedora reports `RedHat` as its OS family (because it
*is* the RedHat family upstream), so it lands in this path. The first task installs
`epel-release` unconditionally — but EPEL is an *extension* to Enterprise Linux that
backports packages from Fedora's main repos. On Fedora itself, no `epel-release`
package exists, because Fedora already has what EPEL provides.

### Fix Applied

Added a `when: ansible_distribution != 'Fedora'` guard to the EPEL install task in
`ansible/tasks/pre-install-RedHat.yml`. Rocky, Alma, Oracle, CentOS Stream still
install EPEL as before; Fedora skips it. The `Update dnf cache` task continues
unconditionally — `dnf` is Fedora's native package manager, so the cache update is
valid and useful there too.

### Workaround (pre-fix)

Run the deploy, accept the Ansible failure, then manually complete DNS registration
and inventory updates. Decomm needs `vmid` added to the deployment JSON before it
can find the VM. Not recommended — fix is one line and cheap.

---

## BUG-002 — `expire.py --reap` confirmation panel shows `IP: ???`

**Status:** Fixed
**Severity:** Low (cosmetic — decomm proceeds correctly, DNS removal uses correct IP)
**Affected script:** `expire.py` → `modules/lib.py` (`confirm_destruction`)
**First observed:** 2026-03-15, during Phase 5.2 reap testing (test-expire, VMID 111)
**Date Added:** 2026-03-15
**Date Fixed:** 2026-03-15

### Symptom

The confirmation panel before decommission shows:

```
IP  : ???
```

Even though the DNS removal step immediately after uses the correct IP (`10.220.220.150`).

### Root Cause

`confirm_destruction()` in `lib.py` read only the `ip_address` key from the resource dict. Entries built by `scan_expiring()` in `expire.py` set the `ip` key (not `ip_address`) from `assigned_ip`/`ip_address` in the deployment JSON. The DNS step reads `assigned_ip` directly from the JSON file on disk, which is why it worked correctly.

### To Investigate (FIXED: See Fix Applied below)

- Check `confirm_destruction()` in `lib.py` — which key does it read for IP display?
- Check `decomm_resource()` in `lib.py` — does it set both `ip` and `ip_address` on the resource dict before calling `confirm_destruction`?
- Check `scan_expiring()` in `expire.py` — the entry sets `"ip": assigned_ip or ip_address` but does it also set `"ip_address"`?

### Fix Applied

`confirm_destruction()` in `modules/lib.py` — changed the IP lookup from:
```python
ip = deploy.get("ip_address", "???")
```
to:
```python
ip = deploy.get("ip_address") or deploy.get("ip", "???")
```
This falls back to the `ip` key if `ip_address` is not set, matching how `scan_expiring()` populates the resource dict.

---

## BUG-001 — VM Health Check: SSH fails with "Bad authentication type"

**Status:** Fixed
**Severity:** Low (cosmetic — deploy succeeds)
**Affected script:** `deploy_vm.py` → `modules/lib.py` (`health_check`)
**First observed:** 2026-03-15, during Phase 4.3 TTL testing (testvm-1d, VMID 113)
**Date Added:** 2026-03-15
**Date Fixed:** 2026-03-16

### Symptom

At the end of a successful VM deployment, the health check reports:

```
✓ TCP port 22 open on <IP>
⚠ SSH check failed: Bad authentication type; allowed types: ['publickey']
```

The deployment itself completes successfully — DNS, inventory, Ansible all worked. Only the final SSH health check fails.

### Root Cause

Ubuntu 24.04 cloud images ship `/etc/ssh/sshd_config.d/60-cloudimg-settings.conf` which enforces `PasswordAuthentication no` at the SSH daemon level. This overrides any changes the Ansible post-deploy playbook makes to `/etc/ssh/sshd_config`, so password authentication is rejected regardless of what the playbook sets. The health check was attempting password auth and failing at the protocol level.

Additionally, cloud-init only injects the SSH key for `root` — not for the additional user created during deploy — so connecting as that user with any auth method would also fail.

### Not affected

- LXC deployments (`deploy_lxc.py`) — health check passes cleanly on LXCs because SSH is bootstrapped manually via `pct exec` and password auth is explicitly configured before Ansible runs.

### To Investigate (FIXED: See Fix Applied below)

- Check what authentication method the health check in `deploy_vm.py` is using (search for the health check section near the end of `main()`).
- Verify whether `PasswordAuthentication yes` is actually taking effect — SSH into the VM manually after deploy and check `sshd_config`.
- Consider switching the health check to key-based auth for VMs (consistent with how Ansible connects to VMs).
- Alternatively, just test TCP port 22 connectivity and skip the SSH auth check entirely for VMs, since Ansible already confirmed SSH works.

### Fix Applied

`health_check()` in `modules/lib.py` — removed password-based authentication and the `ssh_key` parameter entirely. The function now always connects as `root` using the SSH agent (`allow_agent=True, look_for_keys=True`), which matches how the Proxmox node injects the deployment key for root:

```python
client.connect(ip, username="root", timeout=timeout,
               allow_agent=True, look_for_keys=True)
```

All call sites in `deploy_vm.py` and `deploy_lxc.py` updated to remove the `ssh_key=` argument.
