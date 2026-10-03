# Development

How to work on PiBuddyCam: repository layout, tests, CI, building images and testing on hardware.
Contribution rules (commits, PRs, what must never be committed) are in
[CONTRIBUTING.md](../CONTRIBUTING.md). How the pieces fit together is in
[architecture](architecture.md).

## Repository layout

| Path | Contents |
|---|---|
| `app/` | The appliance runtime (Python), its systemd units (`app/systemd/`) and the web console (`app/web/`). See [app/README.md](../app/README.md) |
| `image/` | The SD-card image build (rpi-image-gen layer, installer, validator, release scripts). See [image/README.md](../image/README.md) |
| `tests/` | `app/`, `image/` and `docs/` unit tests; `e2e/` Playwright console tests |
| `docs/` | Documentation. `reverse-engineering/` holds the protocol knowledge |
| `research/` | Firmware-analysis helpers (Ghidra scripts) |
| `.github/workflows/` | CI and the release pipeline |
| `_archive/` | Historical material. Not maintained |

## Runtime stack

| Component | Used for |
|---|---|
| Python 3.13 (Debian trixie), stdlib-first | All application code. Third-party wheels are pinned with hashes in `image/requirements.lock`: aiohttp, python-socketio, paho-mqtt |
| GStreamer + PyGObject | RTSP servers (`gst-rtsp-server`), WebRTC (`webrtcbin`, needs `gstreamer1.0-nice`), JPEG capture |
| rpicam-apps / libcamera | Camera capture (`rpicam-vid`, `libcamerasrc`) |
| NetworkManager | Wi-Fi station and the setup hotspot |
| rpi-image-gen (pinned) | Building the image |
| minisign | Signing and verifying releases |

## Tests

The unit tests need only the Python standard library. PyYAML, `minisign`, `mtools` and
`dosfstools` are optional; without them a few image and signature tests are skipped.

```sh
python3 -m unittest discover -s tests -t .      # all unit tests
python3 -m unittest tests.app.test_rotation -v   # one module
python3 -m compileall -q app tests               # byte-compile
for f in image/scripts/*.sh app/*.sh image/assets/*.sh image/assets/pibuddycam-priv \
         image/assets/networkmanager/dispatcher.d/*; do bash -n "$f"; done   # shell syntax
```

The web console has an offline end-to-end suite that drives Chromium against a fake runtime:

```sh
npm install -g playwright && npx playwright install chromium   # once
NODE_PATH="$(npm root -g)" tests/e2e/run.sh
```

CI runs it in the `console-e2e` job (with `E2E_REQUIRED=1`, so a missing browser fails instead of
skipping). Run it locally when you change `app/web/` or the admin HTTP API.

The console script is split into ES modules under `app/web/` (`app.js` is the entry point). They
import each other with `?v=__ASSET_VERSION__`, which the server replaces with the content hash, so
every new module must also be added to `ASSET_ALLOWLIST` in `admin_http.py`.
The setup wizard is a separate page, `setup.html` with the self-contained `setup.js` (it imports
no console module, so the captive-portal page stays small). The E2E harness serves it with
`POST /__e2e/reset {"mode": "setup"}`; the harness has no `/data` mount, so the provisioning
state does not advance past `unclaimed` there.

The `tests/app` package redirects every path that points into `/data` to a throw-away directory
(`tests/app/__init__.py`); a test that needs a real path must inject it.

There is no formatter or type checker for the repository. Match the surrounding style.

## Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request, on Python 3.12 and 3.13:
- the unit tests, with PyYAML, minisign, mtools and dosfstools installed, plus the hash-locked
  runtime dependencies so the aiohttp-based tests run (they skip without it);
- the console end-to-end suite (a separate `console-e2e` job);
- byte-compilation;
- `bash -n` on all scripts;
- a secret scan of the changed files (`image/scripts/scan-secrets.sh`);
- `git diff --check`.

