"""
Poiesis.freebsd_firstboot — Configure a freshly-booted FreeBSD cloud-init VM via
the Proxmox node's serial console.

The FreeBSD project's `BASIC-CLOUDINIT-*` qcow images ship with `nuageinit`
(FreeBSD's limited cloud-init alternative), not real `cloud-init`. nuageinit
does NOT honor Proxmox's NoCloud-format static IP, password, or SSH key
settings — so a fresh FreeBSD VM boots, gets a DHCP lease (FreeBSD default),
and has no password and no SSH key.

This module connects to the VM's serial console (passwordless root login,
which is the FreeBSD cloud image default), then writes /etc/rc.conf, sets
the root password, installs the deploy SSH key, and restarts networking —
basically doing by hand what nuageinit *should* have done from Proxmox's
cloud-init seed.

After this returns, deploy_vm.py continues normally: wait for SSH on the
configured static IP, run Ansible, register DNS, etc.

Mechanism mirrors `modules.bigip_firstboot`:
  1. paramiko-SSH into the Proxmox node (root, key-based auth)
  2. `qm terminal <vmid>` to attach to the VM's serial console
  3. Expect-style automation logs in (no password) and runs a small script
"""

import os
import re
import time
from typing import Pattern

import paramiko
from rich.console import Console

from modules.proxmox import node_ssh_host

console = Console()

# Boot + dhclient + getty can take 1–2 minutes on first boot.
LOGIN_TIMEOUT = 240

# FreeBSD's default root prompt — matches `root@<hostname>:<cwd> # `
ROOT_PROMPT = re.compile(r"root@[^\s]+:[^#\n]*#\s*$")
LOGIN_PROMPT = re.compile(r"login:\s*$", re.IGNORECASE)


class SerialExpect:
    """Minimal expect-style wrapper around a paramiko channel (same shape as
    the one in bigip_firstboot, kept separate so each module owns its prompts
    without coupling to the other)."""

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
                tail = self.buffer[-500:]
                raise TimeoutError(
                    f"Channel closed before pattern {pattern!r} matched. Last 500 bytes: {tail!r}"
                )
            else:
                time.sleep(0.1)
        tail = self.buffer[-500:]
        raise TimeoutError(
            f"Timed out after {timeout}s waiting for {pattern!r}. Last 500 bytes: {tail!r}"
        )

    def send_line(self, line: str) -> None:
        """Send a string followed by carriage return."""
        self.channel.send((line + "\r").encode("utf-8"))

    def run_command(self, cmd: str, timeout: int = 30) -> str:
        """Send a shell command and read until the next prompt. Returns captured output."""
        self.send_line(cmd)
        return self.read_until(ROOT_PROMPT, timeout=timeout)


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
    time.sleep(0.5)
    while channel.recv_ready():
        channel.recv(4096)
    return client, channel


