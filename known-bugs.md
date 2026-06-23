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
- Bug IDs are sequential and never reused. Next ID: **BUG-013**.
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

## BUG-012 — `deploy_lxc.py` bootstrap step is Debian/Ubuntu-only (ssh.service name, /etc/ssh/sshd_config edits)

**Status:** Open
**Severity:** High (blocks every LXC deploy of a non-Debian-family template — Rocky/Alma/CentOS/Fedora/openSUSE/Alpine LXCs all fail at the post-create bootstrap step before Ansible even runs)
**Affected script:** `deploy_lxc.py` (bootstrap step run after `pct create`)
**First observed:** 2026-06-23, deploying a Rocky 10 LXC on `proxmox01` after BUG-011's auto-download fix landed. The container created cleanly with the right Rocky template, then the bootstrap step printed:

```
  Enabling and starting SSH...
  Warning: pct exec failed (exit 1): Failed to enable unit: Unit ssh.service does not exist
  Allowing root SSH login...
  Warning: pct exec failed (exit 2): sed: can't read /etc/ssh/sshd_config: No such file or directory
  Setting root password...
  ✓ Bootstrap complete — SSH is ready
  Waiting for SSH to become reachable...
✗ SSH on 10.200.200.123:22 did not become reachable within 60s
```

**Date Added:** 2026-06-23

### Root Cause

The bootstrap step inside `deploy_lxc.py` (between `pct create` and the Ansible handoff) assumes the container's OS uses Debian/Ubuntu conventions:

- `pct exec <vmid> -- systemctl enable --now ssh` — fails on RHEL family (`sshd.service` not `ssh.service`), Alpine (no systemd), etc.
- `pct exec <vmid> -- sed -i ... /etc/ssh/sshd_config` — fails on RHEL 10 LXC templates that ship a minimal `/etc/ssh/sshd_config.d/`-only sshd config with no main file.

The warnings are non-fatal (we still print "✓ Bootstrap complete"), but SSH doesn't actually start, so the subsequent "Waiting for SSH" times out.

This is parallel to the multi-OS work we did for VMs:
- `ansible/vars/<OS-family>.yml` defines `ssh_service` (`sshd` on RHEL/Alpine/FreeBSD, `ssh` on Debian).
- `ansible/post-deploy*.yml` uses `sshd_config.d/00-poiesis.conf` (a drop-in) instead of editing the main file.

The LXC bootstrap step needs the same treatment — but it runs *before* Ansible (in `deploy_lxc.py` itself, via `pct exec`), so it doesn't have the OS-family vars to dispatch from. It has to detect the OS family inside the container at runtime.

### Fix shape (not yet implemented)

Three places in `deploy_lxc.py`'s bootstrap need to become OS-aware:

1. **Detect OS family inside the container** via `pct exec <vmid> -- cat /etc/os-release | grep -i 'ID_LIKE\|^ID='` once at the start of bootstrap.
2. **Service name:** map detected family to `ssh` (Debian) vs `sshd` (RHEL/Alpine/FreeBSD).
3. **SSH config:** instead of editing `/etc/ssh/sshd_config` directly, use the same drop-in pattern the post-deploy playbook uses — `mkdir -p /etc/ssh/sshd_config.d && cat > /etc/ssh/sshd_config.d/00-poiesis.conf <<EOF ... EOF`. Drop-ins work on every modern OpenSSH, regardless of whether the main file exists.

Alpine LXC also needs OpenRC handling for the service-start step (`rc-update add sshd default && rc-service sshd start` instead of `systemctl`). Same pattern as the qga snippet init-system detection.

### Workaround

Use Debian-family LXC templates only (Ubuntu, Debian) until this is fixed. VM-based deploys are unaffected — they go through cloud-init + Ansible, both of which already handle multi-OS correctly.

### Why this didn't surface earlier

The LXC pipeline was only ever exercised with Ubuntu and Debian templates (the only LXC templates Poiesis ships defaults for, and the only ones any of the example `deployments/lxc/*.json` files use). The Rocky LXC test deploys today were the first time a non-Debian family LXC template was actually attempted via Poiesis. The BUG-011 silent-substitution behavior was *masking* this bug — every LXC deploy was secretly running on Ubuntu even when the JSON asked for something else.

---


---
# FIXED Bugs / Issues
---

## BUG-011 — `deploy_lxc.py --silent` silently substitutes a different LXC template when the requested one isn't downloaded

