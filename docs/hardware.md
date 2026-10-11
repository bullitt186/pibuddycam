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

**Self-test.** With a jumper between two offered pins, stop the camera runtime and run the
test as the service account (it exercises the group and udev rule too):

```sh
sudo systemctl stop pibuddycam.service
sudo -u pibuddycam /opt/pibuddycam/venv/bin/python /opt/pibuddycam/gpio_selftest.py \
     --out-pin 27 --in-pin 17
sudo systemctl start pibuddycam.service
```

It finds the chip, checks that the pull-up reads high, that falling and rising edges arrive
when the output toggles, and that a glitch shorter than the debounce time is filtered. It
changes no configuration. Its result settles the chip-label assumption above.

## Camera and rotation

- **Sensor:** the OV5647 (Pi Camera v1) is tested. Other libcamera sensors are expected to work
  but stay experimental until they pass the capture matrix. libcamera allows **one** process to
  own the sensor. `rpicam-source` owns it and fans H.264 out on `127.0.0.1:8888`; everything else
  (RTSP, WebRTC, snapshots, timelapse, console) reads that stream.
- **Rotation 0°:** plain `rpicam-vid`.
- **Rotation 180°:** a GStreamer source that flips in the bcm2835 ISP (`v4l2convert` with
  `horizontal_flip`/`vertical_flip`, dmabuf to the encoder). On a Pi Zero 2 W the m2m ISP
  manages 30 fps up to 720p but only about 25 fps at 1080p, so 1080p runs at 25 fps. It uses
  about 10 percentage points more total CPU than `rpicam-vid` (≈21% instead of ≈11%)
  **[confirmed]**. At 1080p the `gst-launch` process uses 83–85% of a core, 71% of it in one
  streaming thread in user space, so most likely the copy of each frame from `libcamerasrc` into
  the ISP **[assumption]**. That copy can't be avoided: `v4l2convert output-io-mode=dmabuf-import`
  (also at 1920×1088) and `libcamerasrc` straight into `v4l2h264enc output-io-mode=dmabuf-import`
  both stop at once with "cannot import buffers from upstream" (libcamera 0.7.2, kernel 6.18.50,
  2026-10-11) **[confirmed]**. Sensor flips (`rpicam-vid --rotation 180`) are not used; see field note 22.
  Without ISP flip controls it falls back to software `videoflip`, which manages only about
  2–3 fps at 1080p.
- **Rotation 90°/270°:** the ISP can't transpose through libcamera, so a GStreamer source is used
  instead. It rotates in the bcm2835 ISP (`v4l2convert`) when the kernel exposes a rotate control,
  and falls back to software `videoflip` at 10 fps otherwise. The tested kernel (6.18, Pi Zero 2 W)
  exposes **no** rotate control, so 90°/270° run in software there. **Not yet verified on
  hardware.** To verify on a device:
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
| 22 | At 180° the picture was sometimes upside down with red and blue swapped (orange looked blue); a restart could fix or cause it | `rpicam-vid --rotation 180` uses OV5647 sensor flips; on some starts the flip did not reach the sensor while libcamera still assumed the flipped Bayer order (kernel 6.18). The 90°/270° GStreamer pipeline also never ran: its unquoted `level=(string)4` is a bash syntax error | 180° flips in the ISP instead; quote the caps; a test runs `bash -n` on every pipeline. The cause in the driver is an **[assumption]** |
| 23 | Readers of `127.0.0.1:8888` got very different frame rates per 20 s run (0–19 fps against a steady 25 fps source), with many 0.2–0.4 s pauses | `stream_mux` removed a disconnecting reader's queue with `list.remove`, which compares deques **by value**: when a snapshot client left while another reader's queue was also empty, the *other* queue was removed, so that reader got no more data while its connection stayed open **[confirmed]** (unit test, and on the device a reader stalled with its handler thread still polling). The bursts came from `stdin.read(65536)`, which waits for a full 64 KB (≈0.13 s at 4 Mbit/s) **[confirmed]** | remove queues by identity; read with `read1`. Measured with 2 GStreamer readers, a WebRTC reader, a snapshot per second and 4 probes, 3 × 30 s each **[confirmed]**: before, a probe stalled in 2 of 3 runs, 105 gaps > 150 ms per probe (max 0.38 s), mux 29–33% of a core, RSS growing 20→29 MB; after, no stalls, no gap > 150 ms (max 0.09 s), mux 15–16%, RSS steady at 12 MB, total CPU 35–36% instead of 37–45%. The after-runs averaged 23.9 fps instead of 24.7–25.0. That is not the mux: an A/B with only the identity fix (still `read(65536)`) gave the same 23.9 fps under load and 24.4 fps with one reader **[confirmed]**, and the identity fix alone already halved the mux CPU (17%). The ISP source itself delivers 24–25 fps at 1080p. Follow-up (2026-10-11): each reader thread still woke every 5 ms to poll its queue; it now blocks on a `threading.Condition` that `broadcast` notifies. Same load, 3 × 30 s each, old mux re-measured as a control under identical conditions **[confirmed]**: mux 14.8–15.9% → 7.6–7.7% of a core, total CPU 33.3–38.2% → 32.4–34.4%, RSS 11–12 MB both, 23.6–24.0 → 24.1–24.2 fps per reader, frame gaps p50 41–42 ms and p95 49–52 ms both, max 74–87 ms → 73–97 ms, 5–16 → 4–17 gaps > 60 ms per reader, no gap > 150 ms and no stall in any run. A first baseline taken while one more external reader was attached gave mux 18.7–18.8% and total 41.9–42.7% |
| 24 | While testing hardware-decoded snapshots, the camera stream stopped completely and restarting `rpicam-source` did not help; the source process hung as a zombie in the driver | 4 parallel `gst-launch … ! v4l2h264dec ! …` snapshot pipelines, ended by `timeout` mid-stream, wedged the VideoCore: `bcm2835-codec` stop-streaming WARNING in `__vb2_queue_cancel`, `bcm2835_mmal_vchiq: timed out waiting for sync completion`, then `bcm2835-isp … Failed disabling o/p port, ret -62`, so the encoder and ISP of the source hung too (kernel 6.18.50, firmware 2026-09-14, `gpu=64M`) **[confirmed]**. Known upstream: tearing down `v4l2h264dec` with buffers outstanding, especially with several decoders at once, wedges the firmware ([raspberrypi/linux#7163](https://github.com/raspberrypi/linux/issues/7163), open; Chromium capped its concurrent decoders, [trixie-feedback#101](https://github.com/raspberrypi/trixie-feedback/issues/101)) | only a reboot recovered it. Snapshots stay on the software decoder (`openh264dec`): `capture_jpeg` ends its pipeline after the first frame and snapshots can overlap, which are exactly the two triggers. The hardware decoder would have cost 0.55 s instead of 1.5 s of CPU per snapshot **[confirmed]**; don't switch without serialising snapshots and keeping one long-lived decoder |

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
