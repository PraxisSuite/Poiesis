"""
Poiesis.bigip_firstboot — Drive BIG-IP first-boot via the Proxmox node's serial console.

BIG-IP appliances ship with no management IP and a default `root` / `default` password
that must be changed on first login. We can't reach the appliance over the network yet,
so we attach to its serial console from the Proxmox host that runs the VM.

Mechanism:
  1. paramiko-SSH into the Proxmox node (root, key-based auth — same path as qcow import)
  2. `qm terminal <vmid>` to attach to the VM's serial console
  3. Expect-style automation drives login → password change → tmsh config → license install

Requires: `serial0=socket` in the VM's Proxmox config (set by deploy_bigip.py at create
time) AND a BIG-IP image that emits its login prompt on ttyS0 (F5 virtual editions do
by default).
"""

import os
import re
import time
from typing import Pattern

import paramiko
from proxmoxer import ProxmoxAPI
from rich.console import Console

from modules.proxmox import node_ssh_host

console = Console()

# Boot can be slow (cloud image first-boot + cloud-init + BIG-IP services + license preload).
# 5 minutes is generous but not unreasonable.
LOGIN_TIMEOUT = 360

# A licensed BIG-IP shell prompt looks like:
#   [root@localhost:NO LICENSE:Standalone] config #
#   [root@<hostname>:Active:Standalone] config #
# We anchor on " config # " which is stable across license/hostname state.
SHELL_PROMPT = re.compile(r"\bconfig\s*#\s*$")


class SerialExpect:
    """Minimal expect-style wrapper around a paramiko channel.

    Reads accumulate in a buffer; read_until() searches the buffer for a pattern and
    consumes everything up to and including the match.
    """

    def __init__(self, channel: paramiko.Channel, log_sink=None, redact: list[str] | None = None):
        self.channel = channel
        self.buffer = ""
        self.log = log_sink
        self.redact = list(redact or [])

    def _log(self, data: str) -> None:
        if not self.log:
            return
        cleaned = data
        for secret in self.redact:
            if secret:
                cleaned = cleaned.replace(secret, "***")
        self.log.write(cleaned)
        try:
            self.log.flush()
        except Exception:
            pass

    def read_until(self, pattern: str | Pattern, timeout: int = 60) -> str:
        """Read from the channel until `pattern` is found in the buffer, or raise TimeoutError."""
        compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.channel.recv_ready():
                chunk = self.channel.recv(4096).decode("utf-8", errors="replace")
                if chunk:
                    self.buffer += chunk
                    self._log(chunk)
                    m = compiled.search(self.buffer)
                    if m:
                        consumed = self.buffer[: m.end()]
                        self.buffer = self.buffer[m.end():]
                        return consumed
            elif self.channel.exit_status_ready():
                # Channel closed before we found the pattern
                tail = self.buffer[-500:]
                raise TimeoutError(
                    f"Channel closed before pattern {pattern!r} matched. Last 500 bytes: {tail!r}"
                )
            else:
                time.sleep(0.1)
        tail = self.buffer[-500:]
        raise TimeoutError(f"Timed out after {timeout}s waiting for {pattern!r}. Last 500 bytes: {tail!r}")

    def send_line(self, line: str) -> None:
        """Send a string followed by \\r. (BIG-IP console expects CR, not LF.)"""
        self.channel.send((line + "\r").encode("utf-8"))

    def send_control(self, ctrl_char: str) -> None:
        """Send a Ctrl+X character (e.g., 'O' for Ctrl+O to exit qm terminal)."""
        code = ord(ctrl_char.upper()) - ord("A") + 1
        self.channel.send(bytes([code]))


