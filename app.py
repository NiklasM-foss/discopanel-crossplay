"""
DiscoPanel Crossplay Provisioner
================================
A small web app that provisions a ready-to-run Bedrock<->Java crossplay
Minecraft server on a DiscoPanel instance from a single web form.

It is meant to run ON the DiscoPanel host (as a systemd service), so it talks
to the DiscoPanel API on localhost and writes plugin/extension files straight
into the server data directory - no SSH required.

Per created server it:
  * creates a Paper server (chosen MC version, 4 GB RAM)
  * publishes it through the DiscoPanel proxy (custom hostname + base domain)
  * installs the newest crossplay plugin stack (ViaVersion, ViaBackwards,
    ViaRewind, SkinsRestorer, Geyser, Floodgate) plus a small admin stack
    (EssentialsX, VaultUnlocked, LuckPerms, AntiAFKPlus; Chunky and BetterTeams
    are optional and can be deselected in the form),
    disables spawn protection (spawn-protection=0), lifts BetterTeams limits
    (unlimited warps/chests/members/allies/admins), sets up sidebar/tab
    leaderboards (entity kills / deaths), kicks off Chunky world pre-generation
    in the background after the first boot, and pins the container's Docker
    restart policy to "no" so autostart is controlled only by DiscoPanel
  * assigns the next free Bedrock UDP port from 19132 and forwards it
  * installs the newest MCXboxBroadcast Geyser extension, points it at the
    public IP and the server's Bedrock port, and surfaces the one-time
    Xbox device-code login from the server logs

Configuration comes from the environment (see config.example.env).
"""

import glob
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import urllib.parse
import urllib.request

from flask import Flask, Response, jsonify, render_template, request

# --------------------------------------------------------------------------
# Configuration (from environment)
# --------------------------------------------------------------------------
DP_URL = os.environ.get("DP_URL", "http://127.0.0.1:8080").rstrip("/")
DP_TOKEN = os.environ.get("DP_TOKEN", "")

HOST_DATA_ROOT = os.environ.get("HOST_DATA_ROOT", "/opt/discopanel/data/servers")
PUBLIC_IP = os.environ.get("MCXBOX_IP", "87.106.60.148")
BEDROCK_BASE = int(os.environ.get("BEDROCK_BASE", "19132"))
BEDROCK_MAX = int(os.environ.get("BEDROCK_MAX", "19199"))
PROXY_LISTENER = os.environ.get("PROXY_LISTENER", "default")
BIND_HOST = os.environ.get("BIND_HOST", "0.0.0.0")
BIND_PORT = int(os.environ.get("BIND_PORT", "5005"))
# Default Chunky world pre-generation radius in blocks (0 disables). The form can
# override this per server; pre-generation runs in the background after boot.
CHUNKY_RADIUS = int(os.environ.get("CHUNKY_RADIUS", "1000"))

# DiscoPanel always creates server containers with the Docker restart policy
# "unless-stopped", which re-starts a running server on host/Docker reboot
# regardless of DiscoPanel's own AutoStart flag. Setting this to "no" (via a
# scoped sudo rule for `docker update`) makes autostart purely DiscoPanel-driven.
# Set to "" to leave DiscoPanel's default untouched.
CONTAINER_RESTART = os.environ.get("CONTAINER_RESTART", "no")

# Geyser-Spigot and Floodgate-Spigot are not on Modrinth for Spigot/Paper;
# they come from the canonical GeyserMC download API (always the newest build).
GEYSERMC_DL = "https://download.geysermc.org/v2/projects/{p}/versions/latest/builds/latest/downloads/spigot"
GEYSERMC_PLUGINS = {
    "geyser": "Geyser-Spigot.jar",
    "floodgate": "floodgate-spigot.jar",
}
# The rest come from Modrinth (newest release). We install jars directly rather
# than via itzg's MODRINTH_PROJECTS, which filters by the exact Minecraft
# version and fails for Paper's calendar versions (e.g. 26.1.2).
#   crossplay : viaversion/viabackwards/viarewind translate client versions,
#               skinsrestorer restores Bedrock/offline skins.
#   admin     : essentialsx (core commands), vaultunlocked (maintained, API-
#               compatible Vault drop-in that EssentialsX/LuckPerms hook into),
#               luckperms (permissions), antiafkplus (AFK handling; exempt
#               players via the "antiafkplus.bypass" permission = the whitelist).
MODRINTH_PLUGINS = [
    "viaversion", "viabackwards", "viarewind", "skinsrestorer",
    "essentialsx", "vaultunlocked", "luckperms", "antiafkplus",
]
# Optional plugins the create form can deselect.
#   chunky (Modrinth): world pre-generation.
#   BetterTeams (teams/clans): not on Modrinth/Hangar for Spigot; the canonical
#     plugin publishes release jars on GitHub. We pick the newest release that
#     actually carries a matching jar asset (booksaw only attaches jars to some
#     releases; the rest are SpigotMC-only). (repo, jar name fragment).
CHUNKY_SLUG = "chunky"
BETTERTEAMS_REPO = ("booksaw/BetterTeams", "betterteams")
MCXBOX_SLUG = "mcxboxbroadcast"

# Geyser config is pre-written before the first boot so crossplay is correct
# from the start (Floodgate auth, correct broadcast port) without a restart.
GEYSER_CONFIG_TEMPLATE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "geyser-config.yml"
)

