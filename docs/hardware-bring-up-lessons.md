# Hardware bring-up lessons — appliance on a Pi Zero 2 W

> **For future Claude / future sessions:** this is the field record of bringing the
> Buddy3D appliance image up on real hardware for the first time (Pi Zero 2 W +
> OV5647 CSI camera). Every entry is a defect that unit tests and offline
> `validate-image.sh` could not catch, with the symptom seen on the device, the
> root cause, and the fix (with its commit). Read this before changing the image
> layer, units, camera path, or NetworkManager config. It is the fastest way to
> avoid re-learning these on hardware.

## Context

- **Target:** Raspberry Pi Zero 2 W Rev 1.0, OV5647 CSI camera (Pi Camera v1),
  appliance image built by `image/scripts/build-image.sh` on `rpi5.stahmer.lan`
  (native arm64).
- **Environment quirks:** the image uses a read-only ROOT with `overlayroot`,
  the app runs as the unprivileged `prusa-cam` account, and configuration moved
  from the dev-Pi `config.ini` to `/data/prusa-cam/config/device.toml` +
  `secrets.toml`.
- **Diagnostic method that saved the day:** an SSH-injected card
  (`authorized_keys` + pre-generated host keys + `ssh.service`) plus
  `[pi02] dtoverlay=dwc2,dr_mode=host` for a USB-Ethernet path. With SSH you can
  iterate on the device (remount ROOT rw, patch a file, restart a unit) instead
  of swapping the SD card.

## Live view (WebRTC) is gated by Prusa's camera-service registry

Symptom: registration, `/c/info`, snapshots and RTSP all work, but the **live
view** never appears in the Prusa Connect website or the app. The device logs:

```
ICE configured: stun=stun://coturn.prusa3d.com:5349 turn=coturn.prusa3d.com:3478 user=set cred=set
Offer SDP text ready (833 chars, m-lines=['m=video 9 UDP/TLS/RTP/SAVPF 96'])
SIO OUT webrtc ... 5=3 (offer) / 5=4 (candidates)
WebRTC stream ended (no-ice-connection)
```

The device sends its offer and ICE candidates but receives **no answer and no
remote candidates**, so ICE never connects. The web UI exposes only the snapshot
(no `<video>` player for the external camera).

Cause (verified on the device, 2026-09-24):

```
GET https://camera-service-api.prusa3d.com/v1/cameras/<token>
  -> 404 {"statusCode":404,"errorCode":"NOT_FOUND"}
```

`docs/protocol.md` §5 "Server-side gate" already recorded this: the viewer's
`client_authentication` is rejected (ACK 5) for tokens not in Prusa's
camera-service registry and `GET .../v1/cameras/<token>` returns 404, so
"viewers cannot connect and the camera never receives any relayed events".

**Conclusion:** the live stream is blocked by a **Prusa backend gate** (a
real-hardware allowlist or a staged feature rollout — the docs leave which one
open), not by an appliance defect. It cannot be fixed from the device. Snapshots
work because `PUT /c/snapshot` is not gated. The device half of WebRTC is
correct: `/c/info` `registered=True`, signaling `Auth ACK: 0`, `Snapshot: 200`,
offer + candidates emitted, TURN/STUN configured.

## Signed-update acceptance (WP-R4c, 2026-09-24)

