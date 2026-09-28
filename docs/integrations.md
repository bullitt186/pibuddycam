# Integrations

How to use PiBuddyCam with Prusa Connect, Home Assistant, RTSP players and MQTT. All local
interfaces are meant for a **trusted LAN**. Never expose them to the Internet.

| Interface | Address | Notes |
|---|---|---|
| Prusa Connect | outbound only | Snapshots, camera info, control and WebRTC signaling |
| Prusa RTSP | `rtsp://<device>:8554/live` | On/off controlled by Prusa Connect and the console |
| Home Assistant RTSP | `rtsp://<device>:8555/live` | Always on, independent of the Prusa setting |
| JPEG snapshot | `http://<device>/snapshot.jpg` | |
| ONVIF device/media | `http://<device>/onvif/…` (TCP 80) | Unauthenticated |
| ONVIF discovery | UDP 3702, multicast `239.255.255.250` | |
| Samba share | `smb://<device>/sdcard` | Timelapse frames and videos (guest access) |
| MQTT | your broker | Optional; off until configured |

All video outputs share one hardware H.264 encoder. Watching RTSP or Home Assistant never
disturbs Prusa Connect snapshots or live view.

## Prusa Connect

After [setup](install.md) the device registers with your token:
- It uploads camera info and a snapshot every 10 seconds (configurable).
- It follows settings changes from Connect (name, quality, intervals, RTSP, WebRTC).
- It answers WebRTC live-view requests.

**Known limitation:** Connect currently lists PiBuddyCam under *Other cameras* and may hide its
live-view and settings controls. See [status](status.md#the-current-connect-limitation).
Snapshots and control keep working.

## Home Assistant (ONVIF)

1. In Home Assistant open **Settings → Devices & services**. The camera is discovered as an ONVIF
   device under its configured name.
2. Accept it and leave username and password empty.

Home Assistant receives the H.264 stream from `:8555/live`, the JPEG still from `/snapshot.jpg`,
and one ONVIF media profile whose resolution follows the camera's quality setting.

If multicast doesn't cross your VLANs, add the **ONVIF** integration manually with the device IP,
port `80` and no credentials. Home Assistant needs to reach UDP `3702` (discovery), TCP `80` and
TCP `8555`.

The facade implements the ONVIF calls Home Assistant uses. It is **not ONVIF certified**, and it
never exposes the Prusa token or fingerprint.

## RTSP players

```sh
vlc --network-caching=100 rtsp://<device>:8555/live
```

Server-side latency is about 70 ms once the stream is running. The first connection after the
camera has been idle takes about 2.5 s. VLC's default 1000 ms buffer is what makes the stream feel
slow; set `--network-caching` low as above.

## MQTT and Home Assistant discovery

MQTT is **off until you configure it**, in the setup wizard or in the web console under
*Integrations*.
- **Brokers:** `mqtt://` and `mqtts://` over IPv4, IPv6 or DNS, with optional username and
  password, and the system CA or your own CA file.
- **No automatic setup:** there is no broker auto-discovery, and credentials are never taken from
  Home Assistant.

Topics are derived from the persisted device ID (not from the name or IP):

```text
pibuddycam/<device-id>/availability     retained "online"; last will "offline"
pibuddycam/<device-id>/state            retained state JSON (no secrets)
pibuddycam/<device-id>/command/<name>   QoS 1, never retained
pibuddycam/<device-id>/update/state     retained update state (Home Assistant update schema)
pibuddycam/<device-id>/update/install   non-retained literal install command
```

The commands are `quality`, `snapshot_upload`, `snapshot_interval`, `timelapse_enabled`,
`timelapse_interval`, `timelapse_fps`, `timelapse_build`, `prusa_rtsp`, `webrtc` and `restart`.
Each command:
1. validates a bounded payload;
2. applies and saves the change through the same settings path as the console;
3. publishes the resulting state, even when rejected. A rejection sets `last_command_error` to a
   short reason.

**Discovery.** Home Assistant discovery is one retained device document at
`homeassistant/device/pibuddycam_<device-id>/config`, which creates a *<camera name> Controls*
device. The media camera itself comes from ONVIF, so no duplicate camera entity is created. When
Home Assistant publishes `online`, discovery and state are republished after a short random
delay, so entities survive broker and Home Assistant restarts.

Two entities exist for the [GPIO timelapse trigger](user-guide.md#timelapse-gpio-trigger-prusa-gpio-hackerboard)
and are disabled by default in Home Assistant: *Timelapse recording* (on while a print session
is recording) and *Timelapse pulse-to-frame time* (seconds from the last layer pulse to the stored
frame). Their state fields, `timelapse_recording` and `timelapse_trigger_latency_s`, are `false`
and `null` unless the GPIO trigger is armed.

Both prefixes (`homeassistant`, `pibuddycam`) are configurable. MQTT failures are isolated: they
never affect Prusa Connect, RTSP or ONVIF.

> **Upgrading from a pre-1.4.0 image:** the old `buddy3d` prefix and `buddy3d_*` discovery IDs are
> gone. Home Assistant shows a new device; delete the old one and its retained discovery topics.