UA = {"User-Agent": "discopanel-crossplay"}

app = Flask(__name__)

# --------------------------------------------------------------------------
# DiscoPanel ConnectRPC helpers
# --------------------------------------------------------------------------


def rpc(service, method, payload):
    """Call a DiscoPanel ConnectRPC method and return the parsed JSON body."""
    url = f"{DP_URL}/discopanel.v1.{service}/{method}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if DP_TOKEN:
        req.add_header("Authorization", f"Bearer {DP_TOKEN}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode()
    return json.loads(body) if body else {}


def list_servers():
    return rpc("ServerService", "ListServers", {}).get("servers", [])


def next_bedrock_port(servers):
    """Lowest free UDP host port in [BEDROCK_BASE, BEDROCK_MAX]."""
    used = set()
    for s in servers:
        for p in s.get("additionalPorts") or []:
            if (p.get("protocol") or "tcp").lower() == "udp":
                hp = int(p.get("hostPort") or 0)
                if BEDROCK_BASE <= hp <= BEDROCK_MAX:
                    used.add(hp)
    for port in range(BEDROCK_BASE, BEDROCK_MAX + 1):
        if port not in used:
            return port
    raise RuntimeError("no free Bedrock port in range")


def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "server"


# Upper bound for a manual RAM override from the form (GB).
MEM_MAX_GB = int(os.environ.get("MEM_MAX_GB", "24"))

# Sizing tiers keyed on the expected number of concurrent players. Memory is a
# bit generous because the crossplay stack (Geyser + ViaVersion) adds overhead.
# (max_players_expected, memory_mb, max_slots, view_distance, simulation_distance)
SIZING_TIERS = [
    (4, 6144, 8, 10, 8),
    (8, 8192, 12, 10, 8),
    (16, 12288, 24, 10, 6),
    (32, 16384, 40, 8, 6),
    (64, 20480, 80, 8, 5),
    (10 ** 9, 24576, 150, 6, 4),
]


def plan_for_players(players):
    """Map an expected concurrent player count to server sizing."""
    p = max(1, int(players or 1))
    for cap, mem, slots, view, sim in SIZING_TIERS:
        if p <= cap:
            return {
                "players": p,
                "memory": mem,
                "max_players": slots,
                "view_distance": view,
                "simulation_distance": sim,
            }
    return {"players": p, "memory": 24576, "max_players": 150,
            "view_distance": 6, "simulation_distance": 4}


# --------------------------------------------------------------------------
# Local filesystem helpers (app runs on the DiscoPanel host)
# --------------------------------------------------------------------------


def find_server_dir(server_id):
    matches = glob.glob(os.path.join(HOST_DATA_ROOT, f"*_{server_id}"))
    if not matches:
        raise RuntimeError("server data directory not found on host")
    return matches[0]


def download(url, dest):
    """Download url straight to dest (atomic-ish via temp file)."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest))
    os.close(fd)
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as fh:
            shutil.copyfileobj(resp, fh)
        os.replace(tmp, dest)
        try:
            os.chmod(dest, 0o644)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def write_file(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Plugin resolution + install
# --------------------------------------------------------------------------


def _modrinth_newest_file(slug):
    """(filename, url) of the newest RELEASE jar of a Modrinth plugin."""
    q = '["paper","spigot","bukkit"]'
    api = f"https://api.modrinth.com/v2/project/{slug}/version?loaders={urllib.parse.quote(q)}"
    req = urllib.request.Request(api, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        versions = json.loads(resp.read().decode())
    if not versions:
        raise RuntimeError(f"no Modrinth versions for {slug}")
    rel = [v for v in versions if v.get("version_type") == "release"] or versions
    v = rel[0]
    f = next((x for x in v["files"] if x.get("primary")), v["files"][0])
    return f["filename"], f["url"]


def _github_newest_asset(repo, fragment):
    """(filename, url) of the jar asset (matching fragment) from the newest
    GitHub release of `repo` that actually attaches one."""
    api = f"https://api.github.com/repos/{repo}/releases?per_page=30"
    req = urllib.request.Request(api, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        releases = json.loads(resp.read().decode())
    for rel in releases:  # newest first
        for a in rel.get("assets", []):
            n = a.get("name", "")
            if fragment.lower() in n.lower() and n.lower().endswith(".jar"):
                return n, a["browser_download_url"]
    raise RuntimeError(f"no jar asset matching '{fragment}' in {repo} releases")


def resolve_plugin_downloads(install_chunky=True, install_betterteams=True):
    """Return [(filename, url)] for the selected plugin stack (newest builds)."""
    downloads = []
    for p, fn in GEYSERMC_PLUGINS.items():
        downloads.append((fn, GEYSERMC_DL.format(p=p)))
    for slug in MODRINTH_PLUGINS:
        downloads.append(_modrinth_newest_file(slug))
    if install_chunky:
        downloads.append(_modrinth_newest_file(CHUNKY_SLUG))
    if install_betterteams:
        downloads.append(_github_newest_asset(*BETTERTEAMS_REPO))
    return downloads


def install_plugins(server_dir, log, install_chunky=True, install_betterteams=True):
    """Download the selected plugin jars into plugins/."""
    plugins_dir = f"{server_dir}/plugins"
    os.makedirs(plugins_dir, exist_ok=True)
    for filename, url in resolve_plugin_downloads(install_chunky, install_betterteams):
        log(f"Installing {filename}")
        download(url, f"{plugins_dir}/{filename}")


# --------------------------------------------------------------------------
# MCXboxBroadcast (Geyser extension)
# --------------------------------------------------------------------------

MCXBOX_CONFIG = """# Managed by discopanel-crossplay
session:
  remote-address: {ip}
  remote-port: {port}
  update-interval: 30
friend-sync:
  update-interval: 60
  auto-follow: true
  auto-unfollow: true
  initial-invite: true
  expiry:
    enabled: true
    days: 15
    check: 1800
notifications:
  enabled: false
  webhook-url: ''
config-version: 2
"""


def newest_mcxbox_extension_url():
    """(filename, url) for the newest MCXboxBroadcast extension jar."""
    api = f"https://api.modrinth.com/v2/project/{MCXBOX_SLUG}/version"
    req = urllib.request.Request(api, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        versions = json.loads(resp.read().decode())
    for v in versions:  # newest first
        for f in v.get("files", []):
            fn = f.get("filename", "")
            if "extension" in fn.lower() and fn.lower().endswith(".jar"):
                return fn, f["url"]
    v = versions[0]
    f = next((x for x in v["files"] if x.get("primary")), v["files"][0])
    return f["filename"], f["url"]


def install_mcxbox(server_dir, port, log):
    ext_dir = f"{server_dir}/plugins/Geyser-Spigot/extensions"
    cfg_dir = f"{ext_dir}/mcxboxbroadcast"
    os.makedirs(cfg_dir, exist_ok=True)

    if not glob.glob(f"{ext_dir}/*[Ee]xtension*.jar"):
        try:
            fn, url = newest_mcxbox_extension_url()
            log(f"Downloading newest MCXboxBroadcast extension ({fn})")
            download(url, f"{ext_dir}/{fn}")
        except Exception as e:  # noqa: BLE001 - reuse an existing jar on the host
            log(f"Modrinth download failed ({e}); copying an existing extension from the host")
            found = glob.glob(f"{HOST_DATA_ROOT}/*/plugins/Geyser-Spigot/extensions/*[Ee]xtension*.jar")
            if not found:
                raise RuntimeError("no MCXboxBroadcast extension jar available to install")
            shutil.copy(found[0], f"{ext_dir}/{os.path.basename(found[0])}")

    log(f"Writing MCXboxBroadcast config (advertise {PUBLIC_IP}:{port})")
    write_file(f"{cfg_dir}/config.yml", MCXBOX_CONFIG.format(ip=PUBLIC_IP, port=port))


def write_geyser_config(server_dir, port):
    """Pre-write Geyser's config.yml so the very first boot is already correct:
    Floodgate authentication and the public (host) Bedrock port as broadcast
    port. Doing this before boot avoids a container restart afterwards."""
    with open(GEYSER_CONFIG_TEMPLATE, encoding="utf-8") as fh:
        cfg = fh.read()
    cfg = cfg.replace("__BROADCAST_PORT__", str(port))
    write_file(f"{server_dir}/plugins/Geyser-Spigot/config.yml", cfg)


# --------------------------------------------------------------------------
# Server lifecycle helpers
# --------------------------------------------------------------------------


def start_server(sid, log):
    """StartServer with retries (it can briefly 500 while Docker settles)."""
    for attempt in range(6):
        try:
            rpc("ServerService", "StartServer", {"id": sid})
            return
        except Exception as e:  # noqa: BLE001
            if attempt == 5:
                raise
            log(f"Start not ready yet ({e}); retrying...")
            time.sleep(5)


def wait_for_path(sid, subpath, what, timeout):
    """Block until <server_dir>/<subpath> exists; return the server_dir.

    The first boot is also what makes the data directory writable for us: itzg's
    entrypoint chowns /data to its run user (uid 1000 = the service user) and
    creates plugins/, so we can only stage files after this returns.
    """
    server_dir = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if server_dir is None:
                server_dir = find_server_dir(sid)
            if os.path.exists(os.path.join(server_dir, subpath)):
                return server_dir
        except Exception:  # noqa: BLE001 - not there yet
            pass
        time.sleep(6)
    raise RuntimeError(f"timed out waiting for {what}")


def server_status(sid):
    for s in list_servers():
        if s.get("id") == sid:
            return s.get("status", "")
    return ""


def apply_settings_and_reload(sid, plan, log):
    """Stop, apply the player-count game settings, then start.

    UpdateServerConfig recreates the container *cleanly* (it stops+removes the
    old one before creating the new one), so this both applies the new env
    (slots, view/sim distance) and loads the freshly-staged crossplay plugins -
    without ever hitting DiscoPanel's restart path, which recreates the
    container without removing the old one first and can name/port-conflict.
    """
    log("Stopping server to apply settings and load the crossplay stack")
    rpc("ServerService", "StopServer", {"id": sid})
    deadline = time.time() + 120
    while time.time() < deadline:
        st = server_status(sid)
        if st in ("SERVER_STATUS_STOPPED", "SERVER_STATUS_ERROR", "", None):
            break
        time.sleep(3)
    log(
        f"Applying game settings ({plan['max_players']} slots, view/sim "
        f"distance {plan['view_distance']}/{plan['simulation_distance']}, "
        f"spawn-protection off)"
    )
    rpc(
        "ConfigService",
        "UpdateServerConfig",
        {
            "serverId": sid,
            "updates": {
                "maxPlayers": str(plan["max_players"]),
                "viewDistance": str(plan["view_distance"]),
                "simulationDistance": str(plan["simulation_distance"]),
                "spawnProtection": "0",
            },
        },
    )
    log("Starting server with the crossplay stack loaded")
    start_server(sid, log)


def fetch_logs_text(sid, lines=150):
    """Return the newest server console lines as plain text (ANSI stripped)."""
    resp = rpc("ServerService", "GetServerLogs", {"id": sid, "lines": lines})
    entries = resp.get("logs") or []
    msgs = [e.get("message", "") if isinstance(e, dict) else str(e) for e in entries]
    return re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(msgs))


def send_command(sid, cmd):
    """Run a console command on a running server via DiscoPanel."""
    return rpc("ServerService", "SendCommand", {"id": sid, "command": cmd})


def apply_restart_policy(sid, log):
    """Set the server container's Docker restart policy to CONTAINER_RESTART.

    DiscoPanel hardcodes "unless-stopped" on every (re)created container, so a
    running server would come back after a host reboot regardless of DiscoPanel's
    AutoStart flag. Overriding it to "no" leaves autostart entirely to DiscoPanel.
    Needs a scoped sudo rule (docker update --restart no discopanel-server-*).
    """
    if not CONTAINER_RESTART:
        return
    container = f"discopanel-server-{sid}"
    try:
        subprocess.run(
            ["sudo", "-n", "/usr/bin/docker", "update", "--restart",
             CONTAINER_RESTART, container],
            check=True, capture_output=True, timeout=30,
        )
        log(f"Docker restart policy set to '{CONTAINER_RESTART}' "
            "(autostart controlled only by DiscoPanel)")
    except Exception as e:  # noqa: BLE001
        detail = getattr(e, "stderr", b"")
        detail = detail.decode(errors="replace").strip() if detail else e
        log(f"Could not set restart policy ({detail})")


def wait_for_command_ready(sid, timeout=120):
    """Block until the server accepts console commands.

    Right after a (re)start, DiscoPanel may report boot from the persistent log
    while the fresh container is not yet 'running' (SendCommand then 400s), so we
    probe with a harmless command until it actually succeeds.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if send_command(sid, "list").get("success"):
                return True
        except Exception:  # noqa: BLE001 - container not ready yet (e.g. 400)
            pass
        time.sleep(5)
    return False


def start_chunky_pregen(sid, radius, log):
    """Kick off Chunky world pre-generation (overworld around 0,0).

    Fire-and-forget: Chunky generates in the background on the running server and
    resumes across restarts (continue-on-restart), so we don't wait for it.
    """
    if not wait_for_command_ready(sid):
        log("Server not accepting commands yet; skipping Chunky pre-generation "
            "(start it manually with /chunky start)")
        return
    log(f"Starting Chunky pre-generation (overworld, radius {radius} blocks)")
    for cmd in ("chunky world world", "chunky center 0 0",
                f"chunky radius {radius}", "chunky start"):
        try:
            send_command(sid, cmd)
        except Exception as e:  # noqa: BLE001
            log(f"Chunky command failed ('{cmd}'): {e}; skipping pre-generation")
            return
        time.sleep(1)
    log("Chunky is pre-generating chunks in the background")


# BetterTeams per-team limits to lift to "unlimited" (-1). warps is the one that
# matters most; the rest ("etc") are the other capacity limits that document -1
# as unlimited. maxOwners is intentionally left alone (no documented -1).
BETTERTEAMS_UNLIMITED_KEYS = ("maxWarps", "maxChests", "teamLimit",
                              "maxAdmins", "allyLimit")
_BT_LIMIT_RE = re.compile(
    r"^(\s*)(" + "|".join(BETTERTEAMS_UNLIMITED_KEYS) + r"):\s*-?\d+\s*$"
)


def patch_betterteams_unlimited(config_path):
    """Rewrite BetterTeams limit keys to -1 (unlimited). Returns True if changed.

    Value-independent and comment-safe: only lines like `<indent>key: <int>` are
    touched (comment lines start with '#', so they never match)."""
    if not os.path.exists(config_path):
        return False
    with open(config_path, encoding="utf-8") as fh:
        lines = fh.readlines()
    changed = False
    for i, line in enumerate(lines):
        m = _BT_LIMIT_RE.match(line)
        if m and line.strip() != f"{m.group(2)}: -1":
            lines[i] = f"{m.group(1)}{m.group(2)}: -1\n"
            changed = True
    if changed:
        with open(config_path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
    return changed


def configure_betterteams(server_dir, sid, log, reload=True):
    """Set BetterTeams warps/chests/members/allies/admins to unlimited.

    Patches config.yml on disk (takes effect on next boot regardless) and, if the
    server is accepting commands, live-reloads it with `teamadmin reload`."""
    cfg = f"{server_dir}/plugins/BetterTeams/config.yml"
    if not patch_betterteams_unlimited(cfg):
        return
    log("BetterTeams: warps/chests/members/allies/admins set to unlimited")
    if reload:
        try:
            send_command(sid, "teamadmin reload")
        except Exception as e:  # noqa: BLE001
            log(f"BetterTeams reload failed ({e}); applies on next restart")


# Vanilla scoreboard leaderboards: (objective, criterion, display slot, title).
# sidebar = right-hand list, list = the tab player list. Both persist in the
# world's scoreboard data and update live as players kill mobs / die.
LEADERBOARDS = [
    ("ekills", "minecraft.custom:minecraft.mob_kills", "sidebar",
     '{"text":"Entity Kills","color":"gold"}'),
    ("deaths", "deathCount", "list", '{"text":"Tode","color":"red"}'),
]


def setup_leaderboards(sid, log):
    """Create the sidebar (entity kills) and tab (deaths) leaderboards.

    Re-adding an existing objective just no-ops (SendCommand reports the failure
    without raising), so this is safe to run again."""
    log("Setting up leaderboards (sidebar: entity kills, tab: deaths)")
    for name, crit, slot, title in LEADERBOARDS:
        try:
            send_command(sid, f"scoreboard objectives add {name} {crit} {title}")
            send_command(sid, f"scoreboard objectives setdisplay {slot} {name}")
        except Exception as e:  # noqa: BLE001
            log(f"Leaderboard '{name}' setup failed: {e}")


def wait_for_boot(sid, log, timeout=300):
    """Block until Paper reports startup complete (or Geyser has started)."""
    ready = re.compile(r'Done \(|Started Geyser|Geyser.*started', re.I)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if ready.search(fetch_logs_text(sid)):
                return
        except Exception:  # noqa: BLE001 - logs not available yet
            pass
        time.sleep(5)
    log("Server is taking a while to finish starting; continuing anyway")


# --------------------------------------------------------------------------
# Provisioning job (background thread, streams progress via SSE)
# --------------------------------------------------------------------------

JOBS = {}  # job_id -> queue.Queue
PDFS = {}  # job_id -> {name, xbox, chunky, betterteams} for the command-list PDFs


def provision(job_id, name, version, install_xbox, players, memory_gb=None,
              slots=None, view_distance=None, pregen_radius=None,
              install_chunky=True, install_betterteams=True):
    q = JOBS[job_id]

    def log(msg, **extra):
        q.put({"type": "log", "msg": msg, **extra})

    try:
        plan = plan_for_players(players)
        manual = set()
        if memory_gb:  # explicit overrides from the form
            plan["memory"] = max(1024, min(MEM_MAX_GB * 1024, int(memory_gb * 1024)))
            manual.add("ram")
        if slots:
            plan["max_players"] = max(1, min(1000, int(slots)))
            manual.add("slots")
        if view_distance:
            vd = max(2, min(32, int(view_distance)))
            plan["view_distance"] = vd
            if plan["simulation_distance"] > vd:  # sim can't exceed view
                plan["simulation_distance"] = vd
            manual.add("view")
        gb = plan["memory"] / 1024
        servers = list_servers()
        hostname = slugify(name)
        if any(hostname == slugify(s.get("name", "")) for s in servers):
            hostname = f"{hostname}-{uuid.uuid4().hex[:4]}"
        port = next_bedrock_port(servers)
        log(f"Next free Bedrock port: {port}", port=port, hostname=hostname)

        def m(key):
            return " (manual)" if key in manual else ""
        log(
            f"~{plan['players']} players -> {gb:.0f} GB RAM{m('ram')}, "
            f"{plan['max_players']} slots{m('slots')}, view {plan['view_distance']}"
            f"{m('view')} / sim {plan['simulation_distance']}"
        )
        log(f"Creating Paper {version} server '{name}'")
        create = rpc(
            "ServerService",
            "CreateServer",
            {
                "name": name,
                "description": "Bedrock/Java crossplay (provisioned)",
                "modLoader": "MOD_LOADER_PAPER",
                "mcVersion": version,
                "memory": plan["memory"],
                "maxPlayers": plan["max_players"],
                "proxyHostname": hostname,
                "proxyListenerId": PROXY_LISTENER,
                "useBaseUrl": True,
                "additionalPorts": [
                    {
                        "name": "Geyser Bedrock",
                        "containerPort": BEDROCK_BASE,
                        "hostPort": port,
                        "protocol": "udp",
                    }
                ],
                "startImmediately": False,
            },
        )
        server = create.get("server", create)
        sid = server["id"]
        full_host = server.get("proxyHostname", hostname)
        log(f"Server created (id {sid[:8]}), reachable at {full_host}", server_id=sid)

        # Set loader/version explicitly (harmless; no container yet). The
        # player-count game settings are applied later, once a container exists,
        # because config changes only take effect via a container recreate.
        rpc(
            "ConfigService",
            "UpdateServerConfig",
            {"serverId": sid, "updates": {"type": "paper", "version": version}},
        )

        # First boot: brings up the (empty) server and, crucially, makes the
        # data directory writable for us - itzg chowns /data to uid 1000 and
        # creates plugins/. We can only stage files after this.
        log("Starting server (first boot to initialise the data directory)")
        start_server(sid, log)
        server_dir = wait_for_path(sid, "plugins", "server to finish first boot", 300)

        # Stage the whole crossplay stack, then reload with a stop+start (never
        # a restart, which would recreate the container).
        log("Installing crossplay plugin stack")
        install_plugins(server_dir, log, install_chunky, install_betterteams)
        log(f"Writing Geyser config (Floodgate auth, broadcast port {port})")
        write_geyser_config(server_dir, port)
        if install_xbox:
            install_mcxbox(server_dir, port, log)

        apply_settings_and_reload(sid, plan, log)

        wait_for_boot(sid, log)
        log("Server is up; crossplay stack active")

        ready = wait_for_command_ready(sid)
        if install_betterteams:
            configure_betterteams(server_dir, sid, log, reload=ready)
        if ready:
            setup_leaderboards(sid, log)
        else:
            log("Server not accepting commands yet; leaderboards apply on next "
                "restart")

        if install_chunky:
            radius = CHUNKY_RADIUS if pregen_radius is None else max(0, int(pregen_radius))
            if radius > 0:
                start_chunky_pregen(sid, radius, log)

        if install_xbox:
            surface_xbox_code(sid, log)

        # Final step: the container is stable now (no more recreations), so pin
        # its restart policy - DiscoPanel would otherwise leave it unless-stopped.
        apply_restart_policy(sid, log)

        q.put(
            {
                "type": "done",
                "server_id": sid,
                "hostname": full_host,
                "port": port,
                "connect_java": full_host,
                "connect_bedrock": f"{PUBLIC_IP}:{port}",
            }
        )
    except Exception as e:  # noqa: BLE001
        q.put({"type": "error", "msg": str(e)})


def surface_xbox_code(sid, log):
    """Poll server logs for the MCXboxBroadcast device-code prompt.

    GetServerLogs returns {"logs": [{"message": ...}, ...]} (newest ~100 lines).
    """
    log("Waiting for the Xbox sign-in code (MCXboxBroadcast)...")
    url_re = re.compile(r"(https?://\S*?/link)\b", re.I)
    code_re = re.compile(r"code\s+([A-Z0-9]{6,10})\b", re.I)
    deadline = time.time() + 240
    while time.time() < deadline:
        try:
            text = fetch_logs_text(sid, lines=150)
            for ln in text.splitlines():
                if "/link" in ln.lower() and "code" in ln.lower():
                    c = code_re.search(ln)
                    if c:
                        u = url_re.search(ln)
                        log(
                            "Xbox sign-in required",
                            xbox_url=u.group(1) if u else "https://www.microsoft.com/link",
                            xbox_code=c.group(1),
                        )
                        return
            if re.search(r"session (created|updated)|broadcasting|friend list", text, re.I):
                log("MCXboxBroadcast authenticated (cached session) - broadcasting")
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(5)
    log("No Xbox code seen yet - open the server console and sign in once to start broadcasting")


# --------------------------------------------------------------------------
# Command-list PDFs (grouped by addon, with explanations)
# --------------------------------------------------------------------------


def command_catalog(chunky=True, betterteams=True, xbox=True):
    """Ordered [(addon, {"player": [(cmd, desc)], "admin": [(cmd, desc)]})].

    Only the addons actually installed on the server are included."""
    cat = [
        ("EssentialsX", {
            "player": [
                ("/spawn", "Zum Server-Spawn teleportieren."),
                ("/sethome [name]", "Setzt einen Zuhause-Punkt."),
                ("/home [name]", "Teleportiert zu einem Zuhause."),
                ("/delhome <name>", "Loescht ein Zuhause."),
                ("/tpa <spieler>", "Teleport-Anfrage an einen Spieler senden."),
                ("/tpaccept", "Teleport-Anfrage annehmen."),
                ("/tpdeny", "Teleport-Anfrage ablehnen."),
                ("/back", "Zur letzten Position bzw. zum Todesort zurueck."),
                ("/msg <spieler> <text>", "Private Nachricht senden."),
                ("/r <text>", "Auf die letzte private Nachricht antworten."),
                ("/mail send <spieler> <text>", "Offline-Nachricht hinterlassen."),
                ("/afk", "AFK-Status an- oder ausschalten."),
                ("/list", "Zeigt die Online-Spieler."),
                ("/rules", "Zeigt die Serverregeln."),
                ("/warp [name]", "Teleportiert zu einem oeffentlichen Warp."),
                ("/pay <spieler> <betrag>", "Geld ueberweisen (falls Economy aktiv)."),
                ("/balance", "Zeigt dein Guthaben."),
            ],
            "admin": [
                ("/gamemode <0-3> [spieler]", "Spielmodus (auch /gmc /gms /gmsp /gma)."),
                ("/give <spieler> <item> [anzahl]", "Item geben."),
                ("/heal [spieler]", "Leben und Hunger auffuellen."),
                ("/feed [spieler]", "Hunger auffuellen."),
                ("/god [spieler]", "Unverwundbarkeit an oder aus."),
                ("/fly [spieler]", "Flugmodus an oder aus."),
                ("/tp <spieler> [ziel]", "Teleportieren."),
                ("/tphere <spieler>", "Spieler zu dir teleportieren."),
                ("/kick <spieler> [grund]", "Spieler kicken."),
                ("/ban <spieler> [grund]", "Spieler bannen."),
                ("/tempban <spieler> <zeit> [grund]", "Zeitlich bannen."),
                ("/mute <spieler> [zeit]", "Spieler stummschalten."),
                ("/setwarp <name>", "Oeffentlichen Warp setzen."),
                ("/delwarp <name>", "Warp loeschen."),
                ("/setspawn", "Server-Spawn setzen."),
                ("/broadcast <text>", "Server-weite Nachricht."),
                ("/vanish", "Unsichtbar werden."),
                ("/invsee <spieler>", "Inventar eines Spielers ansehen."),
                ("/time set <wert>", "Tageszeit setzen."),
                ("/weather <clear|storm>", "Wetter setzen."),
            ],
        }),
        ("LuckPerms (Rechteverwaltung)", {
            "player": [],
            "admin": [
                ("/lp user <spieler> parent add <gruppe>", "Spieler einer Gruppe hinzufuegen."),
                ("/lp user <spieler> permission set <node> true|false", "Einzelrecht setzen."),
                ("/lp group <gruppe> permission set <node> true|false", "Gruppenrecht setzen."),
                ("/lp creategroup <name>", "Neue Gruppe anlegen."),
                ("/lp editor", "Web-Editor-Link zum Bearbeiten oeffnen."),
                ("/lp user <spieler> permission set antiafkplus.bypass true",
                 "Spieler von AFK-Behandlung ausnehmen (AFK-Whitelist)."),
            ],
        }),
        ("AntiAFKPlus", {
            "player": [("/afk", "AFK-Status manuell umschalten.")],
            "admin": [
                ("/antiafkplus reload", "Konfiguration neu laden."),
                ("Permission antiafkplus.bypass", "Traeger gelten nie als AFK (Whitelist)."),
            ],
        }),
        ("SkinsRestorer", {
            "player": [
                ("/skin set <name>", "Skin eines anderen Namens uebernehmen."),
                ("/skin clear", "Eigenen Skin zuruecksetzen."),
                ("/skins", "Skin-Auswahl-Menue oeffnen."),
            ],
            "admin": [
                ("/sr reload", "SkinsRestorer neu laden."),
                ("/sr set <spieler> <name>", "Skin eines Spielers setzen."),
            ],
        }),
        ("Geyser / Floodgate (Crossplay)", {
            "player": [
                ("/linkaccount", "Bedrock- und Java-Konto verknuepfen."),
                ("/unlinkaccount", "Konto-Verknuepfung aufheben."),
            ],
            "admin": [
                ("/geyser reload", "Geyser neu laden."),
                ("/geyser dump", "Diagnose-Dump erstellen (fuer Support)."),
                ("/floodgate", "Floodgate-Info anzeigen."),
            ],
        }),
        ("ViaVersion", {
            "player": [],
            "admin": [
                ("/viaversion list", "Client-Versionen der Spieler anzeigen."),
                ("/viaversion", "Plugin-Status anzeigen."),
            ],
        }),
    ]
    if betterteams:
        cat.append(("BetterTeams", {
            "player": [
                ("/team create <name>", "Team gruenden."),
                ("/team join <name>", "Team beitreten (nach Einladung)."),
                ("/team invite <spieler>", "Spieler einladen."),
                ("/team leave", "Team verlassen."),
                ("/team info [name]", "Team-Infos anzeigen."),
                ("/team chat <text>", "Im Team-Chat schreiben (auch /tc)."),
                ("/team sethome", "Team-Zuhause setzen."),
                ("/team home", "Zum Team-Zuhause teleportieren."),
                ("/team setwarp <name>", "Team-Warp anlegen (unbegrenzt)."),
                ("/team warp <name>", "Zu einem Team-Warp teleportieren."),
                ("/team ally <team>", "Buendnis anfragen oder annehmen."),
                ("/team money", "Team-Bank anzeigen."),
            ],
            "admin": [
                ("/teamadmin reload", "BetterTeams-Konfiguration neu laden."),
                ("/teamadmin delete <team>", "Team aufloesen."),
                ("/teamadmin info <team>", "Team-Details (Admin)."),
            ],
        }))
    if chunky:
        cat.append(("Chunky (Weltvorgenerierung)", {
            "player": [],
            "admin": [
                ("/chunky radius <bloecke>", "Radius der Generierung setzen."),
                ("/chunky start", "Vorgenerierung starten."),
                ("/chunky pause", "Pausieren (Fortschritt wird gespeichert)."),
                ("/chunky continue", "Fortsetzen."),
                ("/chunky cancel", "Abbrechen und verwerfen."),
                ("/chunky progress", "Fortschritt anzeigen."),
            ],
        }))
    if xbox:
        cat.append(("MCXboxBroadcast (Xbox-Freunde)", {
            "player": [],
            "admin": [
                ("Hinweis", "Beim ersten Start einmalig per Xbox-Code anmelden "
                 "(Code wird im Erstell-Tool angezeigt)."),
            ],
        }))
    cat.append(("Leaderboards (Scoreboard)", {
        "player": [
            ("Sidebar rechts", "Rangliste getoeteter Gegner (Entity Kills)."),
            ("Tab-Liste", "Anzahl Tode hinter jedem Spielernamen."),
        ],
        "admin": [
            ("/scoreboard objectives setdisplay sidebar ekills",
             "Kill-Rangliste wieder einblenden."),
            ("/scoreboard objectives setdisplay list deaths",
             "Tode im Tab wieder einblenden."),
        ],
    }))
    return cat


def build_command_pdf(audience, name, chunky=True, betterteams=True, xbox=True):
    """Render the player or admin command list to PDF bytes."""
    from fpdf import FPDF  # local import so the app still starts without it
    from fpdf.enums import XPos, YPos

    def cell(pdf, h, text, **kw):
        # Always full width, and reset the cursor to the left margin on the next
        # line (fpdf2's multi_cell otherwise leaves x at the right margin, which
        # makes a following w=0 cell raise "not enough horizontal space").
        pdf.multi_cell(pdf.epw, h, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT, **kw)

    title = "Befehlsliste Spieler" if audience == "player" else "Befehlsliste Admins"
    pdf = FPDF()
    pdf.set_auto_page_break(True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 18)
    cell(pdf, 10, title)
    pdf.set_font("Helvetica", "", 11)
    pdf.set_text_color(90, 90, 90)
    cell(pdf, 6, f"Server: {name}")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)
    for addon, groups in command_catalog(chunky, betterteams, xbox):
        cmds = groups.get(audience, [])
        if not cmds:
            continue
        pdf.set_font("Helvetica", "B", 13)
        pdf.set_fill_color(232, 232, 236)
        cell(pdf, 8, addon, fill=True)
        pdf.ln(1)
        for cmd, desc in cmds:
            pdf.set_font("Courier", "B", 10)
            cell(pdf, 5, cmd)
            pdf.set_font("Helvetica", "", 10)
            pdf.set_text_color(70, 70, 70)
            cell(pdf, 5, "    " + desc)
            pdf.set_text_color(0, 0, 0)
        pdf.ln(3)
    return bytes(pdf.output())


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/download/<job_id>/<audience>")
def download_pdf(job_id, audience):
    meta = PDFS.get(job_id)
    if meta is None or audience not in ("spieler", "admin"):
        return "unknown command list", 404
    data = build_command_pdf(
        "player" if audience == "spieler" else "admin",
        meta["name"], meta["chunky"], meta["betterteams"], meta["xbox"],
    )
    return Response(
        data, mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="commandliste_{audience}.pdf"'},
    )


@app.route("/api/versions")
def versions():
    """Paper versions for the dropdown (newest first, stable only)."""
    try:
        req = urllib.request.Request("https://fill.papermc.io/v3/projects/paper", headers=UA)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        vs = []
        for group in data.get("versions", {}).values():
            for v in group:
                if "-" not in v:  # drop pre/rc/snapshot builds
                    vs.append(v)
        return jsonify({"versions": vs})
    except Exception as e:  # noqa: BLE001
        return jsonify({"versions": [], "error": str(e)})


@app.route("/api/servers")
def servers_view():
    try:
        servers = list_servers()
        return jsonify(
            {
                "next_port": next_bedrock_port(servers),
                "servers": [
                    {"name": s.get("name"), "hostname": s.get("proxyHostname")}
                    for s in servers
                ],
            }
        )
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 502


@app.route("/create", methods=["POST"])
def create():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    version = (data.get("version") or "").strip()
    install_xbox = bool(data.get("xbox", True))
    install_chunky = bool(data.get("chunky", True))
    install_betterteams = bool(data.get("betterteams", True))
    try:
        players = int(data.get("players") or 8)
    except (TypeError, ValueError):
        players = 8
    def opt_num(key, cast):
        v = data.get(key)
        try:
            return cast(v) if v not in (None, "", 0) else None
        except (TypeError, ValueError):
            return None

    memory_gb = opt_num("memory", float)
    slots = opt_num("slots", int)
    view_distance = opt_num("view", int)
    # pregen: empty -> None (use default radius), 0 -> off, N -> radius. Can't use
    # opt_num here because it maps 0 to None (which would mean "default").
    pregen_raw = data.get("pregen")
    try:
        pregen_radius = int(pregen_raw) if pregen_raw not in (None, "") else None
    except (TypeError, ValueError):
        pregen_radius = None
    if not name or not version:
        return jsonify({"error": "name and version are required"}), 400
    job_id = uuid.uuid4().hex
    JOBS[job_id] = queue.Queue()
    # Remember the selection so the command-list PDFs can be built on download.
    PDFS[job_id] = {
        "name": name,
        "xbox": install_xbox,
        "chunky": install_chunky,
        "betterteams": install_betterteams,
    }
    threading.Thread(
        target=provision,
        args=(job_id, name, version, install_xbox, players, memory_gb,
              slots, view_distance, pregen_radius, install_chunky,
              install_betterteams),
        daemon=True,
    ).start()
    return jsonify({"job": job_id})


@app.route("/stream/<job_id>")
def stream(job_id):
    q = JOBS.get(job_id)
    if q is None:
        return "unknown job", 404

    def gen():
        while True:
            try:
                evt = q.get(timeout=30)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            yield f"data: {json.dumps(evt)}\n\n"
            if evt["type"] in ("done", "error"):
                break
        JOBS.pop(job_id, None)

    return Response(gen(), mimetype="text/event-stream")


if __name__ == "__main__":
    if not DP_TOKEN:
        print("WARNING: DP_TOKEN is not set")
    app.run(host=BIND_HOST, port=BIND_PORT, threaded=True)