def _wait_for_mcpd(ex: "SerialExpect", label: str = "", max_wait_seconds: int = 1800,
                   poll_interval_seconds: int = 60,
                   required_consecutive_healthy: int = 2) -> bool:
    """Poll `tmsh show sys mcp-state` until mcpd reports a stable, usable state (or timeout).

    BIG-IP first boot + license operations can leave mcpd unavailable for a while:
    typical 5–15 minutes after password change, up to 30 minutes worst case. We poll once
    per minute and require N consecutive healthy responses to guard against a fleeting
    "up" right before another restart.

    A tmsh command issued while mcpd is unavailable returns:
        Cannot connect to mcpd. Your preferences and aliases will not be available until
        it comes back up.

    Returns True if mcpd became stably healthy, False if the timeout was hit.
    """
    label = f" ({label})" if label else ""
    console.print(
        f"  [dim]Waiting for mcpd to be stably running{label} — "
        f"polling every {poll_interval_seconds}s, max {max_wait_seconds // 60} min, "
        f"need {required_consecutive_healthy} consecutive healthy checks...[/dim]"
    )
    mcpd_lost = re.compile(r"(Cannot connect to mcpd|connection to mcpd has been lost)", re.IGNORECASE)
    deadline = time.time() + max_wait_seconds
    consecutive_healthy = 0
    elapsed = 0
    while time.time() < deadline:
        time.sleep(poll_interval_seconds)
        elapsed += poll_interval_seconds
        ex.send_line("tmsh show sys mcp-state field-fmt | head -20")
        try:
            out = ex.read_until(SHELL_PROMPT, timeout=30)
        except TimeoutError:
            consecutive_healthy = 0
            console.print(f"  [dim]  no response at {elapsed}s, resetting streak...[/dim]")
            continue
        if mcpd_lost.search(out):
            consecutive_healthy = 0
            console.print(f"  [dim]  mcpd not yet ready ({elapsed}s elapsed)[/dim]")
            continue
        # mcpd answered. Look for an explicit "running" / "success" signal; otherwise we
        # treat any non-mcpd-lost response as healthy too.
        healthy = (
            ("running-phase" in out and "running" in out)
            or ("last-update-status" in out and "success" in out)
            or True  # any non-error response means mcpd is answering tmsh queries
        )
        if healthy:
            consecutive_healthy += 1
            console.print(
                f"  [dim]  mcpd healthy {consecutive_healthy}/{required_consecutive_healthy} "
                f"({elapsed}s elapsed)[/dim]"
            )
            if consecutive_healthy >= required_consecutive_healthy:
                console.print(f"  [green]✓ mcpd is stably running after {elapsed}s[/green]")
                return True
    return False


def _open_proxmox_shell(cfg: dict, node_name: str) -> tuple[paramiko.SSHClient, paramiko.Channel]:
    """SSH to the Proxmox host and return (client, interactive-shell-channel)."""
    pve = cfg["proxmox"]
    ssh_host = node_ssh_host(cfg, node_name)
    ssh_key = os.path.expanduser(pve.get("ssh_key", "~/.ssh/id_rsa"))

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(ssh_host, username="root", key_filename=ssh_key, timeout=30)
    except paramiko.AuthenticationException as e:
        raise RuntimeError(
            f"SSH key auth to {ssh_host} failed. Ensure {ssh_key} is authorized on the node."
        ) from e

    channel = client.invoke_shell(term="xterm", width=200, height=50)
    channel.settimeout(None)
    # Drain any login banner / motd before we start expecting things
    time.sleep(0.5)
    while channel.recv_ready():
        channel.recv(4096)
    return client, channel


