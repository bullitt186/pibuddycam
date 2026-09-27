# Hardware bring-up lessons — appliance on a Pi Zero 2 W

> This is the field record of bringing the
> PiBuddyCam appliance image up on real hardware for the first time (Pi Zero 2 W +
> OV5647 CSI camera). Every entry is a defect that unit tests and offline
> `validate-image.sh` could not catch, with the symptom seen on the device, the
> root cause, and the fix (with its commit). Read this before changing the image
> layer, units, camera path, or NetworkManager config. It is the fastest way to
> avoid re-learning these on hardware.

## Context

- **Target:** Raspberry Pi Zero 2 W Rev 1.0, OV5647 CSI camera (Pi Camera v1),
  appliance image built by `image/scripts/build-image.sh` on the native arm64 build host (see `.agent/pi-ops.md`)
  (native arm64).
- **Environment quirks:** the image uses a direct read-only ext4 ROOT (`overlayroot` is present but
  did not activate), explicit tmpfs mounts for `/var` and `/etc/pibuddycam`, and durable `/data`.
  The app runs as the unprivileged `pibuddycam` account, and configuration moved
  from the dev-Pi `config.ini` to `/data/pibuddycam/config/device.toml` +
  `secrets.toml`.
- **Diagnostic method that saved the day:** an SSH-injected card
  (`authorized_keys` + pre-generated host keys + `ssh.service`) plus
  `[pi02] dtoverlay=dwc2,dr_mode=host` for a USB-Ethernet path. With SSH you can
  iterate on the device (remount ROOT rw, patch a file, restart a unit) instead
  of swapping the SD card.

## Current Connect live-view state (updated 2026-09-25)

Symptom: registration, `/c/info`, snapshots and RTSP all work, but the **live
view** never appears in the Prusa Connect website or the app. The device logs:

```
ICE configured: stun=stun://coturn.prusa3d.com:5349 turn=coturn.prusa3d.com:3478 user=set cred=set
Offer SDP text ready (833 chars, m-lines=['m=video 9 UDP/TLS/RTP/SAVPF 96'])
SIO OUT webrtc ... 5=3 (offer) / 5=4 (candidates)
WebRTC stream ended (no-ice-connection)
```

In the 2026-09-24 probe the device sent its offer and ICE candidates but received **no answer and
no remote candidates**, so ICE never connected. A clean browser session on 2026-09-25 instead
listed the device under “Other cameras” and exposed neither live nor settings controls.

Cause (verified on the device, 2026-09-24):

```
GET https://camera-service-api.prusa3d.com/v1/cameras/<token>
  -> 404 {"statusCode":404,"errorCode":"NOT_FOUND"}
```

The copied known-working SD token also returned 404. Historical WebRTC viewer probes received
`client_authentication` ACK 5, but a 2026-09-25 non-WebRTC control viewer received ACK 0 and
successfully relayed `trigger` and nested `configuration`; that configuration drove a live
FHD→HD→FHD test on application release `1.0.4`.

**Conclusion:** the normal live-view path is currently blocked by Connect UI/backend enrollment,
not by a demonstrated media regression. Registry 404, UI classification, viewer authentication,
control relay, and WebRTC admission are separate observations; do not collapse them into “no
viewer events can relay.” The device half of WebRTC was live-verified on 2026-09-19/20, while the
latest 2026-09-25 replay verified control/configuration only. Full WebRTC was not rerun after that
state change.

## Signed-update acceptance (WP-R4c, 2026-09-24)

