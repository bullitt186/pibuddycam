# Hardware

What PiBuddyCam runs on, how the card is laid out, camera specifics, and the lessons from bringing
the appliance up on real hardware. For the shopping list, see [install](install.md#what-you-need).

## Board

- **Raspberry Pi Zero 2 W** (quad Cortex-A53, 512 MB RAM, about 415 MB usable, 2.4 GHz Wi-Fi) is
  the only supported and validated board. The image targets its device-tree set.
- Memory is tight. Installing an update briefly stops the camera services while it builds the
  release's Python environment (see defect 16 below).
- The micro-USB data port is OTG. For a USB Ethernet adapter during development, add
  `[pi02] dtoverlay=dwc2,dr_mode=host` to `config.txt`.

## Card layout

Three partitions: `BOOT` (FAT, 512 MiB), `ROOT` (ext4, 4 GiB, mounted **read-only**) and
`PERSIST` (ext4, grown to fill the card on first boot, mounted at `/data`). Runtime files live on
tmpfs (`/var`, `/etc/pibuddycam`), so a power cut can't corrupt the system. The exact layout,
service ordering and build are in [`image/README.md`](../image/README.md). What lives on `/data` is
in the [user guide](user-guide.md#your-data).

## GPIO header

The timelapse [GPIO trigger](user-guide.md#timelapse-gpio-trigger-prusa-gpio-hackerboard) reads
the 40-pin header through the kernel's GPIO character device (`/dev/gpiochip*`, uAPI v2), with
the internal pull-up enabled and a falling edge as the event.

**Electrical reasoning.** A Prusa GPIO Hackerboard output is open-drain: when active it connects
the pin to ground, otherwise it floats. OUT0–OUT3 switch up to 24 V / 500 mA to ground; OUT4–OUT7
are 3.3 V logic. An input with a pull-up to 3.3 V therefore reads any of them directly. The only
wires are `OUTn` to the Pi GPIO and Hackerboard GND to a Pi ground pin. Never put a printer
voltage on the Pi.

**Offered pins** (BCM number, header pin, nearest ground pin): GPIO4 (7, 9), 5 (29, 30),
6 (31, 30), 12 (32, 34), 13 (33, 34), 16 (36, 34), 17 (11, 9), 18 (12, 14), 19 (35, 34),
20 (38, 39), 21 (40, 39), 22 (15, 14), 23 (16, 14), 24 (18, 20), 25 (22, 20), 26 (37, 39),
27 (13, 14). The suggested defaults are GPIO17 for the layer pulse and GPIO27 for the recording
pin.

**Never offered:** GPIO0/1 (HAT ID EEPROM), GPIO2/3 (I2C1 with fixed 1.8 kΩ pull-ups), GPIO7–11
(SPI0) and GPIO14/15 (UART console).

**Access.** The `pibuddycam` account needs the `gpio` group and a udev rule
(`image/assets/udev/60-pibuddycam-gpio.rules`); both ship with the image. **[assumption]** The
chip label on a Pi Zero 2 W is `pinctrl-bcm2835`; verify with `gpioinfo` on a device.

## Camera and rotation

- **Sensor:** the OV5647 (Pi Camera v1) is tested. Other libcamera sensors are expected to work
  but stay experimental until they pass the capture matrix. libcamera allows **one** process to
  own the sensor. `rpicam-source` owns it and fans H.264 out on `127.0.0.1:8888`; everything else
  (RTSP, WebRTC, snapshots, timelapse, console) reads that stream.
- **Rotation 0°/180°:** `rpicam-vid --rotation`, done with sensor flips at no cost.
- **Rotation 90°/270°:** the ISP can't transpose through libcamera, so a GStreamer source is used
  instead. It rotates in the bcm2835 ISP (`v4l2convert`) when the kernel exposes a rotate control,
  and falls back to software `videoflip` at 10 fps otherwise. **Not yet verified on hardware.** To
  verify on a device:
  1. `gst-inspect-1.0 libcamerasrc` shows the element (package `gstreamer1.0-libcamera`).
  2. `v4l2-ctl -d /dev/video12 -l`, run on the node named `bcm2835-codec-isp`, lists `rotate`.
     `camera_source.detect_rotation_backend` uses the same probe.
  3. The encoder accepts 1080×1920 NV12 at 30 fps. Measure CPU use with the software fallback.

## Field notes from hardware bring-up

Defects that unit tests and the offline image validator could not catch, found on a Pi Zero 2 W
with an OV5647. Read this before changing the image, units, camera path or network setup.

### Defects found only on hardware

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
| 20 | claimed admin console served **plaintext HTTP on :443** (diagnosed live) | `/etc/pibuddycam/admin.env` was absent: `admin_app.py` consumed `ADMIN_TLS_CERT`/`ADMIN_TLS_KEY` but no boot-time generator existed, and `/etc/pibuddycam` is tmpfs while ROOT is read-only | boot-time `admin_tls.ensure()` from `pi-persist.service` generates a durable `/data` keypair and recreates `admin.env`; `admin_app` now fails closed without it. Live-verified on release `1.1.8` (`0474b46`) on 2026-09-26; fresh-image/setup-hotspot verification remains pending |
| 21 | local console rendered the boot spinner, sign-in form and authenticated shell **all at once**; ghost buttons invisible on light cards; 360px viewport scrolled horizontally; monitor/snapshot frames never appeared | `[hidden]` was overridden by per-component `display` rules, the dark topbar ghost-button palette leaked onto light cards, the mobile nav could not shrink/wrap, and the CSP `img-src` omitted `blob:` while the monitor used `createObjectURL`; the re-auth promise was dropped on expiry and the MQTT/Prusa forms also ran the generic settings submit | enforce `[hidden]` with `display:none !important`, scope the dark ghost style to `.topbar`, let the mobile nav wrap/shrink, add `blob:` to `img-src`, settle a pending re-auth as cancelled, and skip the generic submit without a `data-setting` (`44a2a58`; live-verified on application release `1.1.9` on 2026-09-26) |

### Operational lessons

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
