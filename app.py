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
    ViaRewind, SkinsRestorer, Geyser, Floodgate)
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
MODRINTH_PLUGINS = ["viaversion", "viabackwards", "viarewind", "skinsrestorer"]
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


def resolve_plugin_downloads():
    """Return [(filename, url)] for the six crossplay plugins (newest builds)."""
    downloads = []
    for p, fn in GEYSERMC_PLUGINS.items():
        downloads.append((fn, GEYSERMC_DL.format(p=p)))
    for slug in MODRINTH_PLUGINS:
        downloads.append(_modrinth_newest_file(slug))
    return downloads


def install_plugins(server_dir, log):
    """Download the crossplay plugin jars into plugins/."""
    plugins_dir = f"{server_dir}/plugins"
    os.makedirs(plugins_dir, exist_ok=True)
    for filename, url in resolve_plugin_downloads():
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
        f"distance {plan['view_distance']}/{plan['simulation_distance']})"
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


def provision(job_id, name, version, install_xbox, players, memory_gb=None):
    q = JOBS[job_id]

    def log(msg, **extra):
        q.put({"type": "log", "msg": msg, **extra})

    try:
        plan = plan_for_players(players)
        manual_ram = False
        if memory_gb:  # explicit override from the form (in GB)
            plan["memory"] = max(1024, min(MEM_MAX_GB * 1024, int(memory_gb * 1024)))
            manual_ram = True
        gb = plan["memory"] / 1024
        servers = list_servers()
        hostname = slugify(name)
        if any(hostname == slugify(s.get("name", "")) for s in servers):
            hostname = f"{hostname}-{uuid.uuid4().hex[:4]}"
        port = next_bedrock_port(servers)
        log(f"Next free Bedrock port: {port}", port=port, hostname=hostname)

        ram_note = f"{gb:.0f} GB RAM (manual)" if manual_ram else f"{gb:.0f} GB RAM"
        log(
            f"~{plan['players']} players -> {ram_note}, {plan['max_players']} "
            f"slots, view/sim distance {plan['view_distance']}/{plan['simulation_distance']}"
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
        install_plugins(server_dir, log)
        log(f"Writing Geyser config (Floodgate auth, broadcast port {port})")
        write_geyser_config(server_dir, port)
        if install_xbox:
            install_mcxbox(server_dir, port, log)

        apply_settings_and_reload(sid, plan, log)

        wait_for_boot(sid, log)
        log("Server is up; crossplay stack active")

        if install_xbox:
            surface_xbox_code(sid, log)

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
# Routes
# --------------------------------------------------------------------------


@app.route("/")
def index():
    return render_template("index.html")


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
    try:
        players = int(data.get("players") or 8)
    except (TypeError, ValueError):
        players = 8
    memory_gb = data.get("memory")
    try:
        memory_gb = float(memory_gb) if memory_gb not in (None, "", 0) else None
    except (TypeError, ValueError):
        memory_gb = None
    if not name or not version:
        return jsonify({"error": "name and version are required"}), 400
    job_id = uuid.uuid4().hex
    JOBS[job_id] = queue.Queue()
    threading.Thread(
        target=provision,
        args=(job_id, name, version, install_xbox, players, memory_gb),
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