The tests run on a fresh checkout. A file that exists only on your disk (for example, one excluded
by `.gitignore`) passes locally and fails in CI. The image tests check this for required files.

Keeping CI green:
- **Optional tools.** A test that needs one (PyYAML, minisign, openssl, sfdisk, …) uses
  `unittest.skipUnless`. Add the tool to the CI install step so the test really runs there.
- **Required files.** A file the build or tests need must be tracked, with a `.gitignore`
  negation if a pattern like `*.png` would exclude it. Add it to the required-files list in
  `tests/image/test_image_scaffolding.py`.
- **Secret scan.** The scan uses source mode for `.py`/`.rs`/`.proto` files and artifact mode for
  everything else. Test directories, fixtures and docs (including `_archive/docs/`) are excluded. Names like `token=…` or `password=…` in
  shell or YAML trip it, so pick neutral variable names. Placeholders use `<PLACEHOLDER>` form.

Releases are built by `.github/workflows/release.yml` on a native arm64 runner. See
[releasing](releasing.md).

## Building an image

Images are built natively on arm64 Debian trixie, for example on a Raspberry Pi 5. Foreign-architecture
builds are refused.

```sh
git clone --branch v2.8.0 --depth 1 https://github.com/raspberrypi/rpi-image-gen /path/to/rpi-image-gen
RPI_IMAGE_GEN_DIR=/path/to/rpi-image-gen image/scripts/build-image.sh
```

The pinned revision, layout, validation (`validate-image.sh`) and release assembly are described
in [image/README.md](../image/README.md).

## Testing on hardware

- **Application changes:** build a signed bundle with `image/scripts/make-app-release.sh` (see
  [releasing](releasing.md#manual-fallback-application-bundle-only)). Serve it with a manifest,
  point the device's `/etc/pibuddycam-updater.conf` at it, and install from the console.
  Don't copy files onto a running device. `/opt/pibuddycam` is only the factory fallback.
- **Image changes** (units, packages, helpers, boot configuration): build and flash a new image.
  For a quick experiment you can remount `ROOT` read-write and install the exact file from the
  repository. That write **persists**, so remount read-only afterwards and commit the identical
  change to `image/`.
- **Configuration files** must stay owned by `pibuddycam`: `device.toml` `0640`, `secrets.toml`
  `0600`. A root-owned `secrets.toml` makes the app send an empty token.
- **Developer image.** `image/scripts/build-image.sh --dev` builds an image with SSH on, for
  debugging. Point `PIBUDDYCAM_DEV_SSH_PUBKEY_FILE` at an SSH **public** key. The image has a
  `pibuddydev` login (key only, no password, no root login) with passwordless `sudo`, so you can
  remount `ROOT` read-write and edit `/opt/pibuddycam` or `/data/pibuddycam`. The build records
  `channel: dev` in `build-info.json`. `ROOT` is read-only, so the SSH host key is created once on
  `/data/pibuddycam/ssh` and survives reboots. A dev image also serves the unclaimed setup wizard on
  every interface (Ethernet included, `http://<ip>/`), so anyone on the LAN can claim an unclaimed
  dev camera; release images keep it on the setup hotspot only. **Never publish a dev image**: `release.yml` always builds
  the `release` channel, and `validate-image.sh` fails a release image that carries dev SSH material.
- A diagnostic card with SSH enabled (Raspberry Pi Imager can do this) and a USB Ethernet adapter
  (`[pi02] dtoverlay=dwc2,dr_mode=host`) make on-device iteration much faster.

The [field notes](hardware.md#field-notes-from-hardware-bring-up) list defects that only showed up
on real hardware. Read them before changing the image, units or camera path.

## Protocol work

Changes to how PiBuddyCam talks to Prusa Connect start from the
[reverse-engineering docs](reverse-engineering/README.md). Every protocol change names a `GAP-*`
item in the [gap tracker](reverse-engineering/gap-tracker.md), is backed by firmware or capture
evidence, and adds decoded or golden-byte tests. Never infer a field number or enum value.
