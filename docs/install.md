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

Choose *PiBuddyCam* as the OS. You can optionally set Wi-Fi, hostname and SSH in Imager's
customisation; the Wi-Fi values are pre-filled in the setup wizard.

**Alternative:** Imager's *Use custom* with the downloaded `.img.xz`. Imager can't infer the
first-boot format from a raw image, so prefer the OS list.

Insert the card, connect the camera ribbon, and power on. The first boot grows the data partition
to fill the card, which takes a minute.

## 2. Set up with the wizard

While unclaimed, the device opens a setup Wi-Fi network:

- **SSID:** `PiBuddyCam-Setup-<last 6 characters of the device ID>`
- **Setup page:** `http://192.168.4.1` (your phone or laptop usually opens it automatically)

The setup network is open by design: a headless device has no way to give you a unique password
before setup. **Do the setup somewhere you trust.** The wizard is only ever served on this setup
network, never on your normal LAN.

The wizard walks through:

1. **Status**: checks storage and the camera, with a retry if the camera isn't detected.
2. **Imager settings**: imports any Wi-Fi, hostname or SSH values given in Raspberry Pi Imager.
3. **Wi-Fi**: pick your network or enter a hidden SSID.
4. **Prusa token**: paste the camera registration token from Prusa Connect.
5. **Fingerprint** (optional): only needed to take over an existing registration. Otherwise
   leave it empty and the MAC-derived fingerprint is used, the same way the genuine camera does
   it.
6. **Administrator password**: protects the web console. Required.
7. **MQTT** (optional): broker details, tested live before saving.
8. **Summary**: a review with secrets hidden.
9. **Save and finish**: stores everything, turns the setup network off, joins your Wi-Fi and
   starts the camera.

A rejected token returns you to the wizard without overwriting working credentials.

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