def firstboot_configure(cfg: dict, proxmox: ProxmoxAPI, node_name: str, vmid: int,
                        new_password: str, mgmt_ip: str, mgmt_prefix_len: int,
                        mgmt_gateway: str, registration_key: str,
                        dns_servers: list[str] | None = None,
                        ntp_servers: list[str] | None = None,
                        log_path: str | None = None) -> None:
    """Drive a BIG-IP through first-boot: change password, set mgmt IP+DNS+NTP, install license.

    Raises RuntimeError with a descriptive message on any failure. Caller (deploy_bigip.py)
    is expected to surface the error and leave the VM running for human investigation.
    """
    # DNS + NTP are OPTIONAL — pass None/empty to leave the BIG-IP's defaults / DHCP
    # settings in place. Many environments don't permit external resolvers and BIG-IP
    # already has a way to reach activate.f5.com in those cases.
    dns_servers = list(dns_servers) if dns_servers else []
    ntp_servers = list(ntp_servers) if ntp_servers else []

    log_sink = open(log_path, "w") if log_path else None
    redact = [new_password, registration_key, "default"]

    client, channel = _open_proxmox_shell(cfg, node_name)
    try:
        ex = SerialExpect(channel, log_sink=log_sink, redact=redact)

        console.print(f"  [dim]Attaching to serial console of VM {vmid} on {node_name}...[/dim]")
        ex.send_line(f"qm terminal {vmid}")
        # qm terminal prints: "starting serial terminal on interface serial0 (press Ctrl+O to exit)"
        ex.read_until(r"press Ctrl\+O to exit|escape character", timeout=15)

        # Newline kicks the BIG-IP into showing a fresh prompt if it's mid-boot or idle
        ex.send_line("")

        console.print(f"  [dim]Waiting for BIG-IP login prompt (up to {LOGIN_TIMEOUT}s for first boot)...[/dim]")
        ex.read_until(r"localhost login:\s*$", timeout=LOGIN_TIMEOUT)

        console.print(f"  [dim]Logging in as root with default password...[/dim]")
        ex.send_line("root")
        ex.read_until(r"Password:\s*$", timeout=15)
        ex.send_line("default")

        # Forced password change flow
        ex.read_until(r"\(current\) BIG-IP password:\s*$", timeout=30)
        ex.send_line("default")
        ex.read_until(r"New BIG-IP password:\s*$", timeout=15)
        ex.send_line(new_password)
        ex.read_until(r"Retype new BIG-IP password:\s*$", timeout=15)
        ex.send_line(new_password)

        # Land at the shell prompt
        ex.read_until(SHELL_PROMPT, timeout=30)
        console.print(f"  [green]✓ Root password changed[/green]")

        # Wait for mcpd to be healthy BEFORE issuing any tmsh — first boot can leave mcpd
        # still spinning up for several minutes after the login prompt appears.
        if not _wait_for_mcpd(ex, label="post-login"):
            raise RuntimeError(
                "mcpd never reached a healthy state within 15 min of login. The BIG-IP is "
                "still booting or stuck — investigate manually via the Proxmox console."
            )

        # Explicitly set admin password to match (BIG-IP normally syncs this, but be defensive)
        ex.send_line("passwd admin")
        # 'passwd admin' on BIG-IP uses standard Linux prompts
        ex.read_until(r"(New password:|password:)\s*$", timeout=15)
        ex.send_line(new_password)
        ex.read_until(r"(Retype new password:|password:)\s*$", timeout=15)
        ex.send_line(new_password)
        ex.read_until(SHELL_PROMPT, timeout=30)
        console.print(f"  [green]✓ Admin password set[/green]")

        # ── Configure management IP ──────────────────────────────────────────────
        # BIG-IP defaults to DHCP on mgmt; disable it first, then assign static.
        console.print(f"  [dim]Configuring management interface ({mgmt_ip}/{mgmt_prefix_len}, gw {mgmt_gateway})...[/dim]")
        ex.send_line("tmsh modify sys global-settings mgmt-dhcp disabled")
        ex.read_until(SHELL_PROMPT, timeout=30)

        # delete may fail with "not found" if no mgmt-ip exists yet — non-fatal, ignore
        ex.send_line("tmsh delete sys management-ip all 2>/dev/null; echo TMSH_DONE")
        ex.read_until(r"TMSH_DONE", timeout=30)
        ex.read_until(SHELL_PROMPT, timeout=15)

        ex.send_line(f"tmsh create sys management-ip {mgmt_ip}/{mgmt_prefix_len}")
        ex.read_until(SHELL_PROMPT, timeout=30)

        if mgmt_gateway:
            ex.send_line("tmsh delete sys management-route all 2>/dev/null; echo TMSH_DONE")
            ex.read_until(r"TMSH_DONE", timeout=30)
            ex.read_until(SHELL_PROMPT, timeout=15)
            ex.send_line(f"tmsh create sys management-route default gateway {mgmt_gateway}")
            ex.read_until(SHELL_PROMPT, timeout=30)

        ex.send_line("tmsh save sys config")
        # save can take 10-30s
        ex.read_until(SHELL_PROMPT, timeout=120)
        console.print(f"  [green]✓ Management interface configured[/green]")

        # ── Optional: DNS + NTP ──────────────────────────────────────────────────
        # Only touch these if the operator explicitly set them in the deployment file.
        # In many environments BIG-IP picks up DNS/NTP from elsewhere or uses internal
        # resolvers; we don't want to override that silently.
        if dns_servers:
            console.print(f"  [dim]Configuring DNS ({', '.join(dns_servers)})...[/dim]")
            ex.send_line("tmsh delete sys dns name-servers all 2>/dev/null; echo TMSH_DONE")
            ex.read_until(r"TMSH_DONE", timeout=30)
            ex.read_until(SHELL_PROMPT, timeout=15)
            dns_list = " ".join(dns_servers)
            ex.send_line(f"tmsh modify sys dns name-servers add {{ {dns_list} }}")
            ex.read_until(SHELL_PROMPT, timeout=30)

        if ntp_servers:
            console.print(f"  [dim]Configuring NTP ({', '.join(ntp_servers)})...[/dim]")
            ex.send_line("tmsh delete sys ntp servers all 2>/dev/null; echo TMSH_DONE")
            ex.read_until(r"TMSH_DONE", timeout=30)
            ex.read_until(SHELL_PROMPT, timeout=15)
            ntp_list = " ".join(ntp_servers)
            ex.send_line(f"tmsh modify sys ntp servers add {{ {ntp_list} }}")
            ex.read_until(SHELL_PROMPT, timeout=30)

        if dns_servers or ntp_servers:
            ex.send_line("tmsh save sys config")
            ex.read_until(SHELL_PROMPT, timeout=120)
            # tmsh save can bump mcpd — make sure it's settled before continuing.
            if not _wait_for_mcpd(ex, label="post-dns/ntp"):
                raise RuntimeError(
                    "mcpd did not recover after DNS/NTP config save. Investigate manually."
                )
            console.print(f"  [green]✓ DNS/NTP configured[/green]")

        # ── Install + activate license ───────────────────────────────────────────
        # Wait one more time for mcpd to be healthy right before install — license install
        # is the most sensitive operation. We've seen `tmsh save` push mcpd back to busy,
        # and a rejected install is silent unless we detect the mcpd error in the output.
        if not _wait_for_mcpd(ex, label="pre-license"):
            raise RuntimeError(
                "mcpd did not stabilize before license install. Aborting to avoid a silent "
                "rejected install. Investigate manually."
            )
        console.print(f"  [dim]Installing registration key and activating license (calls F5 activation server)...[/dim]")
        ex.send_line(f"tmsh install sys license registration-key {registration_key}")
        # License install talks to activate.f5.com; can take 30-90s. On failure it prints
        # an error containing "Can not" / "Could not" / "rejected" / "failed".
        try:
            output = ex.read_until(SHELL_PROMPT, timeout=300)
        except TimeoutError as e:
            raise RuntimeError(
                "License activation did not complete within 5 minutes. The VM is still "
                "running — investigate manually via the Proxmox console. "
                f"Last 500 bytes: {e!s}"
            ) from None

        # Failure detection. Special case: "Cannot connect to mcpd" in install output means
        # the install command was REJECTED before doing anything (mcpd was busy when we ran
        # it). We pre-check mcpd above, but if it goes busy mid-command (e.g. another config
        # push), we must detect this and fail loudly — otherwise the script "succeeds" with
        # no license installed.
        if re.search(r"Cannot connect to mcpd|connection to mcpd has been lost", output, re.IGNORECASE):
            tail = output[-1500:]
            raise RuntimeError(
                "License install was REJECTED — mcpd became unavailable during the command. "
                "The license was NOT installed. The VM is still running. Re-run deploy after "
                f"investigating mcpd stability. Output:\n{tail}"
            )
        install_failure_markers = re.compile(
            r"(license.*(rejected|invalid|denied|expired)|"
            r"registration.*(rejected|invalid|denied)|"
            r"could not (verify|process|activate|resolve|connect)|"
            r"unable to (activate|install|resolve|connect)|"
            r"no route to host|connection refused|connection timed out|"
            r"name resolution|temporary failure)",
            re.IGNORECASE,
        )
        if install_failure_markers.search(output):
            tail = output[-1500:]
            raise RuntimeError(
                "License activation FAILED. The VM is still running — investigate manually. "
                f"Activation output (last 1500 chars):\n{tail}"
            )
        # Echo the last bit of install output for debugging — useful even on success because
        # F5 can return informational messages we want to see.
        install_tail = output[-300:].strip()
        if install_tail:
            console.print(f"  [dim]install output tail: {install_tail!r}[/dim]")

        # Verify license is active — but the license install restarts mcpd. Poll mcp-state
        # first as the clean recovery signal, then verify the license once.
        if not _wait_for_mcpd(ex, label="post-activation"):
            raise RuntimeError(
                "License install command completed, but mcpd did not recover within 15 minutes. "
                "The license is likely installed correctly — log in to the BIG-IP and run "
                "`tmsh show /sys license` to confirm."
            )
        ex.send_line("tmsh show /sys license | head -20")
        verify = ex.read_until(SHELL_PROMPT, timeout=30)
        if "Licensed Version" not in verify and "Active Modules" not in verify:
            tail = verify[-500:]
            raise RuntimeError(
                "mcpd is up but `tmsh show /sys license` does not report a licensed state. "
                f"Investigate manually. Output:\n{tail}"
            )
        console.print(f"  [green]✓ License activated[/green]")

        # Final save
        ex.send_line("tmsh save sys config")
        ex.read_until(SHELL_PROMPT, timeout=120)
    finally:
        # Always try to exit qm terminal cleanly with Ctrl+O before tearing down the SSH
        # session — otherwise the qm terminal subprocess on the Proxmox node stays running,
        # holds the VM's serial socket, and prevents future deploys/decomms from attaching.
        try:
            ex.send_control("O")
            time.sleep(1)
        except Exception:
            pass
        try:
            channel.close()
        except Exception:
            pass
        client.close()
        if log_sink:
            log_sink.close()