The signed application update path was exercised end to end on the device
(no reflash): `image/scripts/make-app-release.sh` built and minisign-signed
`pibuddycam-app-<version>.tar.zst` + `update-manifest.json` with the dev key
(public half matches the image's embedded `pibuddycam-release.pub`), served over a
throwaway HTTPS server whose CA was trusted via `SSL_CERT_FILE`, and triggered
through the real `pibuddycam-priv install-update` root helper.

- **Install:** factory → `1.0.0` → `1.0.1`. After the three fixes recorded
  below, the installer quiesced the four launcher units, built the release venv in 52 s,
  swapped `current`, restarted the units, and passed health. `ps` confirms the
  app runs `/data/pibuddycam/releases/current/venv/bin/python .../main.py`;
  `current`/`previous` are `1.0.1`/`1.0.0`; `/c/info registered=True`, signaling
  `Auth ACK: 0`, snapshots `200`, RTSP 8554/8555 open, admin `302 -> /admin`.
- **Rollback:** `1.0.2` installed with a deliberately closed health endpoint
  failed the ≤90 s health check and rolled back to `1.0.1`, leaving
  `/data/pibuddycam/releases/.bad/1.0.2.json`
  (`health checks failed; restored the previous release`); the restored release
  kept serving snapshots and Prusa signaling.

Test state (temporary, removed after): `/etc/pibuddycam-updater.conf` pointed at the
test manifest URL with `SSL_CERT_FILE`, and `/data/pibuddycam/test-ca.pem` was
present; both were reverted and ROOT remounted read-only.

## Admin TLS provisioning (appliance image/security defect — live-verified 2026-09-26)

Live diagnosis found `pibuddycam-admin` serving HTTP on `:443` because
`/etc/pibuddycam/admin.env` did not exist. `admin_app.py` only *consumed*
`ADMIN_TLS_CERT`/`ADMIN_TLS_KEY`; nothing generated them. This is an appliance
image/security defect, not a firmware `GAP-*` item.

**Repo implementation (this change):**

- `pi-impersonator/admin_tls.py` generates a device self-signed keypair once,
  durably, under `/data/pibuddycam/config/admin-tls/` (key `0600`, cert `0644`,
  both `pibuddycam`-owned) and recreates the volatile `/etc/pibuddycam/admin.env`
  (`0640`) on every boot. The SAN covers `pibuddycam-<device-id>.local`; a LAN IP
  change never regenerates the keypair.
- `pi-persist.service` (root, `Before=data-ready.target pibuddycam-admin.service`)
  calls it via `persist_restore._provision_admin_tls`; a provisioning failure is
  logged and isolated so the camera still starts.
- `admin_app.py` now **fails closed** in `admin` mode: missing or invalid TLS
  makes `run` raise `SystemExit` instead of serving plaintext. The setup portal
  stays plain HTTP on `192.168.4.1`.
- `openssl` is added explicitly to the image package manifest; it is invoked as
  an argv list (never through a shell).
- `validate-image.sh` asserts the module, the `openssl` binary, the admin unit's
  `admin.env` wiring, the setup-portal HTTP behavior, and the fail-closed
  `admin_app` wiring.

**Live verification (application release `1.1.8`, source `0474b46`, 2026-09-26):**
the signed updater installed the exact reviewed application release, after which
the committed image-owned `pi-persist.service` and `pibuddycam-admin.service` files
were installed on ROOT for the authorized hardware test. `pi-persist` created
the durable keypair and volatile `admin.env`; the keypair was byte-identical
after a second `admin_tls.ensure()` call. The certificate SAN includes
`pibuddycam-<device-id>.local`, `https://<device-ip>/admin` returned HTTP 200 over TLS,
and a plaintext HTTP request to port 443 was rejected. The key directory/key/
cert/env ownership and modes were respectively `pibuddycam:pibuddycam`
`0750`/`0600`/`0644`/`0640`. Snapshot returned JPEG, RTSP returned H.264
640×480, all camera/admin services were active, temporary updater URL/CA files
were removed, and ROOT was restored read-only. Before provisioning, release
`1.1.8` also live-demonstrated fail-closed behavior: `pibuddycam-admin` exited with
the expected missing-TLS critical error instead of serving HTTP.

A fresh-image boot and setup-hotspot regression remain to be exercised when a
new card is built and user-flashed; offline tests and image validation cover
those paths in the meantime.

## Rename to PiBuddyCam (2026-09-27)

The project, repository (`bullitt186/pibuddycam`, formerly `prusa-buddy3d-camera-re`) and runtime
were renamed. Images and application releases from 1.4.0 on use the new names:
- service account `pibuddycam`;
- `/data/pibuddycam`, `/etc/pibuddycam`, `/opt/pibuddycam`, `/usr/share/pibuddycam`;
- units `pibuddycam*.service` and `pibuddycam.target`;
- helper `/usr/libexec/pibuddycam/pibuddycam-priv`;
- updater config `/etc/pibuddycam-updater.conf` and `PIBUDDYCAM_*` variables;
- hostname `pibuddycam`, setup SSID `PiBuddyCam-Setup-<last6>`;
- MQTT prefix `pibuddycam`.

- **Reflash required.** None of this can arrive over the air. A pre-rename device must be reflashed
  (which recreates `/data`) and re-onboarded.
- **An old image refuses a new bundle.** A 1.4.0+ application bundle installed over the air onto a
  pre-rename image refuses to start (`image_guard`, exit 78). The old updater's health probe then
  rolls back to the previous release.
- **Home Assistant shows a new device** because the discovery IDs changed. Delete the old
  `buddy3d_*` device and its retained discovery topics.
- **Updates work out of the box.** New images ship
  `PIBUDDYCAM_UPDATE_MANIFEST_URL=https://github.com/bullitt186/pibuddycam/releases/latest/download/update-manifest.json`.

## Image rotation setting (implemented 2026-09-27; hardware verification pending)

- `rpicam-source.service` no longer hardcodes `--rotation 180`. It runs `launcher.sh
  camera_source.py`, so the pipeline ships with the signed app release. It reads `CAM_ROTATION`
  from `/etc/pibuddycam/rotation.env`, which `pi-persist` restores from `state.json` `rotation`.
  **Behavior change:** the default is 0°, so a device mounted upside down must select 180° once
  in the console.
- The unit change and the new `gstreamer1.0-libcamera` package are image-owned. An app bundle
  alone keeps the old unit, and that unit still hardcodes 180° and ignores the setting.
- Before trusting 90°/270° on a Pi Zero 2 W, verify three things:
  1. `libcamerasrc` exists: `gst-inspect-1.0 libcamerasrc`.
  2. The bcm2835 ISP m2m node exposes `rotate`: run `v4l2-ctl -d /dev/video12 -l` on the node
     named `bcm2835-codec-isp`. `camera_source.detect_rotation_backend` uses the same
     `VIDIOC_QUERYCTRL` probe.
  3. The encoder accepts 1080×1920 portrait NV12 at 30 fps.

  Without ISP rotation the software `videoflip` fallback runs at 10 fps. Measure its CPU cost.

## Defects found only on hardware

| # | Symptom on device | Root cause | Fix |
|---|---|---|---|
| 1 | No setup hotspot at all; unclaimed device unreachable | `network-manager` only *Recommends* `dnsmasq-base`, and the image installs without recommends, so NM `ipv4.method shared` could not serve DHCP and the AP activation failed | add `dnsmasq-base` (commit `fb63c70`) |
| 2 | shared-mode NAT backend | same recommends gap for `nftables`/`iptables` | add both (`78bb697`) |
| 3 | ROOT read-only; dnsmasq/NM/systemd/smbd all failed to write | `overlayroot` never activates on this image, so `/` stayed `ro` | keep ROOT read-only (power-off safety) and mount volatile state on tmpfs: `/var` and `/etc/pibuddycam` (`c5c7509`) |
| 4 | `data-ready.target` failed → whole camera target failed (`result 'dependency'`) | `pibuddycam-data-grow` compared the MBR PARTUUID suffix to `-3`, but it is zero-padded `-03` | compare the partition number numerically (`7d3a7ce`) |
| 5 | `finish` joined the station Wi-Fi, then the device went offline | the provisioning service's `ExecStopPost` ran `nmcli device disconnect wlan0`, tearing down the station connection `finish` had just activated | `hotspot.stop` deactivates only the `pibuddycam-setup` profile, idempotently (`9d2dc19`) |
| 6 | camera "no CSI sensor detected" although the OV5647 was present | `/dev/dma_heap/*` is `root:root 0600`, so the `pibuddycam` camera stack could not open it ("Could not open any dmaHeap device") | udev rule granting the `video` group (`a14541c`) |
| 7 | camera probe: "still capture failed" | `camera_probe._default_runner` used `subprocess` `text=True`, but `rpicam-still -o -` streams a binary JPEG → UTF-8 decode error | capture bytes; text consumers decode via `_stdout` (`9c2258a`) |
| 8 | `pibuddycam` crash-looped with `KeyError: 'identity'` | `main.load_config` only read the dev-Pi `config.ini`; the appliance has `device.toml`/`secrets.toml` | bridge the durable documents into the legacy cfg shape (`b4cf8d8`) |
| 9 | snapshot 503 "no frame captured"; `sudo: pibuddycam : command not allowed` | gst budget of 5 s too short for the first frame; and `main.py` called `sudo systemctl` directly | gst budget 10 s; route RTSP control through the privileged helper (`681c3bd`) |
| 10 | admin `/api/status` said camera failed while it worked | the probe ran a *second* libcamera consumer while `rpicam-source` owned the sensor | probe only in setup mode (`17b4a86`) |
| 11 | `Fontconfig error: No writable cache directories` spam | GStreamer/fontconfig needs a writable cache; `HOME` is read-only | `XDG_CACHE_HOME=/tmp/pibuddycam` on the camera/RTSP units (`17b4a86`) |
| 12 | USB Ethernet adapter invisible | the Zero 2 W micro-USB data port is OTG and defaults to peripheral mode | `[pi02] dtoverlay=dwc2,dr_mode=host` in `config.txt` (`d041c61`) |
| 13 | Prusa Connect rejected the device: `/c/info` 403, snapshot 400 "Invalid fingerprint", signaling `ACK=1/3` → **no live view** | NetworkManager randomizes the wlan0 MAC during scans; the identity fingerprint is MAC-derived, so it flapped and stopped matching the token binding | `wifi.scan-rand-mac-address=no`; wizard persists the MAC-derived fingerprint (`34af938`) |
| 14 | **Live view still absent; token looked invalid** | config files under `/data/pibuddycam/config/` had been rewritten **as root**, so `pibuddycam` could not read `secrets.toml` and the app sent an **empty token** | keep those files `pibuddycam`-owned (`0640`/`0600`) — operational lesson, see below |
| 15 | Signed install aborts immediately: `install failed (failed): installed version is invalid` | the updater took the "installed version" from the image application version (`0.0.0+local`); the deliberately strict SemVer parser rejects build metadata, so the first install could never run (and on a release device the candidate was always compared against the immutable image version) | resolve the installed version from the active release, and use only the strict SemVer core of the factory version (`747ac8d`) |
| 16 | Install hangs then fails: `release venv creation failed (TimeoutExpired)`; the whole device goes SSH-unresponsive for ~5 min | the release venv build (`python3 -m venv` + `pip install`) runs while the full camera stack is live; on a 415 MB Pi Zero 2 W with no swap the kernel OOM-kills the build and thrashes | quiesce the four launcher units (~110 MB RSS) around the venv build; the build then takes 52 s and peaks ~76 MB, and the runtime is restored on every failure path (`5de54c9`) |
| 17 | Install reports success but the device still runs the **factory** app | `default_restart_services` ran `systemctl restart pibuddycam.target`; the launcher units are `WantedBy=multi-user.target` with no `PartOf=`, so restarting the target restarts none of them and health passed against the stale app | restart the four units that exec `launcher.sh` by name (`5de54c9`) |
| 18 | Same as 17 after fixing the restart: `ps` shows `/opt/pibuddycam/main.py` although `current` points at the release | `tempfile.mkdtemp` creates the staging dir `0700 root`; `switch_release` renamed it unchanged, and the launcher runs as the unprivileged `pibuddycam` user, which cannot traverse a `0700` root dir, so it silently fell back to the factory app | chmod the activated release dir to `0755` (root-owned, world-traversable) in `switch_release` (`22363b2`) |
| 19 | Connect configuration reached the app, but video quality never changed; journal said `sudo: pibuddycam : command not allowed` | the quality path still called broad `sudo systemctl` commands, which the appliance's intentionally narrow sudoers policy rejects | add fixed `pibuddycam-priv quality-restart` and route quality changes through it (`c1d3e76`; live-verified HD→FHD on `1.0.4`) |
| 20 | claimed admin console served **plaintext HTTP on :443** (diagnosed live) | `/etc/pibuddycam/admin.env` was absent: `admin_app.py` consumed `ADMIN_TLS_CERT`/`ADMIN_TLS_KEY` but no boot-time generator existed, and `/etc/pibuddycam` is tmpfs while ROOT is read-only | boot-time `admin_tls.ensure()` from `pi-persist.service` generates a durable `/data` keypair and recreates `admin.env`; `admin_app` now fails closed without it. Live-verified on release `1.1.8` (`0474b46`) on 2026-09-26; fresh-image/setup-hotspot verification remains pending (see above) |
| 21 | local console rendered the boot spinner, sign-in form and authenticated shell **all at once**; ghost buttons invisible on light cards; 360px viewport scrolled horizontally; monitor/snapshot frames never appeared | `[hidden]` was overridden by per-component `display` rules, the dark topbar ghost-button palette leaked onto light cards, the mobile nav could not shrink/wrap, and the CSP `img-src` omitted `blob:` while the monitor used `createObjectURL`; the re-auth promise was dropped on expiry and the MQTT/Prusa forms also ran the generic settings submit | enforce `[hidden]` with `display:none !important`, scope the dark ghost style to `.topbar`, let the mobile nav wrap/shrink, add `blob:` to `img-src`, settle a pending re-auth as cancelled, and skip the generic submit without a `data-setting` (`44a2a58`; live-verified on application release `1.1.9` on 2026-09-26) |

## Operational lessons (not code bugs)

1. **Config file ownership is load-bearing.** `/data/pibuddycam/config/device.toml`
   and `secrets.toml` must stay owned by `pibuddycam` (`0640` / `0600`). The app
   and the wizard write them correctly as `pibuddycam`; editing them as root (e.g.
   over SSH) makes the app read an empty document and Prusa Connect rejects
   everything. Use `sudo -u pibuddycam` or `chown pibuddycam:pibuddycam` after any
   root-side edit. A symptom of this is `token_len=0` in the app.
2. **Prefer SSH over SD-card swaps.** The diagnostic card injection (host key +
   `authorized_keys` + `ssh.service`) and USB host mode give a stable wired SSH
   path. On-device iteration is far faster than rebuild + reflash.
3. **Persist forensics to the FAT partition.** The journal is volatile and `/var` is tmpfs, so
   `bootlog.sh` writes unit states, `nmcli`, camera detection
   and the relevant journals to `/boot/firmware/bootlog.txt` — readable on any
   PC, no console needed. It is also `WantedBy=pibuddycam.target` so the
   claim→runtime boot is captured.
4. **The first real boot is a different program.** `validate-image.sh` (98/0/1)
   passes on things that fail live: missing recommends, `overlayroot` not
   activating, an unmounted MAC-address assumption, PARTUUID formatting. Treat
   the validator as necessary, not sufficient.
5. **NetworkManager MAC randomization breaks MAC-derived identity.** Any
   identity derived from `wlan0`'s MAC must disable scan-time randomization, or
   persist the value, or it will flap.
6. **Single-consumer libcamera.** Only one process may own the sensor. Probe only
   pre-runtime; run the camera through `rpicam-source`/`stream_mux`, and pull
   snapshots from the mux (`127.0.0.1:8888`), never a second `rpicam` capture
   while the source runs.
7. **Prusa Connect token binding.** A registration token binds to the first
   fingerprint that uses it; a mismatch is `{"detail":"Invalid fingerprint"}`
   (snapshot) / `403` (`/c/info`), and signaling returns `ACK=1` (not authorized)
   or `ACK=3` (missing token). `ACK=0` is the only success.

## Commit map

`fb63c70` dnsmasq · `78bb697` NAT backends · `c5c7509` ro ROOT + tmpfs · `7d3a7ce`
data-grow PARTUUID · `9d2dc19` hotspot stop · `a14541c` dma_heap udev · `9c2258a`
probe bytes · `b4cf8d8` config bridge · `681c3bd` capture budget + RTSP helper ·
`17b4a86` probe gating + fontconfig cache · `d041c61` USB host mode · `34af938`
fingerprint stability.

Signed-update acceptance (WP-R4c): `747ac8d` installed-version resolution ·
`5de54c9` quiesce for the venv build + restart real units · `22363b2` traversable
release directory.

Live quality control: `c1d3e76` fixed-verb source/RTSP restart (application release
`1.0.4`, live-verified HD→FHD on the OV5647 pipeline).

Signaling recovery: `66d2de4` bounded initial auth and prevents disconnect deadlock after a failed
ACK. Acceptance record: `cb2ab83`.
