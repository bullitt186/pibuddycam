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

**The camera check fails.**
- Reseat the ribbon cable (contacts facing the right way) and make sure exactly one camera is
  connected, then use the wizard's retry.
- A failed camera keeps the device in setup instead of boot-looping.

**Can't join my Wi-Fi.**
- Use 2.4 GHz only.
- Check the password and that the network hands out DHCP addresses and working DNS.
- Enter hidden networks manually.

**The token is rejected.**
- Get a fresh token from Prusa Connect. The wizard keeps your last working credentials.

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