def revoke_license(cfg: dict, proxmox: ProxmoxAPI, node_name: str, vmid: int,
                   password: str | None = None, log_path: str | None = None) -> None:
    """Drive a running BIG-IP through `tmsh revoke sys license` via the serial console.

    Used by decomm_bigip.py before VM destruction. Raises RuntimeError on failure;
    caller is expected to abort the decomm and leave the VM running for investigation.

    `password` is the post-firstboot root password (stored in the deployment JSON).
    Required if the serial session has logged out since deploy. If not provided and the
    system is at a login prompt, the call will fail.

    We use the serial console (not SSH to the mgmt IP) because:
      - It works whether or not the deploying host can route to the mgmt VLAN
      - Same mechanism as the deploy-side firstboot for symmetry
    """
    log_sink = open(log_path, "w") if log_path else None
    redact = [password] if password else []

    client, channel = _open_proxmox_shell(cfg, node_name)
    try:
        ex = SerialExpect(channel, log_sink=log_sink, redact=redact)

        console.print(f"  [dim]Attaching to serial console of VM {vmid} on {node_name}...[/dim]")
        ex.send_line(f"qm terminal {vmid}")
        ex.read_until(r"press Ctrl\+O to exit|escape character", timeout=15)

        # Defensive: a prior failed decomm may have left the BIG-IP at an unanswered prompt
        # (e.g. tmsh's "Are you sure? Y/N:"). Send Ctrl-C to cancel any pending command, then
        # CRs to redraw a fresh prompt or login banner. Probe with a loose pattern so the
        # match works for any hostname or trailing whitespace state.
        time.sleep(1.0)
        ex.send_control("C")
        time.sleep(0.5)
        ex.send_line("")
        time.sleep(0.5)
        ex.send_line("")
        try:
            seen = ex.read_until(
                re.compile(r"\S+ login:\s*$|\]\s*config\s*#", re.MULTILINE),
                timeout=60,
            )
        except TimeoutError as e:
            raise RuntimeError(
                "Could not detect login prompt or shell on the serial console. The VM may be "
                f"hung or rebooting. Investigate manually before retrying. {e}"
            ) from None

        if "login:" in seen:
            if not password:
                raise RuntimeError(
                    "BIG-IP is at a login prompt and no password was provided. Either pass the "
                    "post-firstboot password (from the deployment JSON) or log in manually, run "
                    "`tmsh revoke sys license`, then re-run decomm with --force-decomm."
                )
            console.print(f"  [dim]Logging in as root...[/dim]")
            ex.send_line("root")
            ex.read_until(r"Password:\s*$", timeout=15)
            ex.send_line(password)
            ex.read_until(SHELL_PROMPT, timeout=30)

        # Wait for mcpd to be stable before issuing tmsh revoke — same reasoning as deploy.
        if not _wait_for_mcpd(ex, label="pre-revoke"):
            raise RuntimeError(
                "mcpd did not stabilize within 30 min before revoke. Investigate manually."
            )

        console.print(f"  [dim]Revoking license via tmsh...[/dim]")
        ex.send_line("tmsh revoke sys license")
        # `tmsh revoke sys license` prompts: "Are you sure? Y/N:" — answer Y.
        try:
            ex.read_until(r"Are you sure\?\s*Y/N\s*:\s*$", timeout=60)
            ex.send_line("Y")
        except TimeoutError:
            # Some BIG-IP versions / contexts may revoke non-interactively. Continue and let
            # the next read_until either succeed or surface the real error.
            console.print(f"  [dim](no Y/N prompt — assuming non-interactive revoke)[/dim]")
        try:
            output = ex.read_until(SHELL_PROMPT, timeout=300)
        except TimeoutError as e:
            raise RuntimeError(
                "License revocation did not complete within 5 minutes. The VM is still running "
                "— investigate manually before retrying decomm. "
                f"{e}"
            ) from None

        # mcpd-restart messaging during license-change is normal; filter it out of failure detection.
        revoke_failure_markers = re.compile(
            r"(revoke.*(rejected|invalid|denied|failed)|"
            r"could not (revoke|process)|"
            r"unable to (revoke|deactivate))",
            re.IGNORECASE,
        )
        if revoke_failure_markers.search(output):
            tail = output[-1000:]
            raise RuntimeError(
                "License revocation FAILED. The VM is still running — investigate manually "
                f"before retrying. Revoke output (last 1000 chars):\n{tail}"
            )

        # Revoke also restarts mcpd. Poll mcp-state first, then verify revocation once.
        if not _wait_for_mcpd(ex, label="post-revocation"):
            raise RuntimeError(
                "License revoke command completed but mcpd did not recover within 15 minutes. "
                "The license is likely revoked correctly — log in and run `tmsh show /sys license` "
                "to confirm. If it shows no license, retry decomm with --force-decomm."
            )
        ex.send_line("tmsh show /sys license | head -10")
        verify = ex.read_until(SHELL_PROMPT, timeout=30)
        # When unlicensed, BIG-IP omits the "Licensed Version" line (or shows REVOKED).
        if "Licensed Version" in verify and "REVOKED" not in verify.upper():
            tail = verify[-500:]
            raise RuntimeError(
                "mcpd is up but `tmsh show /sys license` still reports a license. Investigate "
                f"manually. Output:\n{tail}"
            )
        console.print(f"  [green]✓ License revoked[/green]")
    finally:
        # Always try to exit qm terminal cleanly with Ctrl+O — otherwise the qm terminal
        # subprocess on the Proxmox node stays running and holds the VM's serial socket.
        try:
            ex.send_control("O")
            time.sleep(1)
        except Exception:
            pass
        try:
            channel.close()
        except Exception:
            pass
        client.close()
        if log_sink:
            log_sink.close()
