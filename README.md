# DiscoPanel Crossplay Provisioner

A small web app that provisions a ready-to-run **Bedrock ↔ Java crossplay**
Minecraft server on a [DiscoPanel](https://github.com/nickheyer/discopanel)
instance from a single form. Pick a name and a Paper version, click a button,
and it does the rest.

## What it does per server

- Asks how many players are expected and **sizes the server accordingly**
  (RAM, player slots, view/simulation distance):

  | ~Players | RAM   | Slots (max. players) | View/Sim |
  |----------|-------|----------------------|----------|
  | ≤ 4      | 6 GB  | 8                    | 10 / 8   |
  | ≤ 8      | 8 GB  | 12                   | 10 / 8   |
  | ≤ 16     | 12 GB | 24                   | 10 / 6   |
  | ≤ 32     | 16 GB | 40                   | 8 / 6    |
  | ≤ 64     | 20 GB | 80                   | 8 / 5    |
  | > 64     | 24 GB | 150                  | 6 / 4    |

  "Slots" is the maximum number of players allowed on the server at once
  (`max-players` in `server.properties`). **RAM, slots and view distance can
  each be set manually** in the form to override the automatic tier for that
  server; leave a field empty for automatic sizing. Ranges: RAM 1-`MEM_MAX_GB`
  GB (default cap 24), slots 1-1000, view distance 2-32 chunks (simulation
  distance is clamped to not exceed the view distance).

- Creates a **Paper** server on the chosen Minecraft version.
- Publishes it through the DiscoPanel proxy using a **custom hostname + base
  domain** (`<name>.mc.niklasmetzger.de`).
- Installs the newest crossplay plugin stack:
  **ViaVersion, ViaBackwards, ViaRewind, SkinsRestorer, Geyser, Floodgate**
  (Geyser + Floodgate configured for Bedrock/Java crossplay via Floodgate auth).
- Assigns the **next free Bedrock UDP port** starting at `19132`
  (server 1 → 19132, server 2 → 19133, …) and forwards it as an additional
  UDP port. That range is already DNAT-forwarded end-to-end by the public
  gateway, so no gateway change is needed.
- Installs the newest **MCXboxBroadcast** Geyser extension, points it at the
  public IP (`87.106.60.148`) and the server's Bedrock port, and surfaces the
  one-time **Xbox device-code login** right in the web UI.

## How the ports work

Geyser always listens on `19132` *inside* its own container. The per-server
public port is just the host side of the UDP port mapping
(`hostPort:19133 → containerPort:19132`). MCXboxBroadcast advertises
`87.106.60.148:<hostPort>` so Bedrock friends connect to the public endpoint.

## Deployment (systemd service on the DiscoPanel host)

The app is meant to run **on the DiscoPanel host** so it can talk to the API on
localhost and write plugin files straight into the server data directory.

```bash
# on the host, from a checkout of this repo
sudo ./install.sh
```

`install.sh`:

- installs the app to `/opt/discopanel-crossplay` in its own virtualenv,
- creates `/etc/discopanel-crossplay.env` (root-owned, `chmod 600`) from
  `config.example.env` - **edit it and set `DP_TOKEN`**,
- installs and **enables** `discopanel-crossplay.service` (runs as `niklas`),
- starts it. The web UI is then on `http://<host>:5005`.

After setting the token:

```bash
sudo systemctl restart discopanel-crossplay
sudo systemctl status  discopanel-crossplay
sudo journalctl -u discopanel-crossplay -f
```

The `DP_TOKEN` is a DiscoPanel API token (create one under Profile → API
Tokens). Nothing sensitive is stored in this repo.

## Requirements

- Runs on the DiscoPanel host (Linux) with Python 3.10+.
- Write access to `HOST_DATA_ROOT` (`/opt/discopanel/data/servers`) - the
  service runs as the user that owns those directories (`niklas`).
- Outbound HTTPS to Modrinth, the GeyserMC download API and PaperMC.

## How it boots (no restart)

The whole stack is written into the server data directory **before the first
boot**, so a single clean start brings everything up correctly:

1. Create the server (not started yet) - DiscoPanel makes the data directory.
2. Download the plugin jars into `plugins/`.
3. Pre-write `plugins/Geyser-Spigot/config.yml` (Floodgate auth-type, the public
   Bedrock port as `broadcast-port`).
4. Pre-stage the MCXboxBroadcast extension and its config.
5. Start the server **once**.

This deliberately avoids restarting the server. DiscoPanel *recreates* the
container on restart, which can fail with a name/port conflict if the previous
container has not fully gone away - so we configure everything up front instead.

## Notes

- Geyser and Floodgate come from the canonical GeyserMC download API; Via* and
  SkinsRestorer from Modrinth (newest release each). Jars are installed directly
  rather than via itzg's `MODRINTH_PROJECTS`, which filters by the exact MC
  version and fails for Paper's calendar versions.
- MCXboxBroadcast is a Geyser *extension* (not a plugin), pre-staged into
  `plugins/Geyser-Spigot/extensions/`.
- `geyser-config.yml` is a version-matched Geyser config template
  (`config-version: 7`); Geyser migrates it forward automatically if needed.
- Each server needs its own Xbox/Microsoft account signed in once. The device
  code appears both in the server console and in this app's UI.