**Status:** Fixed
**Severity:** High (silent — the deploy "succeeded" but landed an entirely different OS than the operator asked for; downstream Ansible loaded the wrong `vars/<family>.yml`, downstream cleanup tagged the wrong OS, redeploys from the JSON drifted further)
**Affected script:** `deploy_lxc.py` (silent / `--deploy-file` path)
**First observed:** 2026-06-23, attempting to deploy `test-rocky9-lxc` (with `template_name: rockylinux-9-default_20240912_amd64.tar.xz`) on `proxmoxb01` and `test-rocky10-lxc` on `proxmox01`. Neither Rocky template was downloaded on the target node. Both deploys silently substituted `ubuntu-26.04-standard_26.04-1_amd64.tar.zst` and proceeded as Ubuntu LXCs.
**Date Added:** 2026-06-23
**Date Fixed:** 2026-06-23

### Symptom

Deployment JSON specified a Rocky 10 LXC template; deploy log emitted a one-line warning, then carried on with a totally different template:

```
Warning: Template 'local:vztmpl/rockylinux-10-default_20251001_amd64.tar.xz' not found on proxmox01. Using first available.
  Template (from deployment file): ubuntu-26.04-standard_26.04-1_amd64.tar.zst
```

The deployment "succeeded" — DNS registered, inventory updated, JSON written — but the resulting LXC was Ubuntu 26.04, not Rocky 10. The deployment JSON on disk got *rewritten* to reference the Ubuntu template, hiding the original intent.

### Root Cause

In the silent / file-driven path inside `deploy_lxc.py`'s `step_template`, the template-resolution logic fell through to "first available on the node" when the configured `template_volid` wasn't present locally. A warning was printed but the deploy wasn't gated on it. The interactive wizard had always presented a picker, so this fallback only mattered in `--silent` (which is what `deploy.py --batch` uses) — and `--silent` was the only mode that ever ran from `deployments/lxc/*.json` files.

### Fix Applied

`deploy_lxc.py` `step_template` silent branch — replaced the "warn + use first available" fallback with:

1. Detect missing template by `template_volid` mismatch against the node's downloaded list.
2. Query the node's aplinfo directly via `proxmox.nodes(<node>).aplinfo.get()` for an exact filename match. **Bypass `get_lxc_repo_catalog` here** — that helper drops entries with blank descriptions (a UX-list-display filter for the interactive picker), which we discovered the hard way matters: `proxmox01`'s aplinfo had the Rocky entries with empty descriptions, and the helper was hiding them from our search.
3. If a match exists in the catalog, choose a `vztmpl`-capable storage on the node (prefer the storage from the JSON's `template_volid`, else first available) and `download_lxc_template()` + `wait_for_task()` to fetch it. Re-fetch the downloaded list and confirm the file actually landed before continuing.
4. If no catalog match, **hard fail** with a clear message naming the template, the node, and a manual `pveam download` command the operator can run to stage it.

The existing `download_lxc_template()` helper in `modules/proxmox.py` did all the heavy lifting — same call the interactive wizard's "Download from Proxmox repo..." option uses. The silent path now reuses that, gated by the catalog match.

### Validation

Validated with `deployments/lxc/test-rocky10-lxc.json` on `proxmox01` (where `rockylinux-10-default_*.tar.xz` was NOT downloaded). The deploy log showed the expected new flow:

```
Template 'rockylinux-10-default_20251001_amd64.tar.xz' not downloaded on proxmox01.
Checking Proxmox community catalog...
Auto-downloading rockylinux-10-default_20251001_amd64.tar.xz to proxmox01:local...
✓ Downloaded rockylinux-10-default_20251001_amd64.tar.xz to local
```

CT 147 was then created with the correct Rocky 10 template (verified via `pct config 147`). The deploy still failed downstream because of BUG-012 (LXC bootstrap is Debian-only), but BUG-011's specific behavior — silent OS-family substitution — is gone.

### Bonus discovery

This fix also surfaced **BUG-012** — `deploy_lxc.py`'s bootstrap step (the `pct exec` calls between `pct create` and Ansible) is Debian/Ubuntu-only and breaks on RHEL/Alpine/Suse templates. That had been masked by BUG-011 for who knows how long — every LXC was secretly Ubuntu, so the Debian-flavored bootstrap always worked.

## BUG-010 — FreeBSD VM deploys silently hang (cloud image bugs + 7 secondary issues)

**Status:** Fixed
**Severity:** High (blocked every FreeBSD deploy — VM appeared running but was unreachable, then later hung Ansible mid-playbook)
**Affected script:** `deploy_vm.py` → many — see Fix Applied list
**First observed:** 2026-06-21, test-freebsd (VMID 148) on `proxmoxb01` — VM started, MAC absent from bridge FDB, serial console silent, configured static IP never responded
**Date Added:** 2026-06-21
**Date Fixed:** 2026-06-22

### Symptom — original

Deploy completes Steps 1–3, then Step 4 (Wait for SSH on the static IP) times
out at 300s. From outside:
- VM is reported as `running` by `qm status`
- MAC is **not** in the bridge FDB — VM has sent zero frames
- Serial console produces zero bytes when probed via raw socat
- Pings to the configured static IP get no response
- `qm terminal <vmid>` *does* work and shows a fully booted FreeBSD with a
  passwordless root login on a DHCP-assigned IP that ignores the static IP
  Proxmox put in the cloud-init config

### Root Causes — seven distinct issues, layered

Resolving this required fixing **all seven** issues. The full diagnosis took a
day of iterative debugging because each fix exposed the next layer:

1. **`qm importdisk` doesn't decompress `.xz` files** — the cached image was
   `FreeBSD-...-ufs.qcow2.xz`; the VM disk ended up containing the literal
   XZ-format bytes (magic `fd 37 7a 58 5a`), SeaBIOS found no MBR, the kernel
   never booted. Confirmed via `od /dev/pve/vm-148-disk-0`.

2. **`BASIC-CLOUDINIT-*` images don't ship real cloud-init** — they ship
   `nuageinit`, FreeBSD's limited cloud-init alternative. nuageinit can't
   parse Proxmox's NoCloud-format network-config v1, so the static IP +
   password + SSH key from Proxmox cloud-init are silently ignored. FreeBSD's
   default `ifconfig_DEFAULT="SYNCDHCP"` runs dhclient, so the VM does get
   *a* DHCP-assigned IP — just not the one we configured.

3. **FreeBSD root has no password by default in cloud images, but sshd has
   `PermitRootLogin no`** — so passwordless root works on the console but not
   over SSH. Even with our SSH key installed manually, sshd rejects root.

4. **`pkg install python311` doesn't create `/usr/local/bin/python3`** — only
   `python3.11`. Ansible's interpreter-discovery fallback list looks for
   `python3` first; without the symlink, Ansible's first task (`ping` module)
   hangs the full wait_for_connection timeout (1800s).

5. **Ansible's `Wait for cloud-init first-boot to complete` task waits for
   `/run/cloud-init/result.json`** — nuageinit doesn't create that file, so
   on FreeBSD the task waited the full 1800s × 5 retries = 2.5 hours before
   failing.

6. **The post-deploy playbook used `chpasswd` to set user passwords** —
   `chpasswd` is a Linux-only utility. FreeBSD uses `pw usermod -h 0` with
   stdin-fed password.

7. **The FreeBSD package list had wrong/Linux names** — `ethtool` doesn't
   exist on FreeBSD at all, `bc` is in base (no pkg), `p7zip` was renamed to
   `7-zip` in FreeBSD ports.

### Fix Applied

Seven changes spread across six files:

| Issue | Fix |
|---|---|
| (1) .xz not decompressed | `modules/proxmox.py:import_cloud_image()` — added `xz -dkf` step before `qm importdisk` when filename ends in `.xz` |
| (2) No real cloud-init | New `modules/freebsd_firstboot.py` — drives `qm terminal` via paramiko to log in (passwordless root), then writes `/etc/rc.conf` (static IP + disable nuageinit + disable DHCP default), `/root/.ssh/authorized_keys` (chunked-write because FreeBSD's serial tty has a ~256-char input-line limit), `/etc/resolv.conf`, sets root password via `pw usermod`, restarts networking. Wired into `deploy_vm.py` between Step 3 and Step 4 when `image_filename.startswith("FreeBSD-")` |
| (3) sshd PermitRootLogin no | `freebsd_firstboot.py` also writes `/etc/ssh/sshd_config.d/00-poiesis.conf` with `PermitRootLogin yes` + `PasswordAuthentication yes` and adds the `Include` directive to the main sshd_config if missing; restarts sshd |
| (4) No python3 symlink | `freebsd_firstboot.py` also runs `pkg install -y python311 && ln -sf /usr/local/bin/python3.11 /usr/local/bin/python3` |
| (5) cloud-init wait hang | `ansible/post-deploy-vm.yml` — added `when: ansible_os_family != 'FreeBSD'` to the `Wait for cloud-init first-boot to complete` task |
| (6) chpasswd not on FreeBSD | `ansible/post-deploy-vm.yml` — split both chpasswd tasks (dad-user pw, root pw) into Linux variant (`chpasswd`) and FreeBSD variant (`echo <pw> \| pw usermod <user> -h 0`) with `when:` guards |
| (7) Wrong pkg names | `ansible/vars/FreeBSD.yml` — removed `ethtool` (no FreeBSD equivalent), removed `bc` (in base), renamed `p7zip` → `7-zip` |

### Validation

`test-freebsd` deployed clean end-to-end in 8m 00s on 2026-06-22 — full
chain: `.xz` decompression → VM creation → serial-console firstboot →
network + SSH + Python ready → Ansible post-deploy runs every task to
completion → DNS registered → inventory added.

### Lessons / further work

- The `feedback_check_console_first.md` memory got an active workout —
  every dead-end I chased on FreeBSD (UEFI requirement, x86-64-v3, sshd
  config, etc.) was a wrong hypothesis until I drove the serial console
  via paramiko and saw what was actually happening.
- Future BSD-flavored cloud images (OpenBSD, NetBSD) will likely hit
  similar issues — the chunked-write SSH key, `pw` vs `chpasswd`, and the
  cloud-init result.json skip are the most-reusable parts of the fix.
- BUG-010 has effectively replaced what I originally thought was multiple
  issues. The single bug ID captures the full investigation.

---

## BUG-009 — Alpine VM deploys fail at DHCP discovery (vendor-data snippet uses systemctl, Alpine uses OpenRC)

**Status:** Fixed
**Severity:** High (blocks every Alpine VM deploy with DHCP — VM is actually healthy, but the deploy times out and is marked failed)
**Affected script:** `deploy_vm.py` → `modules/proxmox.py` (`write_guest_agent_snippet`)
**First observed:** 2026-06-21, test-alpine (VMID 147) on `proxmoxb01` — VM booted cleanly, got DHCP-assigned IP 10.200.200.140, SSH key worked, but `deploy_vm.py` timed out waiting for guest agent because the agent service never started
**Date Added:** 2026-06-21
**Date Fixed:** 2026-06-21

### Symptom

```
─── Step 4/7: Discovering DHCP IP via guest agent ───
✗ Guest agent did not report a non-loopback IP within 600s.
```

Inspection inside the running Alpine VM (reached via the SSH key that cloud-init injected):

```
# apk info -e qemu-guest-agent
qemu-guest-agent                  <-- installed
# rc-service qemu-guest-agent status
 * status: stopped                 <-- never started
# cloud-init status --long
status: error
errors:
  - ('scripts_user', RuntimeError('Runparts: 1 failures (runcmd) in 1 attempted commands'))
```

### Root Cause

`write_guest_agent_snippet()` in `modules/proxmox.py` writes a vendor-data
cloud-init snippet that installs `qemu-guest-agent` via the cloud-init
`packages:` directive and then enables/starts it via:

```yaml
runcmd:
  - systemctl enable --now qemu-guest-agent
```

The `packages:` directive works on Alpine (cloud-init dispatches to `apk`). But `systemctl` doesn't exist on Alpine — Alpine uses **OpenRC**, not systemd. The runcmd fails, cloud-init marks `modules-final` as failed, qga is installed but never started, and `deploy_vm.py` times out waiting for the QGA-reported IP.

### Fix Applied

`modules/proxmox.py` `write_guest_agent_snippet()` — replaced the single-line `systemctl` runcmd with a shell-detected init-system block that handles every Linux init we deploy:

```bash
if command -v systemctl >/dev/null 2>&1; then
  systemctl enable --now qemu-guest-agent
elif command -v rc-update >/dev/null 2>&1; then
  rc-update add qemu-guest-agent default
  rc-service qemu-guest-agent start
elif command -v service >/dev/null 2>&1; then
  service qemu-guest-agent start || true
fi
```

systemd-based distros (Debian/RHEL/Suse/Fedora/etc.) keep using systemctl; Alpine takes the OpenRC branch; the final `service` fallback covers sysvinit-style edge cases without affecting existing deploys.

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
