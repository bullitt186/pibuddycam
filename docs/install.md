# Installation

Turn a Raspberry Pi Zero 2 W and a camera module into a PiBuddyCam: flash the image, then complete
the setup wizard. It takes about 15 minutes.

## What you need

| Item | Requirement |
|---|---|
| Board | **Raspberry Pi Zero 2 W**. The image is built and validated for this board only |
| Camera | One CSI camera module. The OV5647 (Pi Camera v1) is tested; other libcamera sensors are experimental |
| microSD | 8 GB minimum; **16 GB or more** recommended for timelapses |
| Power | A stable 5 V supply. Powering the Pi on and off with the printer is fine |
| Network | 2.4 GHz Wi-Fi (the Zero 2 W has no 5 GHz radio) |
| Prusa Connect | A printer in Prusa Connect and a **camera registration token** (Connect → printer → *Camera* → add a camera → *Token*) |

More detail on hardware and the disk layout is in [hardware](hardware.md).

## 1. Flash the image

Download `pibuddycam-pi-zero2w-<version>.img.xz` from the
[latest release](https://github.com/bullitt186/pibuddycam/releases/latest). Its `.sha256` and
`.minisig` files let you verify it (see [SECURITY.md](../SECURITY.md#verifying-releases)).

**Recommended:** Raspberry Pi Imager with the project's OS list. The list tells Imager the right
first-boot format for this image:

```sh
rpi-imager --repo https://github.com/bullitt186/pibuddycam/releases/latest/download/pibuddycam-os-list.json
```

Choose *PiBuddyCam* as the OS. Imager's customisation (Wi-Fi, hostname, SSH) is optional and not
needed: the setup wizard asks for your Wi-Fi itself and does not yet take over Imager's values.

**Alternative:** Imager's *Use custom* with the downloaded `.img.xz`. Imager can't infer the
first-boot format from a raw image, so prefer the OS list.

Insert the card, connect the camera ribbon, and power on. The first boot grows the data partition
to fill the card, which takes a minute.

## 2. Set up with the wizard

While unclaimed, the device opens a setup Wi-Fi network:

- **SSID:** `PiBuddyCam-Setup-<last 6 characters of the device ID>`
- **Setup page:** opens by itself after you join, like a hotel Wi-Fi login. If it doesn't, open
  `http://192.168.4.1` in a browser.

The setup network is open by design: a headless device has no way to give you a unique password
before setup. **Do the setup somewhere you trust.** The wizard is only ever served on this setup
network, never on your normal LAN.

The wizard walks through:

1. **Start**: checks storage and the camera, with *Check again* if the camera isn't detected.
   You can still finish setup without a camera.
2. **Wi-Fi**: pick your network from the scan list or type its name (for hidden networks), then
   the password. If the scan finds nothing while the setup network is running, type the name.
3. **Prusa Connect**: paste the camera registration token (Connect → printer → *Camera* → add a
   camera → *Token*). Choose **Later** if you don't have it at hand, for example because it is in
   the Prusa app on your phone and the setup network has no internet. See
   [adding the token later](#adding-the-prusa-token-later).
4. **Password**: the administrator password for the web console. Required.
5. **Options** (all optional, or choose **Later** to skip the page):
   - **MQTT**: broker URI and credentials, with *Test connection* before saving.
   - **Fingerprint** (under *Advanced*): only needed to take over an existing registration.
     Leave it empty and the MAC-derived fingerprint is used, the same way the genuine camera does
     it.
6. **Review**: everything at a glance with secrets hidden; *Change* jumps back to a step.
   *Save and start camera* stores everything, turns the setup network off, joins your Wi-Fi and
   starts the camera.

Your phone or laptop drops off the setup network at the end, so the last page shows where to go
next before that happens. If the camera can't join your Wi-Fi, the setup network comes back; join
it again and the wizard reopens with your earlier answers, so you only correct the Wi-Fi details.
Reloading the page or closing the login window mid-way also keeps what you entered.

A rejected token returns you to the wizard without overwriting working credentials.

### Adding the Prusa token later

Only Wi-Fi and the administrator password are required. If you chose **Later** on the Prusa
Connect step:

1. Finish the wizard. The camera joins your Wi-Fi and starts, but is not registered with Prusa
   Connect yet. It makes no Prusa Connect requests until it has a token. Local RTSP, ONVIF, MQTT
   and the console work as usual.
2. Reconnect your phone or laptop to your normal Wi-Fi, so it has internet again.
3. Open the console, sign in, and paste the token under **Integrations**. A banner on every page
   reminds you until a token is saved. Saving restarts the camera application, so the token
   applies right away. If the camera runs an older image, the console says a restart is needed.

## 3. Use it

- **Prusa Connect:** the camera appears on your printer within a minute and uploads a snapshot
  every 10 seconds.
- **Web console:** `https://pibuddycam-<device-id>.local/admin`. Sign in with the administrator
  password. The certificate is self-signed by the device, so expect a browser warning. See the
  [user guide](user-guide.md).
- **Home Assistant, RTSP, ONVIF, MQTT:** see [integrations](integrations.md).

The Wi-Fi network, IP address (DHCP or static), hostname and time servers can be changed later in
the console under System, Network, so the wizard does not need to be re-run.

Updates arrive over the air from this project's GitHub releases and are only installed when you
approve them. See [user guide: updates](user-guide.md#updates).

## Reinstalling

Flashing a card rewrites all partitions, **including your data**. First export what you want to
keep: timelapses through the `smb://<device>/sdcard` share, and your settings. Then flash and run
the wizard again. To re-enter setup *without* reflashing, use recovery mode (see
[user guide: recovery](user-guide.md#recovery-and-factory-reset)).