def configure_via_serial(cfg: dict, node_name: str, vmid: int,
                         hostname: str, ip: str, prefix_len: str, gateway: str,
                         dns_servers: list[str], password: str,
                         ssh_pub_key: str,
                         log_path: str | None = None) -> None:
    """Drive the FreeBSD VM's serial console to:
      - log in as root (passwordless — FreeBSD cloud image default)
      - write /etc/rc.conf for static IP, replacing the default DHCP entry
      - stop dhclient and restart netif so the static IP takes effect
      - set root password from the deployment file (so subsequent flows match
        what other distros do)
      - install the deploy SSH public key into /root/.ssh/authorized_keys
        (needed because Proxmox's cloud-init delivers it but nuageinit ignores)
      - configure /etc/resolv.conf with our DNS servers (nuageinit doesn't)

    Raises RuntimeError on any failure. Caller leaves the VM running so the
    operator can investigate via `qm terminal` directly.

    ip / prefix_len / gateway are required (FreeBSD requires static IP via
    this path — DHCP is not supported by Poiesis for FreeBSD yet because the
    image doesn't ship qemu-guest-agent for IP discovery).
    """
    if not ip or ip == "dhcp" or not prefix_len or not gateway:
        raise RuntimeError(
            "FreeBSD deploys require a static IP (ip_address, prefix_len, gateway). "
            "DHCP isn't supported yet because the FreeBSD cloud image doesn't ship "
            "qemu-guest-agent, so Poiesis has no way to discover the DHCP-assigned IP."
        )

    log_sink = open(log_path, "w") if log_path else None
    redact = [password] if password else []

    client, channel = _open_proxmox_shell(cfg, node_name)
    try:
        ex = SerialExpect(channel, log_sink=log_sink, redact=redact)

        console.print(f"  [dim]Attaching to serial console of VM {vmid} on {node_name}...[/dim]")
        ex.send_line(f"qm terminal {vmid}")
        ex.read_until(r"press Ctrl\+O to exit|escape character", timeout=15)

        # Nudge the console — fresh boots may need a couple of newlines
        ex.send_line("")
        time.sleep(1)
        ex.send_line("")

        console.print(
            f"  [dim]Waiting for FreeBSD root prompt (up to {LOGIN_TIMEOUT}s for first boot)...[/dim]"
        )
        # Try to land on the root prompt directly (cloud image default — no
        # password, getty pre-authenticated). If we hit `login:` instead, send
        # `root` and try again.
        try:
            ex.read_until(ROOT_PROMPT, timeout=LOGIN_TIMEOUT)
        except TimeoutError:
            ex.send_line("root")
            ex.read_until(ROOT_PROMPT, timeout=30)

        console.print(f"  [dim]Setting hostname and root password...[/dim]")
        ex.run_command(f"hostname {hostname}")
        ex.run_command(f"sysrc hostname={hostname}")
        # `chpass -p '*'` would lock; we want to actually set a hashed pw.
        # `echo '<password>' | pw usermod root -h 0` is the canonical FreeBSD form.
        ex.send_line(f"echo '{password}' | pw usermod root -h 0")
        ex.read_until(ROOT_PROMPT, timeout=10)

        console.print(f"  [dim]Installing deploy SSH key for root...[/dim]")
        ex.run_command("mkdir -p /root/.ssh && chmod 700 /root/.ssh")
        # FreeBSD's serial getty tty has a kernel input-line buffer limit
        # (MAX_CANON ≈ 256 chars) that silently truncates anything longer.
        # SSH keys are ~370 chars on one line, so neither a single printf
        # nor a here-doc (which reads each line as input) works. Workaround:
        # write the key out in <200-char chunks using `echo -n` + append.
        # Final `echo` (no -n) adds the trailing newline.
        ex.run_command("rm -f /root/.ssh/authorized_keys")
        key_body = ssh_pub_key.strip().replace("\r", "").replace("\n", " ")
        chunk_size = 180
        first = True
        for i in range(0, len(key_body), chunk_size):
            chunk = key_body[i:i + chunk_size]
            redirect = ">" if first else ">>"
            # Single-quote-wrap; SSH key bodies don't contain single quotes.
            ex.run_command(
                f"echo -n '{chunk}' {redirect} /root/.ssh/authorized_keys",
                timeout=10,
            )
            first = False
        # Append final newline so authorized_keys is a proper line-terminated file.
        ex.run_command("echo '' >> /root/.ssh/authorized_keys")
        ex.run_command("chmod 600 /root/.ssh/authorized_keys")

        # FreeBSD's stock sshd_config has `PermitRootLogin no` — even with the
        # right authorized_keys, root SSH is refused until we explicitly enable
        # it. Same drop-in approach the Linux post-deploy playbook uses (low-
        # numbered file so it preempts any later drop-in shipped by the image).
        # We also enable PasswordAuthentication so the Ansible playbook's own
        # `lineinfile` edits later (if it tries to write to sshd_config_d_path)
        # don't lock us out.
        console.print(f"  [dim]Enabling root SSH via /etc/ssh/sshd_config.d drop-in...[/dim]")
        ex.run_command("mkdir -p /etc/ssh/sshd_config.d")
        ex.send_line("cat > /etc/ssh/sshd_config.d/00-poiesis.conf <<'POIESIS_EOF'")
        ex.send_line("# Managed by Poiesis firstboot — enables root SSH for the")
        ex.send_line("# Ansible post-deploy phase to run. Tightened later by the")
        ex.send_line("# operator if root SSH isn't desired long-term.")
        ex.send_line("PermitRootLogin yes")
        ex.send_line("PasswordAuthentication yes")
        ex.send_line("POIESIS_EOF")
        ex.read_until(ROOT_PROMPT, timeout=10)
        # FreeBSD's default sshd_config may not Include the drop-in directory.
        # Add the Include directive idempotently if it isn't there yet.
        ex.run_command(
            "grep -q '^Include /etc/ssh/sshd_config.d/' /etc/ssh/sshd_config "
            "|| echo 'Include /etc/ssh/sshd_config.d/*.conf' >> /etc/ssh/sshd_config"
        )
        ex.run_command("service sshd restart", timeout=15)

        console.print(f"  [dim]Writing static IP {ip}/{prefix_len} gw {gateway} to /etc/rc.conf...[/dim]")
        # sysrc edits /etc/rc.conf safely (handles existing entries).
        ex.run_command(f'sysrc ifconfig_vtnet0="inet {ip}/{prefix_len}"')
        # Remove the SYNCDHCP default so dhclient won't fight us.
        ex.run_command("sysrc -x ifconfig_DEFAULT || true")
        ex.run_command(f"sysrc defaultrouter={gateway}")
        # Don't run nuageinit again — it might re-add the DHCP default.
        ex.run_command('sysrc nuageinit_enable="NO"')

        if dns_servers:
            console.print(f"  [dim]Writing /etc/resolv.conf with DNS servers {', '.join(dns_servers)}...[/dim]")
            ex.send_line("cat > /etc/resolv.conf <<'POIESIS_EOF'")
            for s in dns_servers:
                ex.send_line(f"nameserver {s}")
            ex.send_line("POIESIS_EOF")
            ex.read_until(ROOT_PROMPT, timeout=10)

        console.print(f"  [dim]Stopping dhclient and restarting networking...[/dim]")
        # killall is safer than `service dhclient stop` (works whether or not
        # /etc/rc.d/dhclient knows about the running instance).
        ex.run_command("killall dhclient 2>/dev/null; sleep 1; true")
        # `service netif restart` reads /etc/rc.conf and applies the new ifconfig_vtnet0.
        # `service routing restart` re-installs the default route.
        ex.send_line("service netif restart && service routing restart")
        ex.read_until(ROOT_PROMPT, timeout=30)

        # Confirm the IP took.
        console.print(f"  [dim]Verifying static IP applied...[/dim]")
        out = ex.run_command("ifconfig vtnet0 | grep 'inet '", timeout=10)
        if ip not in out:
            raise RuntimeError(
                f"Static IP {ip} did not apply on vtnet0. ifconfig output:\n{out[-500:]}"
            )

        # FreeBSD's base system has no Python — Ansible's first task (`ping`
        # module) copies a small Python script to the target and runs it,
        # which fails without an interpreter. Install python311 from pkg here
        # so the handoff to Ansible succeeds. Ansible's interpreter-discovery
        # will auto-find /usr/local/bin/python3.11.
        console.print(f"  [dim]Installing python311 from pkg for Ansible to use (~1–2 min)...[/dim]")
        # pkg bootstrap is idempotent — no-op if already installed.
        ex.send_line("ASSUME_ALWAYS_YES=YES pkg bootstrap")
        ex.read_until(ROOT_PROMPT, timeout=120)
        ex.send_line("pkg install -y python311")
        ex.read_until(ROOT_PROMPT, timeout=600)
        # `pkg install python311` does NOT create /usr/local/bin/python3 — only
        # /usr/local/bin/python3.11. Ansible's interpreter-discovery fallback
        # list looks for `python3` first and gives up if not found, so we add
        # the symlink ourselves. Idempotent: `-f` overwrites any stale link.
        ex.run_command("ln -sf /usr/local/bin/python3.11 /usr/local/bin/python3")
        # Sanity-check Python is callable via the discovery path.
        out = ex.run_command("which python3", timeout=10)
        if "/usr/local/bin/python3" not in out:
            raise RuntimeError(
                f"Failed to install Python on FreeBSD VM. `which python3` output:\n{out[-300:]}"
            )

        console.print(f"  [green]✓ FreeBSD configured: {hostname} at {ip}[/green]")

        # Detach from the serial console cleanly (Ctrl+O exits qm terminal).
        channel.send(b"\x0f")
        time.sleep(1)
    finally:
        channel.close()
        client.close()
        if log_sink:
            log_sink.close()