The signed application update path was exercised end to end on the device
(no reflash): `image/scripts/make-app-release.sh` built and minisign-signed
`buddy3d-camera-app-<version>.tar.zst` + `update-manifest.json` with the dev key
(public half matches the image's embedded `buddy3d-release.pub`), served over a
throwaway HTTPS server whose CA was trusted via `SSL_CERT_FILE`, and triggered
through the real `prusa-priv install-update` root helper.

- **Install:** factory → `1.0.0` → `1.0.1`. After the three fixes recorded
  below, the installer quiesced the four launcher units, built the release venv in 52 s,
  swapped `current`, restarted the units, and passed health. `ps` confirms the
  app runs `/data/prusa-cam/releases/current/venv/bin/python .../main.py`;
  `current`/`previous` are `1.0.1`/`1.0.0`; `/c/info registered=True`, signaling
  `Auth ACK: 0`, snapshots `200`, RTSP 8554/8555 open, admin `302 -> /admin`.
- **Rollback:** `1.0.2` installed with a deliberately closed health endpoint
  failed the ≤90 s health check and rolled back to `1.0.1`, leaving
  `/data/prusa-cam/releases/.bad/1.0.2.json`
  (`health checks failed; restored the previous release`); the restored release
  kept serving snapshots and Prusa signaling.

Test state (temporary, removed after): `/etc/prusa-updater.conf` pointed at the
test manifest URL with `SSL_CERT_FILE`, and `/data/prusa-cam/test-ca.pem` was
present; both were reverted and ROOT remounted read-only.

## Defects found only on hardware

| # | Symptom on device | Root cause | Fix |
|---|---|---|---|
| 1 | No setup hotspot at all; unclaimed device unreachable | `network-manager` only *Recommends* `dnsmasq-base`, and the image installs without recommends, so NM `ipv4.method shared` could not serve DHCP and the AP activation failed | add `dnsmasq-base` (commit `fb63c70`) |
| 2 | shared-mode NAT backend | same recommends gap for `nftables`/`iptables` | add both (`78bb697`) |
| 3 | ROOT read-only; dnsmasq/NM/systemd/smbd all failed to write | `overlayroot` never activates on this image, so `/` stayed `ro` | keep ROOT read-only (power-off safety) and mount volatile state on tmpfs: `/var` and `/etc/prusa-cam` (`c5c7509`) |
| 4 | `data-ready.target` failed → whole camera target failed (`result 'dependency'`) | `prusa-data-grow` compared the MBR PARTUUID suffix to `-3`, but it is zero-padded `-03` | compare the partition number numerically (`7d3a7ce`) |
| 5 | `finish` joined the station Wi-Fi, then the device went offline | the provisioning service's `ExecStopPost` ran `nmcli device disconnect wlan0`, tearing down the station connection `finish` had just activated | `hotspot.stop` deactivates only the `buddy3d-setup` profile, idempotently (`9d2dc19`) |
| 6 | camera "no CSI sensor detected" although the OV5647 was present | `/dev/dma_heap/*` is `root:root 0600`, so the `prusa-cam` camera stack could not open it ("Could not open any dmaHeap device") | udev rule granting the `video` group (`a14541c`) |
| 7 | camera probe: "still capture failed" | `camera_probe._default_runner` used `subprocess` `text=True`, but `rpicam-still -o -` streams a binary JPEG → UTF-8 decode error | capture bytes; text consumers decode via `_stdout` (`9c2258a`) |
| 8 | `prusa-cam` crash-looped with `KeyError: 'identity'` | `main.load_config` only read the dev-Pi `config.ini`; the appliance has `device.toml`/`secrets.toml` | bridge the durable documents into the legacy cfg shape (`b4cf8d8`) |
| 9 | snapshot 503 "no frame captured"; `sudo: prusa-cam : command not allowed` | gst budget of 5 s too short for the first frame; and `main.py` called `sudo systemctl` directly | gst budget 10 s; route RTSP control through the privileged helper (`681c3bd`) |
| 10 | admin `/api/status` said camera failed while it worked | the probe ran a *second* libcamera consumer while `rpicam-source` owned the sensor | probe only in setup mode (`17b4a86`) |
| 11 | `Fontconfig error: No writable cache directories` spam | GStreamer/fontconfig needs a writable cache; `HOME` is read-only | `XDG_CACHE_HOME=/tmp/prusa-cam` on the camera/RTSP units (`17b4a86`) |
| 12 | USB Ethernet adapter invisible | the Zero 2 W micro-USB data port is OTG and defaults to peripheral mode | `[pi02] dtoverlay=dwc2,dr_mode=host` in `config.txt` (`d041c61`) |
| 13 | Prusa Connect rejected the device: `/c/info` 403, snapshot 400 "Invalid fingerprint", signaling `ACK=1/3` → **no live view** | NetworkManager randomizes the wlan0 MAC during scans; the identity fingerprint is MAC-derived, so it flapped and stopped matching the token binding | `wifi.scan-rand-mac-address=no`; wizard persists the MAC-derived fingerprint (`34af938`) |
| 14 | **Live view still absent; token looked invalid** | config files under `/data/prusa-cam/config/` had been rewritten **as root**, so `prusa-cam` could not read `secrets.toml` and the app sent an **empty token** | keep those files `prusa-cam`-owned (`0640`/`0600`) — operational lesson, see below |
| 15 | Signed install aborts immediately: `install failed (failed): installed version is invalid` | the updater took the "installed version" from the image application version (`0.0.0+local`); the deliberately strict SemVer parser rejects build metadata, so the first install could never run (and on a release device the candidate was always compared against the immutable image version) | resolve the installed version from the active release, and use only the strict SemVer core of the factory version (`747ac8d`) |
| 16 | Install hangs then fails: `release venv creation failed (TimeoutExpired)`; the whole device goes SSH-unresponsive for ~5 min | the release venv build (`python3 -m venv` + `pip install`) runs while the full camera stack is live; on a 415 MB Pi Zero 2 W with no swap the kernel OOM-kills the build and thrashes | quiesce the four launcher units (~110 MB RSS) around the venv build; the build then takes 52 s and peaks ~76 MB, and the runtime is restored on every failure path (`5de54c9`) |
| 17 | Install reports success but the device still runs the **factory** app | `default_restart_services` ran `systemctl restart prusa-camera.target`; the launcher units are `WantedBy=multi-user.target` with no `PartOf=`, so restarting the target restarts none of them and health passed against the stale app | restart the four units that exec `launcher.sh` by name (`5de54c9`) |
| 18 | Same as 17 after fixing the restart: `ps` shows `/opt/prusa-cam/main.py` although `current` points at the release | `tempfile.mkdtemp` creates the staging dir `0700 root`; `switch_release` renamed it unchanged, and the launcher runs as the unprivileged `prusa-cam` user, which cannot traverse a `0700` root dir, so it silently fell back to the factory app | chmod the activated release dir to `0755` (root-owned, world-traversable) in `switch_release` (`22363b2`) |

## Operational lessons (not code bugs)

1. **Config file ownership is load-bearing.** `/data/prusa-cam/config/device.toml`
   and `secrets.toml` must stay owned by `prusa-cam` (`0640` / `0600`). The app
   and the wizard write them correctly as `prusa-cam`; editing them as root (e.g.
   over SSH) makes the app read an empty document and Prusa Connect rejects
   everything. Use `sudo -u prusa-cam` or `chown prusa-cam:prusa-cam` after any
   root-side edit. A symptom of this is `token_len=0` in the app.
2. **Prefer SSH over SD-card swaps.** The diagnostic card injection (host key +
   `authorized_keys` + `ssh.service`) and USB host mode give a stable wired SSH
   path. On-device iteration is far faster than rebuild + reflash.
3. **Persist forensics to the FAT partition.** The journal is volatile and ROOT
   is an overlay, so `bootlog.sh` writes unit states, `nmcli`, camera detection
   and the relevant journals to `/boot/firmware/bootlog.txt` — readable on any
   PC, no console needed. It is also `WantedBy=prusa-camera.target` so the
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
