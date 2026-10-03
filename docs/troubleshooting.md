# Troubleshooting

Start with the web console: **System → Diagnostics** downloads a redacted log of the current boot,
and the Overview chips show which subsystem is unhappy. If you have SSH access (off by default),
these units are the interesting ones:

```sh
journalctl -u pibuddycam -n 100 --no-pager               # main app: Prusa Connect, snapshots, signaling
journalctl -u rpicam-source -n 100 --no-pager            # camera source (owns the sensor)
journalctl -u pibuddycam-rtsp -u pibuddycam-ha-rtsp -n 100 --no-pager   # RTSP :8554 / :8555
journalctl -u pibuddycam-admin -n 100 --no-pager         # web console
journalctl -u pibuddycam-provisioning -n 100 --no-pager  # setup network and wizard
journalctl -u pibuddycam-updater -n 100 --no-pager       # update checks
```

Logs live in RAM. For problems that happen **before a reboot or during boot**, look at
`bootlog.txt` on the card's `BOOT` partition. It is readable on any computer.

## Setup

**No setup network appears.**
- The device might already be set up. To re-enter setup, use
  [recovery](user-guide.md#recovery-and-factory-reset).
- The data partition might be missing or corrupt. The device then stays in setup, so check
  `bootlog.txt`.

**The setup page doesn't open by itself.**
- Open `http://192.168.4.1` in a browser while connected to `PiBuddyCam-Setup-<id>`. Use `http`,
  not `https`: the setup network has no HTTPS.
- Some phones keep mobile data on and skip the login window; switch mobile data off for setup.
- Automatic opening needs an image that ships the setup network's DNS redirect (see the
  changelog); older images still serve the wizard at the address above.

**The camera check fails.**
- Reseat the ribbon cable (contacts facing the right way) and make sure exactly one camera is
  connected, then use the wizard's retry.
- A failed camera keeps the device in setup instead of boot-looping.

**Can't join my Wi-Fi.**
- Use 2.4 GHz only.
- Check the password and that the network hands out DHCP addresses and working DNS.
- Enter hidden networks manually.

**I changed the network and lost the camera.**
- The camera reverts to the old settings on its own if the new ones do not come up (about a
  minute). If that also fails it starts the setup hotspot `PiBuddyCam-Setup-<id>`; join it and
  re-run setup or use [recovery](user-guide.md#recovery-and-factory-reset).
- If the camera lost its Wi-Fi some other way (new router, changed password), the network
  watchdog starts the same setup hotspot after 10 minutes without a link; join it, open
  `https://192.168.4.1/admin` and fix the network under System, Network. The phone's sign-in
  window shows a short page pointing there; it cannot get past the certificate warning, so open
  the address in the normal browser. It retries the old network
  every 10 minutes and stops the hotspot when that works. To turn the watchdog off, add a systemd
  drop-in setting `PIBUDDYCAM_NETWORK_WATCHDOG_MINUTES=0` for `pibuddycam-network-watchdog.service`.
- After a successful change to a new static address, reconnect at the address the console showed.
  A wrong prefix length or gateway is the usual cause of an automatic revert.
- The Network card says *needs a newer camera image* on an image older than the release after
  1.4.0; reflash to use it.

**`http://<address>` used to show plain text.** It now redirects to `https://<address>/admin`.
If it still shows only `PiBuddyCam`, the request had no usable `Host` header; open the `https`
address directly.

**Wrong timestamps, or the console warns the clock is not synchronized.**
- The camera has no clock chip; time comes from NTP once Wi-Fi is up. Check that the network
  allows outbound NTP (UDP 123), or set your own time server under System, Network, *Time servers*.
- Until the first sync, frame and session names, logs and a freshly generated certificate can
  carry an old date. The last known time is kept across reboots, so this mostly affects the first
  boot.

**The token is rejected.**
- Get a fresh token from Prusa Connect. The wizard keeps your last working credentials.

**The camera never appears in Prusa Connect, and the console shows "Prusa Connect is not set up".**
- You chose *Later* on the token step. Paste the token under Integrations in the console. The
  camera makes no Prusa Connect requests until a token is saved.

## Prusa Connect

**The camera is under "Other cameras" and has no live view.** This is the current, known Connect
behaviour, not a local fault. Snapshots and control still work. See
[status](status.md#the-current-connect-limitation).

<a id="prusa-connect-rejects-the-camera"></a>
**Connect rejects the camera.** Symptoms: `/c/info` returns 403, snapshots return
"Invalid fingerprint", or signaling ACK is `1`/`3`.
- **Fingerprint mismatch.** A token is bound to the *first* fingerprint that uses it, and
  PiBuddyCam derives the fingerprint from the Wi-Fi MAC. If the fingerprint changed (another
  board, or a fingerprint edited in the console), create a **new token** in Connect and enter it.
- **Unreadable config.** If `token_len=0` shows up in the log, the device could not read its
  credentials. This happens when `/data/pibuddycam/config/*.toml` were edited as root. Restore the
  owner:
  ```sh
  sudo chown pibuddycam:pibuddycam /data/pibuddycam/config/{device,secrets}.toml
  ```

## Video

**The stream looks delayed by about a second.** That's the player's buffer, not the Pi. See
[integrations: RTSP players](integrations.md#rtsp-players).

**90°/270° rotation is slow or doesn't work.** Those angles use a slower rotation path and aren't
verified on all hardware yet. See [hardware](hardware.md#camera-and-rotation). Switch back to
0°/180° to confirm the camera itself works.

**Home Assistant doesn't find the camera.**
- Discovery needs multicast on the same network segment. Otherwise add the ONVIF integration
  manually with the device IP and port 80 (see [integrations](integrations.md#home-assistant-onvif)).

## Timelapse GPIO trigger

**Status says *permission denied*.** The image is older than the release that adds the `gpio`
group and udev rule. Reflash.

**Status says *no GPIO chip found* or *line is busy*.** Another program holds the pin, or the
device is not a Pi with the `pinctrl-bcm2835` controller. `gpioinfo` (package `gpiod`) shows the
holder.

**Armed but no frames.** Check the wiring (OUT0 to the layer pin, Hackerboard GND to a Pi ground
pin), that `M262 P0 B0` runs in the Start G-code, and that timelapse capture is enabled. A pulse
inside one second of the previous one, or while capture is disabled, is ignored on purpose.

**The frame shows the wrong layer.** The dwell in the layer-change G-code is shorter than the
pulse-to-frame time shown in the console. Increase it.

**No video after a print.** The recording pin must be released (the End G-code
`M264 P1 B0`) for the session to close and build. A session with no frames is closed without a
build. Builds run one at a time; a second one waits.

## MQTT

**MQTT won't connect.**
- Use *Test connection*. It checks DNS, TCP/TLS, authentication, publishing and subscribing
  separately.
- Check the broker URI, the CA file and the credentials.
- ONVIF and RTSP keep working during an MQTT outage.

## Updates

**An update is offered but won't install.**
- Installs need your approval and password.
- Failed signature, hash, compatibility or free-space checks reject the update *before* it's
  activated, and the console shows the reason.
- If the new version fails its health check, the device rolls back automatically.

**A new release needs a new image.** Changes to system services or packages only arrive by
reflashing; the release notes say when. Releases from 1.4.0 on refuse to run on older
(pre-rename) images and roll back. Reflash with a current image.

## Still stuck?

Open an [issue](https://github.com/bullitt186/pibuddycam/issues/new/choose). Attach the
downloaded diagnostics, which are already redacted. Read them through before posting anyway.
