# jgmDebStore

App store and software updates for **jgmDeb**, a Debian-based OS for the Nokia 8110 4G.
The phone's **Store** app and **Settings > Software update** read this repository over HTTPS:

- `index.json` - catalogue: the latest system release and every app (version, package path, SHA-256)
- `packages/` - the downloadable `.tar.gz` packages (built by `tools/build.py`)
- `apps/<id>/` - app sources (`manifest.json`, `app.py`, optional `icon.png`)
- `system/` - the system update (`files/` are installed into `/opt/jgm` on the phone)

## Apps
| App | What it does |
|---|---|
| JWLink | companion app for the JWatch (Bluetooth LE): scan, sync time, find watch, DND, rename, battery/steps |

## Security model
Packages are checked against the SHA-256 in `index.json` before they are installed, and system updates are
compile-checked, may only write to `/opt/jgm`, and make a backup first (Settings > Software update > Roll back).
Apps run **with full privileges** on the phone, so only install apps from sources you trust. The store address
can be changed in the Store's options.

## Publishing a release
1. Edit `apps/<id>/` (bump `version` in `manifest.json`) and/or `system/files/` (bump `system/VERSION`, update `system/NOTES.txt`).
2. `python3 tools/build.py` regenerates `packages/` and `index.json`.
3. Commit and push. Phones check for updates after each boot and notify you.

Only own code belongs here: no vendor firmware, drivers or blobs.

See [docs/APP_API.md](docs/APP_API.md) to write an app.
