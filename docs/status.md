# Project status

What works today, what is blocked, and what is still unverified. It covers facts only; planned
work is in the [roadmap](roadmap.md). The history of how we got here is archived in
[`_archive/docs/status-history.md`](../_archive/docs/status-history.md).

Claims marked **[confirmed]** were observed live on a Pi Zero 2 W + OV5647 or traced in the
firmware. Anything else is stated as open or as an assumption.

## At a glance

| Capability | State |
|---|---|
| Registration with a Prusa Connect token, `/c/info`, snapshots every 10 s | ✅ Working **[confirmed]** |
| Socket.IO camera authentication (ACK `0`), control/configuration from Connect | ✅ Working **[confirmed]** |
| Video quality SD/HD/FHD switched from Connect | ✅ Working on the device **[confirmed]**. The exact per-payload "persist" flag is still open (GAP-QUALITY-02) |
| WebRTC live view in the Prusa app and browser | ⚠️ The implementation worked live (2026-09-19/20). It then stopped connecting because the browser rejected every camera ICE candidate (media id `video0` against Connect's `sdpMid` `"0"`). With the media id sent as `0` the stream connected from Chrome on the LAN (2026-10-03, one run, 1920×1080) **[confirmed]**. The *Other cameras* state described below is unchanged |
| Local RTSP (`:8554` Prusa-controlled, `:8555` always on), ONVIF, JPEG snapshot | ✅ Working **[confirmed]** |
| Home Assistant via ONVIF / MQTT discovery | ✅ Camera-side working **[confirmed]**. A long Home Assistant + Prusa soak has not been recorded |
| Timelapse capture and AVI build, Samba share | ✅ Working **[confirmed]**. Connect's `file_list` path isn't exercised by the app |
| Local web console (HTTPS, settings, monitor, local WebRTC, updates, diagnostics) | ✅ Working **[confirmed]**. The local monitor shows snapshots on the dev image `v1.4.0-35-gacfa0fb` (2026-10-11) after the admin service got a GStreamer cache (`XDG_CACHE_HOME`) |
| Setup wizard joins the home Wi-Fi from the hotspot | ✅ Working **[confirmed]** on the dev image `v1.4.0-35-gacfa0fb` (2026-10-11, one run). Earlier images restarted the hotspot over the new link because the camera target was slow to start |
| MQTT with plain `mqtt://` to a local broker | ✅ Working **[confirmed]** (2026-10-04, dev image `v1.4.0-33-g94bcd05`): availability, state, update state and the Home Assistant discovery document were retained on the broker. The console restarting the camera on save, the quicker snapshot capture and the fixed encoder bitrate (1.5/3/4 Mbit/s) are **not yet verified on hardware** |
| Signed OTA application updates with rollback | ✅ Working **[confirmed]** (install and deliberate rollback tested) |
| Image rotation 0°/180° | ✅ Zero-cost `rpicam-vid` flip |
| Image rotation 90°/270° | ❓ Implemented. **Not yet verified on hardware** (see [hardware](hardware.md#camera-and-rotation)) |

## The current Connect limitation

The device side of the protocol works, but Prusa Connect currently lists a PiBuddyCam under
**"Other cameras"** and shows no live-view or settings controls. **[confirmed 2026-09-25]**
- A read-only lookup `GET camera-service-api.prusa3d.com/v1/cameras/<token>` returns `404`, both
  for the current token and for one that worked before.
- A control-only viewer still authenticates (ACK `0`) and delivers configuration, so this is not
  a blanket signaling failure.
- Earlier WebRTC viewer probes were refused with ACK `5`.

What adds a token to that camera-service registry is unknown. There is no public registration
endpoint, and finding out needs a controlled comparison with a genuine camera or information from
Prusa. Don't guess or mutate backend state while developing. Keep these observations separate:
registry lookup, UI classification, viewer authentication, control relay and WebRTC admission.
The investigation so far is in the [archived history](../_archive/docs/status-history.md) and in
[dead ends](reverse-engineering/dead-ends.md).

## Protocol parity

Remaining differences from firmware 3.1.6 are tracked item by item in the
[gap tracker](reverse-engineering/gap-tracker.md). No gap is fully open. Every remaining item is
partial: implemented but not verified live, gated off because the server rejected the encoding,
or deferred until a descriptor or capture removes an ambiguity.

## Acceptance

The core runtime is accepted on **one** Pi Zero 2 W + OV5647, including a signed update and a
rollback. These are **not yet recorded**:
- the fresh-card and card-size matrix;
- every onboarding route (Imager prefill, hotspot, recovery sentinel);
- other camera sensors;
- a long Home Assistant + Prusa coexistence soak;
- power-loss and recovery matrices.

Treat releases as beta until these are recorded.
