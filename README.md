<p align="center">
  <img src="docs/logo.png" alt="btrfs-ui logo" height="120">
</p><br>

<h1 align="center">btrfs-ui</h1><br>

<p align="center">
  A small web UI for btrfs on headless servers, packaged as a docker container.<br>
  Space and device health, subvolumes and snapshots, snapper, scrub, balance, defrag and trim.
</p>

<table>
  <tr>
    <td>
      <a href="docs/screenshots/overview-light.png"><picture>
        <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/overview-dark.png">
        <img src="docs/screenshots/overview-light.png" alt="Overview tab" width="400">
      </picture></a>
    </td>
    <td>
      <a href="docs/screenshots/subvolumes-light.png"><picture>
        <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/subvolumes-dark.png">
        <img src="docs/screenshots/subvolumes-light.png" alt="Subvolumes tab" width="400">
      </picture></a>
    </td>
  </tr>
  <tr>
    <td>
      <a href="docs/screenshots/snapper-light.png"><picture>
        <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/snapper-dark.png">
        <img src="docs/screenshots/snapper-light.png" alt="Snapper tab" width="400">
      </picture></a>
    </td>
    <td>
      <a href="docs/screenshots/maintenance-light.png"><picture>
        <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/maintenance-dark.png">
        <img src="docs/screenshots/maintenance-light.png" alt="Maintenance tab" width="400">
      </picture></a>
    </td>
  </tr>
</table>

btrfs-assistant is a great tool, but it is a desktop app, and running it on a server means
putting a whole graphical session on the box. This does the useful parts from a browser
instead: a Flask backend that runs `btrfs` and `snapper` for you, and a plain HTML, CSS and
JS front end with no build step. It follows your light or dark system setting.

## Quick start

1. Make sure the filesystem's **top level** (subvolid 5) is mounted somewhere on the host.
   That lets the UI see every subvolume, not just the ones you happen to mount. Check
   with `findmnt -t btrfs`. If it is not mounted yet:

   ```sh
   sudo mkdir -p /mnt/btrfs-root
   sudo mount -o subvolid=5 /dev/sdX /mnt/btrfs-root     # add a matching line to /etc/fstab
   ```

2. Download the compose file and set a password:

   ```sh
   mkdir btrfs-ui && cd btrfs-ui
   curl -O https://raw.githubusercontent.com/thornygravy/btrfs-ui/main/docker-compose.yml
   curl -O https://raw.githubusercontent.com/thornygravy/btrfs-ui/main/.env.example
   cp .env.example .env          # then put your password in .env
   ```

   Edit the volume paths in `docker-compose.yml` to match your mount points. If your
   password contains a `$`, wrap it in single quotes.

3. Start it:

   ```sh
   docker compose up -d
   ```

4. Open `http://<your-server-ip>:8080` from any machine on your network.

To update, run `docker compose pull && docker compose up -d`. The footer shows the
btrfs-ui version, so you can always tell which one is running.

Images are published for `amd64` and `arm64`. `latest` follows the main branch. If you
would rather update on purpose than by surprise, pin a release in the compose file, for
example `ghcr.io/thornygravy/btrfs-ui:1.0` (releases are tagged `1.0.0` and `1.0`).

### Build it yourself

Clone or download this repository, then in `docker-compose.yml` comment out the `image:`
line and uncomment `build: .`. Start it with `docker compose up -d --build`; without
`--build`, Compose keeps using an image you built earlier.

## What the container needs

| Setting | Why |
| --- | --- |
| `cap_add: SYS_ADMIN` | btrfs ioctls (delete subvolume, scrub, balance, defrag, trim) are refused without it. |
| Bind mounts of your btrfs mount points | The container only sees what you mount into it. Use the **same path as on the host**. |
| `/var/lib/btrfs` from the host | btrfs stores scrub history there. Without it the UI cannot see past scrubs and says "never run". |
| `/etc/snapper` (optional) | Only for the Snapper tab. |

No `/dev` access is needed. It runs as root inside the container, which btrfs requires.

Start the container after the filesystems are mounted on the host. A mount added later
is not visible to a container that is already running.

### Scrub history

`btrfs scrub status` reads its history from `/var/lib/btrfs/scrub.status.<UUID>`, and
that file lives on the host. The compose file bind-mounts the host's `/var/lib/btrfs`
into the container, so the UI shows scrubs that were started elsewhere (a cron job, a
shell) as well as the ones you start here. If you replace that mount with a Docker volume
or drop it, the container sees an empty directory and every filesystem reports
"never run".

### Snapper

Snapper is run directly (`snapper --no-dbus`), so no daemon or D-Bus is needed. Create
configs on the host as usual (`snapper -c data create-config /mnt/pool/@data`); the UI
manages the snapshots, not the config creation. Each
snapper config names a directory (`SUBVOLUME=` in `/etc/snapper/configs/<name>`). That
exact path has to exist inside the container, so bind-mount each one at the same path
as on the host, in addition to `/etc/snapper`:

```yaml
volumes:
  - /etc/snapper:/etc/snapper
  - /srv/data:/srv/data          # a config with SUBVOLUME="/srv/data"
```

A config for the host's root filesystem (`SUBVOLUME="/"`) cannot work this way, because
`/` inside the container is the container's own root. `BTRFS_UI_SNAPPER_ROOT=/host`
passes `--root /host` to snapper (mount the host's `/` there); this is experimental and
has not been tested.

Rolling back to a snapshot is deliberately not offered. It is easy to get wrong from
inside a container; do it from the host.

## Configuration

All through environment variables.

| Variable | Default | |
| --- | --- | --- |
| `BTRFS_UI_PASSWORD` | – | **Required** unless auth is off. |
| `BTRFS_UI_PASSWORD_FILE` | – | Read the password from a file (Docker secret). |
| `BTRFS_UI_AUTH` | `on` | `off` disables the login (only if something in front already authenticates). |
| `BTRFS_UI_READONLY` | off | `1` = view everything, change nothing. |
| `BTRFS_UI_COOKIE_SECURE` | off | `1` when served over HTTPS. |
| `BTRFS_UI_SECRET` | random | Session signing key. Set it if you want logins to survive restarts. |
| `BTRFS_UI_HOST` / `BTRFS_UI_PORT` | `0.0.0.0` / `8080` | Listen address inside the container. |
| `BTRFS_UI_SNAPPER_ROOT` | – | Experimental, see above. |

## Security

This UI can delete subvolumes and snapshots and start long, heavy operations, so treat
it like root access to the storage.

- The container refuses to start without a password (or with an obvious placeholder).
  Login attempts are rate limited.
- The compose file publishes port 8080 so you can reach the UI from anywhere on your
  LAN. Keep it on your LAN and do not forward the port to the internet. The password is
  sent in clear text over plain HTTP; if you want HTTPS, put a reverse proxy in front
  (Caddy, nginx, Traefik) and set `BTRFS_UI_COOKIE_SECURE=1`.
- The session cookie is `HttpOnly` and `SameSite=Strict`, and every change needs a
  custom request header, which blocks cross-site requests.
- Every path you type is resolved and must end up inside a mounted btrfs filesystem
  (`..` and symlinks that leave it are rejected). Commands are run without a shell.
- Deleting is refused for the top-level subvolume, the default subvolume and anything
  mounted in the container.
- `BTRFS_UI_READONLY=1` is a good way to try it out: everything is visible, nothing can
  be changed.

## What is in it

- **Overview**: raw/allocated/used/free space, allocation by profile, per-device size and
  error counters, scrub and balance state, raw `filesystem usage`.
- **Subvolumes**: list with snapshot / read-only / mounted flags and creation time,
  sizes when quotas are enabled, create, snapshot, toggle read-only, delete.
- **Snapper**: list, create and delete snapshots, compare two snapshots, edit the
  timeline/number retention settings, run cleanup.
- **Maintenance**: scrub (start, cancel, resume), balance with usage filters, defragment
  (with optional recompression), trim, and a list of background jobs with their output.

Not included compared with btrfs-assistant: deduplication (bees/duperemove),
btrfsmaintenance configuration, enabling or disabling quotas, snapper rollback, and
adding or removing devices.

## Troubleshooting

- **"No btrfs filesystems are mounted inside this container"**: the container only sees
  paths you bind-mount. Mount the filesystem into it and restart the container.
- **Scrub says "never run"**: the host's `/var/lib/btrfs` is not mounted into the
  container, or that filesystem really has no scrub history. Compare the UUID on the
  Overview tab with the file names in `/var/lib/btrfs` on the host.
- **An update does not show up**: run `docker compose pull && docker compose up -d` and
  check the version in the footer. If you build it yourself, add `--build` instead.
- **Snapper tab is empty**: see "Snapper" above; the configs and the snapshotted
  directories both have to be mounted at the same paths as on the host.

## Development

```sh
pip install -r requirements.txt pytest
python -m pytest tests            # parsers, API, and a loopback-btrfs integration test (needs root)

BTRFS_UI_AUTH=off BTRFS_UI_PORT=8080 python -m app      # run without Docker
```

The parsers are tested against output captured from btrfs-progs 6.6.3 and snapper
0.10.6 (`tests/fixtures`). The integration tests create a btrfs filesystem on a
loopback file and are skipped when that is not possible.

## License

[MIT](LICENSE)
