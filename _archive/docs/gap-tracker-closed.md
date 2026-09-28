# Closed firmware-parity gaps: full evidence (historical)

Evidence recorded while closing each gap. The one-line index lives in
[docs/reverse-engineering/gap-tracker.md](../../docs/reverse-engineering/gap-tracker.md).

### GAP-WEBRTC-01 — Consume Connect-provided ICE server configuration

- [x] **P0 · Closed 2026-09-20 (live-verified): Connect ICE config consumed; media plays live in app and browser**
- **Live-verified 2026-09-20:** the WebRTC stream plays live in the Prusa app and the browser (Pi log `Remote answer set` → `ICE connection state: 1/2/3`, page `<video>` 1920×1080 playing).
- **Recovered 2026-09-19 (live):** the `webrtc` ICE config is tag8 = `{1: <blob>}` with a repeated list of `{id, host, port, type}` (1=STUN/2=TURN/3=TURNS) plus a TURN block carrying a time-limited username and base64 credential (`FUN_000bc0ec`). `proto.decode_ice_config` parses it; `webrtc.create_offer` configures `webrtcbin`'s `stun-server` and `turn-server` (escaped credentials) with it. Verified live: 12 servers + `coturn.prusa3d.com:3478` + credentials applied.
- **Firmware behavior:** parses the incoming WebRTC ICE submessage, configures every supplied STUN
  and TURN server, including hostname, port, username, credential, and server type; falls back to
  its defaults only when no servers were supplied. **[confirmed]**
- **Current behavior (implemented):** the nested `tag8` ICE config is decoded and all supplied
  STUN/TURN servers and credentials are mapped into `webrtcbin`, with firmware-equivalent fallback
  servers. Media negotiation is live-verified (browser and app).
- **Before (superseded):** field 12 was not decoded as `ice_config`; GStreamer was always configured
  with only `stun://stun.l.google.com:19302`, so remote/cloud viewing would commonly fail when
  direct ICE was unavailable and TURN relay was required.
- **Implementation:** recover and implement the ICE submessage; map all supplied URLs and TURN
  credentials into `webrtcbin`; retain firmware-equivalent fallback servers.
- **Acceptance:** unit fixtures recover the complete ICE list; an integration test forces relay and
  establishes a `RELAYED` connection using Connect-supplied credentials.
- **Code:** [`proto.py`](../app/proto.py),
  [`webrtc.py`](../app/webrtc.py)

### GAP-WEBRTC-02 — Share the existing camera encoder instead of opening libcamera twice

- [x] **P0 · Closed 2026-09-20 (live-verified): WebRTC consumes the shared `stream_mux` source**
- **Live-verified 2026-09-20:** the WebRTC branch consumes the shared `stream_mux` output (SPS-patched, port 8889) and the stream plays — no separate encoder.
- **Recovered 2026-09-19:** `webrtc.create_offer` now reads the always-running mux stream (`tcpclientsrc 127.0.0.1:8888`) instead of spawning a second `rpicam-vid` (which died defunct — libcamera is single-consumer). The encoder also runs H264 `--profile baseline --level 3.1` to match the firmware's `H264CameraSource`.
- **Firmware behavior:** WebRTC, RTSP, and snapshots consume coordinated outputs from the existing
  hardware video pipeline. **[confirmed]**
- **Current behavior (implemented):** WebRTC reads the shared `stream_mux` output; the live-verified
  browser and app sessions ran without a second libcamera owner or camera-busy errors.
- **Before (superseded):** `rpicam-source.service` continuously owned the camera, while WebRTC
  started a second independent `rpicam-vid` process, which was likely to fail with a camera-busy
  error once an offer reached the Pi.
- **Implementation:** feed WebRTC from `stream_mux.py` or provide one shared capture/encode service
  with independent RTSP, JPEG, and WebRTC consumers.
- **Acceptance:** RTSP, periodic snapshots, and a WebRTC session can run without a second libcamera
  owner or camera-busy errors.
- **Code:** [`webrtc.py`](../app/webrtc.py),
  [`rpicam-source.service`](../app/systemd/rpicam-source.service)

### GAP-WEBRTC-03 — Implement session lifecycle and teardown

- [x] **P0 · Stream + teardown live-verified (app and browser) 2026-09-19**
- **Live reproduction 2026-09-19 (browser page-open):** a Connect-web cameras page triggered one
  offer (`Sent WebRTC offer … 755 chars`), the viewer answered (`Remote answer set`), and 21
  candidates arrived — **7 added, 14 dropped** as `WebRTC candidate message carried no candidate`.
  The stream never completed and `state.streaming` stayed `True`; two `GET /snapshot.jpg` 13 s
  apart were byte-identical and 40 s of `journalctl -f -u pibuddycam` had zero `Snapshot:` lines —
  **periodic snapshots were permanently paused**, exactly as the "Current behavior" note predicts.
- **Live fix (2026-09-19, `8bd7a45`):** `webrtcbin` has **no** `on-ice-connection-state-change` signal (gst-inspect confirms only `on-ice-candidate`/`on-negotiation-needed`); the first attempt raised in `create_offer` and left snapshots paused again. Fixed by connecting **`notify::ice-connection-state`** (a readable GObject property), arming the connect watchdog *before* signal wiring, wrapping each connect, and resuming snapshots if `create_offer` raises. **Verified live:** a browser "Play live stream" now creates and sends the offer (`m=video 9`, media accepted, 754 chars) with local candidates; when ICE never connected, the 30 s watchdog fired (`WebRTC stream ended (no-ice-connection)`) and snapshots resumed (`Resuming snapshots after WebRTC stream ended`; cadence gap 22:33:19→22:34:00 then every 10 s). Candidate extraction (`proto.find_webrtc_candidate`) also fixed (unit-tested).
- **Browser WebRTC now works (live-verified 2026-09-19):** after the candidate-extraction fix the viewer's trickle candidates are all applied, and the browser stream establishes — Pi log `Remote answer set` → `WebRTC inbound candidate added` → `ICE connection state: 1/2/3`; the page's `<video>` was **1920×1080, readyState 4, playing** (`currentTime` advancing). The candidate fix was the enabler: the earlier run dropped 14/21 candidates and ICE never completed. Teardown also verified: ending the viewer gave `ICE connection state: 4` → `WebRTC stream ended (ice-failed)` → `Resuming snapshots` and snapshots resumed within seconds.
- **`CameraIsNotSessionMemberError` fixed (2026-09-19, `ba48dc8`):** the server error was caused by the **unsolicited post-auth burst** (`send_sio_info` + `status` + `protobuf_version` + `features`) — the server answered `protobuf_version` with ACK `1` + `CameraIsNotSessionMemberError` and cycled the session, dropping the viewer's answer. Firmware `FUN_000a05e4` sends nothing post-auth (only the trigger dispatcher `FUN_000a9638` for tags 1/2/12 does), so the burst was removed. **Live-verified after deploy:** 0 `CameraIsNotSessionMemberError`, one stable connection (Auth ACK `0`, no disconnect), and a browser "Play live stream" relayed its answer immediately (`Remote answer set` → candidates added → `ICE connection state: 1/2/3`, video playing 1920×1080).
- **Recovered 2026-09-19 (live):** the full camera-side flow is implemented — ICE config → camera **offer** (type 3) → viewer **answer** (type 2) → **trickle candidates** (type 4). The viewer's candidates arrive as `a=candidate:...` with `tag2 = mid` and are applied via `add-ice-candidate`.
- **Final fix (live-verified):** the Connect answerer (a libdatachannel endpoint) **validates the H.264 SPS** and rejects our v4l2 SPS (`428029`, baseline level 4.1) with `m=video 0`; it wants the firmware's `42e01f` class (constrained baseline level 3.1). Transcoding was too heavy (`openh264enc` wedged the Pi; `v4l2h264enc` is owned by the camera source), so `stream_mux` now serves the same H264 on **port 8889** with each SPS NAL's profile/constraint/level patched to `42 e0 1f` (matched by NAL type — rpicam-vid emits header `0x27`), and the WebRTC branch reads 8889 (8888/RTSP/snapshots untouched). After the patch the answer is `m=video 9 …` (media **accepted**) and the stream plays. Requires `gstreamer1.0-nice`.
- **Firmware behavior:** tracks clients and connection state, enforces lifetime/scope, tears down
  disconnected/expired peers, and restores scoped video settings. **[confirmed]**
- **Current behavior (historical, fixed 2026-09-19):** the global `streaming` flag used to become true
  on the first offer and was never cleared. ICE/peer failure, close, disconnect and a watchdog now
  clear it and resume snapshots.
- **Connect impact (historical):** snapshots remained permanently paused after the first offer; now
  they resume on any stream end, and stale peer resources are torn down.
- **Implementation:** represent each session explicitly; handle ICE/DTLS/peer state transitions;
  enforce TTL; tear down on failure/disconnect/expiry; clear streaming state when the last client
  ends.
- **Acceptance:** connect/disconnect, failed negotiation, and TTL-expiry tests all clean up the
  pipeline and resume snapshots.
- **Code:** [`main.py`](../app/main.py),
  [`webrtc.py`](../app/webrtc.py)
- **Implementation (done, `1e1e2f2` + `8bd7a45`):** `webrtc.py` connects
  **`notify::ice-connection-state`** (webrtcbin has no `on-ice-connection-state-change` signal) and
  maps states through the stdlib-only
  [`webrtc_lifecycle.py`](../app/webrtc_lifecycle.py): FAILED/CLOSED notify
  immediately, DISCONNECTED is re-checked after a 15 s grace period, and a 30 s connect watchdog
  fires when ICE never connects. `main.py`'s `on_stream_ended` clears `state.streaming` and
  resumes periodic snapshots. Viewer trickle candidates are extracted by the host-testable
  [`proto.find_webrtc_candidate`](../app/proto.py), which also unwraps the
  UTF-8-collapsed tag4 `str` (the live 14-of-21 drop). Tests:
  `test_proto.py::FindWebRtcCandidateTests`, `test_webrtc_lifecycle.py`. The firmware's
  exact peer TTL worker remains untraced, so the watchdog is explicitly Pi-side policy.
  **Live-verified (app and browser, 2026-09-19):** connect, media, and teardown (ICE failed/closed/
  disconnected clears `state.streaming` and resumes snapshots). Only explicit TTL-expiry tests
  remain.

### GAP-STATUS-01 — Report actual dynamic camera state

- [x] **P1 · Closed 2026-09-20 (live-verified): shared state drives quality/name/interval/WebRTC/RTSP**
- **Live-verified 2026-09-20:** `status` is sent without errors and `/c/info` reflects the live `config.name`/`config.model`/quality; quality/interval changes are reflected.
- **Firmware behavior:** constructs `CameraInfoMessage` from current snapshot state, upload interval,
  IR mode, speaker volume, RTSP mode/status/URL, WebRTC mode/status, service state, current quality,
  network state, timezone, and system telemetry. **[confirmed]**
- **Current behavior (implemented):** `CameraState` drives status; quality, camera name,
  snapshot/timelapse intervals, WebRTC mode/status, and RTSP mode/running (queried from the
  systemd service) are read from shared runtime state rather than fixed defaults.
- **Connect impact (resolved):** UI state tracks actual camera behavior for the supported settings.
- **Before (superseded):** many values were fixed defaults — WebRTC always enabled/running, video
  quality always FHD, upload interval fixed at 10, and RTSP state not following the service.
- **Implementation:** introduce a single runtime state model shared by command handlers and status
  encoding; read the persisted quality at startup; query actual service states where necessary.
- **Acceptance:** after every supported command, a decoded `status` fixture shows the resulting
  firmware-equivalent state.
- **Code:** [`signaling.py`](../app/signaling.py)

### GAP-SNAPSHOT-01 — Apply snapshot upload interval changes

- [x] **P1 · Implemented and unit-tested (d1ec311); live cadence verified (20 s, GAP-CONFIG-01)**
- **Firmware behavior:** accepts `snapshot_interval` in seconds, validates the inclusive range
  `10–600`, stores milliseconds in configuration, and changes the active upload cadence.
  **[confirmed]**
- **Current behavior (implemented):** the interval lives in shared mutable state; a valid change
  wakes/reschedules the active loop and updates status. **Live-verified:** the Connect
  "Displayed Frame Update Interval" slider sent `{3:{5:20}}` and the snapshot cadence settled to
  exactly 20 s (GAP-CONFIG-01).
- **Connect impact (resolved):** the UI setting takes effect live.
- **Before (superseded):** the interval was read once at loop creation and later valid changes were
  only logged, so the UI setting had no effect.
- **Implementation:** keep interval in shared mutable state, persist it if desired, wake/reschedule
  the active loop, and update status.
- **Acceptance:** changing 10→60→10 seconds changes measured upload scheduling without restart;
  values outside the firmware range are rejected.
- **Code:** [`main.py`](../app/main.py),
  [`main.py`](../app/main.py)

### GAP-INFO-01 — Refresh and retry `/c/info`

- [x] **P1 · Closed 2026-09-20 (live-verified): periodic `/c/info` refresh**
- **Live-verified 2026-09-20:** repeated `200`s observed across boots.
- **Firmware behavior:** retries attribute upload and marks it dirty after relevant configuration or
  state changes. The recovered service loop retries on a countdown until successful. **[confirmed]**
- **Current behavior (implemented):** `info_service` runs the firmware-style dirty/countdown loop
  (`next_info_action`, reload to 10 on failure) and marks dirty on camera-name, quality,
  snapshot-interval, and RTSP/WebRTC mode changes, so `/c/info` is republished rather than sent once.
- **Connect impact (resolved):** a transient failure recovers without restart, and changed
  attributes are republished.
- **Before (superseded):** performed one `/c/info` upload during startup and never refreshed it, so a
  transient failure or later attribute change left stale metadata until restart.
- **Implementation:** add bounded retry/backoff and a dirty/update mechanism invoked by relevant
  changes and reconnect/network events.
- **Acceptance:** injected HTTP failures recover without restart; changing a published attribute
  results in a subsequent successful `/c/info` containing the new value.
- **Code:** [`main.py`](../app/main.py),
  [`upload.py`](../app/upload.py)
- **Implementation:** pure decisions in
  [`info_service.py`](../app/info_service.py) reproduce the firmware
  dirty/countdown loop (`next_info_action`, reload to 10 on failure); `main.info_service_loop`
  ticks every second and marks dirty on camera-name, quality, snapshot-interval, and RTSP/WebRTC
  mode changes. Retry is bounded by `http_result.MAX_INFO_RETRIES` (the task contract's finite
  bound; firmware itself retries indefinitely). Tests: `test_info_service.py`
  (`NextInfoActionTests`, `DirtyAfterResultTests`, `ServiceLoopRecoveryTests`).

### GAP-CAP-01 — Stop overpromising unsupported features, or implement their wire behavior

- [x] **P1 · Resolved 2026-09-19: prune the hardware-absent features**
- **Decision:** stop advertising features the Pi cannot honor. `/c/info` now advertises only what is implemented: `SocketCom, UploadInterval, TimelapseEn/Interval/VideoMake/FileList, VideoStream, RtspStream, GetSnapshot, WiFi, FwVer, HwVer, CameraName, MicroSd, FwUpdate, CameraReboot, McuTemp, VideoQuality, WebRtc, TurnVideoQualityChange, trigger_scheme`. Removed `IrMode`, `SpeakerVolume`, `FanControl` (no such hardware on the Pi — Connect was showing the IR sun/moon/auto control that could never work). **`MicroSd` is kept** — the Pi backs it with the emulated SD at `/mnt/sdcard`, so Connect's timelapse UI works (GAP-TIMELAPSE-01). `configuration.light_control` still returns a truthful unavailable result if it ever arrives.
- **Firmware behavior:** advertises features it implements: `SocketCom`, `UploadInterval`,
  `TimelapseEn`, `TimelapseInterval`, `TimelapseVideoMake`, `TimelapseFileList`, `VideoStream`,
  `RtspStream`, `GetSnapshot`, `IrMode`, `SpeakerVolume`, `WiFi`, `FwVer`, `HwVer`, `CameraName`,
  `MicroSd`, `FwUpdate`, `CameraReboot`, `McuTemp`, `VideoQuality`, `WebRtc`,
  `TurnVideoQualityChange`, `trigger_scheme`, and `FanControl`; motor features are conditional.
  **[confirmed]**
- **Current behavior:** advertises the same motorless list but does not implement many corresponding
  commands or result messages.
- **Connect impact:** Connect exposes controls that silently fail or receive malformed/no responses.
- **Implementation choice:** either implement each advertised contract or determine, with live
  testing, which capabilities may be removed without losing Buddy classification/WebRTC enrollment.
  Do not claim a feature merely to resemble the string list if its behavior is absent.
- **Acceptance:** every advertised feature has a passing command/status integration test; all
  intentionally unsupported features are absent and the resulting feature hash is updated.
- **Code:** [`features.py`](../app/features.py)

### GAP-QUALITY-01 — Correct the raw quality-byte mapping

- [x] **P2 · Closed 2026-09-20 (live-verified): raw quality mapping `{5:1,6:2,7:3}` correct**
- **Live-verified 2026-09-20:** the app set HD and the encoder actually ran `--width 1280 --height 720`.
- **Firmware parameter:** raw event/internal values are `5=SD`, `6=HD`, `7=FHD`; protobuf enums are
  `1=SD`, `2=HD`, `3=FHD`; dimensions are `640×480`, `1280×720`, `1920×1080`. **[confirmed directly
  from `FW-QUALITY-PB`, `FW-QUALITY-DIRECT`, and `FW-QUALITY-DIMS`]**
- **Current parameter (implemented):** `state.py` `RAW_TO_ENUM = {5: 1, 6: 2, 7: 3}` and the
  dimension table is correct, so raw bytes 5/6/7 select SD/HD/FHD.
- **Connect impact (resolved):** raw-byte commands select the intended tier.
- **Before (superseded):** the direct event handler mapped `5→HD`, `6→FHD`, `7→SD`, so every
  `change_video_size`/`save_video_size` raw-byte command selected the wrong tier.
- **Implementation:** use raw-to-protobuf mapping `{5: 1, 6: 2, 7: 3}` and preserve the existing
  protobuf-enum-to-dimensions table.
- **Acceptance:** raw bytes 5/6/7 yield SD/HD/FHD respectively and status reports enums 1/2/3.
- **Code:** [`quality.py`](../app/quality.py)

### GAP-QUALITY-03 — Initialize and publish persisted quality

- [x] **P2 · Closed 2026-09-20 (live-verified): persisted quality restored after a real reboot**
- **Live-verified 2026-09-20:** `state.json {"quality_tier":2}` → `quality.env 1280x720` → encoder 1280x720 after a real reboot (GAP-PERSIST-01).
- **Firmware behavior:** starts from its stored quality and reports the translated current enum.
  **[confirmed]**
- **Current behavior (implemented):** the persisted tier is loaded once into shared state and used
  for the source, WebRTC, `/c/info`, and status (live-verified after reboot: `state.json`
  `{"quality_tier": 2}` → `quality.env` `1280x720` → encoder starts 1280×720; GAP-PERSIST-01).
- **Connect impact (resolved):** state and actual stream resolution agree after restart.
- **Before (superseded):** module state started as FHD and status always encoded FHD even if
  `quality.env` contained HD or SD, so state and stream resolution could disagree after a restart.
- **Implementation:** load `quality.read_current()` once into shared state and use it for source,
  WebRTC, `/c/info`, and status.
- **Acceptance:** starting with each persisted tier produces matching encoder dimensions,
  `/c/info`, and status.

### GAP-RTSP-01 — Align RTSP port and advertised URL

- [x] **P2 · Closed 2026-09-20 (Wave 2): firmware default confirmed 554; 8554 is an intentional Pi exception**
- **RE 2026-09-20 (Wave 2, direct 3.1.6 decompiler/ELF) [confirmed]:** the shipped firmware RTSP
  default is **554** — `FUN_000b04d4` logs `"RTSP server started on port %d"` with the literal
  `0x22a` = 554.
- **Firmware parameter:** loads the RTSP mode/port through configuration getters, starts the service
  when mode equals `2`, and advertises an RTSP URL assembled from runtime state. **[confirmed]**
- **Current parameter (intentional exception):** the Pi listens on `8554` (privileged-port
  avoidance) and advertises `rtsp://<ip>:8554/live`. **Live-verified:** the local stream works and
  Connect consumes the advertised URL.
- **Connect impact (resolved):** Connect uses the explicitly advertised URL, so the Pi's `8554`
  endpoint works despite the OEM default.
- **Decision:** retain `8554` as a documented intentional Pi exception rather than requiring
  privileged port `554`; do not change the listener/status URL.
- **Acceptance (met):** listener, status, and documentation agree on `8554`, and a real
  Connect/local-client test consumes it.
- **Code:** [`rtsp_server.py`](../app/rtsp_server.py),
  [`signaling.py`](../app/signaling.py)

### GAP-HTTP-01 — Snapshot `Expect: 100-continue`

- [x] **P2 · Closed 2026-09-20 (live-verified): HTTP handshake succeeds live**
- **Live-verified 2026-09-20:** `PUT /c/info` → `200` and `PUT /c/snapshot` → `200` live.
- **Firmware parameter:** sends `Expect: 100-continue` for JPEG snapshot uploads. **[confirmed]**
- **Current parameter (implemented):** the snapshot PUT sets aiohttp `expect100=True`, so the
  `Expect: 100-continue` header is sent.
- **Connect impact (resolved):** the handshake matches firmware; `PUT /c/info` and `PUT /c/snapshot`
  returned `200` live (2026-09-20).
- **Before (superseded):** sent the body immediately without the header.
- **Implementation:** enable aiohttp's `expect100` behavior for snapshot PUT and verify no latency or
  proxy regression.
- **Acceptance:** capture shows the header and successful `100`/final response flow.
- **Code:** [`upload.py`](../app/upload.py)

### GAP-HTTP-02 — Handle HTTP result classes and throttling

- [x] **P2 · Closed 2026-09-20 (live-verified): identity headers accepted live**
- **Live-verified 2026-09-20:** `PUT /c/info` → `200` and `PUT /c/snapshot` → `200` with the recovered `Token`/`Fingerprint` headers.
- **Firmware behavior:** distinguishes successful, blocked/throttled, redirected, and failed upload
  paths and changes service/retry behavior accordingly. **[confirmed]**
- **Current behavior (implemented):** `http_result` classifies responses into
  `success`/`redirect`/`blocked`(403)/`client_error`/`server_error`/`timeout`/`connection_error`
  and bounds transient retries; one session is reused for the app lifetime; redirects are
  classified but not auto-followed (firmware shows none).
- **Connect impact (resolved):** blocked/failed uploads take the firmware-equivalent path instead of
  being retried blindly.
- **Before (superseded):** returned/logged only the status code for snapshots, used a new session and
  fixed cadence regardless of result, and had no clear handling of server blocking.
- **Implementation:** reuse an HTTP session, classify responses, follow only firmware-equivalent safe
  redirects, and implement bounded retry/backoff/throttle behavior.
- **Acceptance:** mocked 2xx, 3xx, 4xx-blocked, 5xx, timeout, and TLS failures take the documented
  path without leaking token/fingerprint.
- **Implementation:** [`http_result.py`](../app/http_result.py)
  classifies `success`/`redirect`/`blocked`/`client_error`/`server_error`/`timeout`/
  `connection_error` and bounds transient retries. Direct evidence resolves the blocked class to
  exactly `403`: snapshot handler `FUN_0005c568` compares the response text to `"200"`, `"204"`,
  `"403"` and logs `Upload image BLOCKED by server!` (lp_app.strings:8304); `/c/info`
  `FUN_00062d74` accepts only `"200"`. Redirects are classified but not auto-followed because
  firmware shows no redirect handling. Tests: `test_http_result.py`. Log paths redact
  token/fingerprint via `main.redact_secrets`.

### GAP-INFO-02 — Keep `/c/info` dynamic values consistent

- [x] **P2 · Closed 2026-09-20 (live-verified): dynamic `/c/info` values**
- **Live-verified 2026-09-20:** `200` with `origin='OTHER'`, `registered=True`, and live features/config.
- **Firmware behavior:** publishes the current configured name, resolution, network values, model,
  firmware, manufacturer, trigger scheme, options, capabilities, and feature list. **[confirmed]**
- **Current behavior (implemented):** `info_body` builds the JSON body from `CameraState`
  (`state.resolution()`/`state.camera_name`), so `/c/info`, status, and the encoder share one state;
  the dirty loop republishes after changes.
- **Connect impact (resolved):** metadata shown in Connect tracks the shared runtime state.
- **Before (superseded):** name and dimensions came from startup config while live quality had
  separate persisted state; later name/quality changes did not update the document.
- **Implementation:** build `/c/info` from the shared runtime/config state used by status and command
  handlers.
- **Acceptance:** one state fixture produces mutually consistent `/c/info`, status, and encoder
  settings.
- **Implementation:** [`info_body.py`](../app/info_body.py)
  builds the JSON body from `CameraState` (`state.resolution()`/`state.camera_name`), and
  `upload.upload_info(session, state, ...)` no longer takes independent width/height/name.
  Tests: `test_info_body.py` (body/status name and resolution consistency).

### GAP-CONTROL-01 — Apply and publish camera-name changes

- [x] **P2 · Closed 2026-09-20 (live-verified): camera-name durability**
- **Live-verified 2026-09-20:** `camera_name` persists via `state.json` (GAP-PERSIST-01) and is re-applied at boot.
- **Firmware behavior:** stores the new camera name and includes it in subsequent status and
  `/c/info`. **[confirmed]**
- **Current behavior (implemented):** `state.set_camera_name` updates shared state, status and
  `/c/info` are republished via the dirty flag, and the name is persisted in `state.json`
  (GAP-PERSIST-01), so it survives reboot.
- **Connect impact (resolved):** rename control has a durable, visible effect.
- **Before (superseded):** logged the value only; all outbound metadata stayed `Buddy3D Camera`.
- **Implementation:** store the configured name in shared state, update status, mark `/c/info` dirty,
  and define safe persistence under the appliance's direct read-only ROOT + durable `/data` model.
- **Acceptance:** rename is reflected in both outbound surfaces and survives the intended reboot
  policy.

### GAP-OTA-01 — Implement truthful OTA behavior

- [x] **P2 · Closed 2026-09-20: resolved by owner decision (truthful decline), not live-tested**
- **Resolution:** resolved by owner decision (truthful decline), not live-tested.
- **Implementation:** `ota.py` (stdlib-only) parses the check-in (`file`/`last_version`/`sha1sum`/`force_upgrade`), compares dotted versions and classifies `up_to_date`/`update_available`/`forced_update`/`invalid`, and verifies integrity by SHA-1. `main.py` runs a periodic `ota_loop` (6 h), classifies and logs, and both `start_fw_update` (configuration) and the `fw_update` trigger return an explicit unsupported result (`decline_firmware_update`, reason `pi_impersonator_does_not_flash_firmware`) — never a silent no-op and never a destructive flash. Tests: `tests/app/test_ota.py` (no update, available, forced, older, malformed, integrity match/mismatch, policy).
- **Acceptance:** staged fixtures cover no update / available / integrity failure / remote-start decline; no unsafe installation path exists.
- **Still open:** a genuine release download+verify+install is intentionally not implemented (owner default); progress `client_trigger` messages remain under `GAP-SIO-01`.
- **Firmware behavior:** periodically queries the OTA endpoint, compares release/version metadata,
  downloads and verifies an update, observes update policy/time windows, installs/reboots, and
  reports progress through `client_trigger`. **[confirmed]**
- **Current behavior (implemented):** a periodic (6 h) `ota_loop` parses and classifies the
  check-in and verifies SHA-1, and both `start_fw_update` and the `fw_update` trigger return an
  explicit unsupported result (`decline_firmware_update`); `FwUpdate` remains advertised.
- **Connect impact (resolved):** Connect receives an explicit decline rather than a silent no-op; no
  unsafe installation path exists. Progress `client_trigger` remains open (GAP-SIO-01).
- **Before (superseded):** made one GET at startup, logged up to 200 response characters, never
  updated, and still advertised `FwUpdate`.
- **Implementation choice:** implement a safe Pi-software update mechanism and OEM-shaped progress
  responses, or remove `FwUpdate` and return an explicit unsupported result if the protocol permits.
- **Acceptance:** staged fixtures cover no update, available update, integrity failure, successful
  update, and remote-start request without unsafe arbitrary firmware installation.
- **Code:** [`main.py`](../app/main.py)

### GAP-PERSIST-01 — Persist settings + timelapse storage on /data

- [x] **P2 · Live-verified 2026-09-20: settings + timelapse store survive a reboot**
- **Historical problem (legacy deployment):** `/etc/pibuddycam/*` (quality/rtsp/identity) and
  `/mnt/sdcard` (timelapse frames, `.avi`, `.timelapse_videos.csv`) lived in volatile storage and
  were discarded on reboot. The appliance now uses direct read-only ext4 ROOT, explicit tmpfs for
  volatile state, and `/data` for durability; `overlayroot` did not activate on accepted hardware.
  `CameraState` settings (quality tier, camera name, snapshot/timelapse intervals and enables,
  RTSP/WebRTC modes) were memory-only.
- **Design:** a new 4 GB ext4 partition (`mmcblk0p3`, label `PERSIST`, PARTUUID `46f0d7c3-03`)
  mounted at `/data`; `/data/pibuddycam/state.json` holds the runtime settings and `/data/sdcard`
  is bind-mounted onto `/mnt/sdcard` (SMB keeps sharing `/mnt/sdcard` unchanged).
  `quality.live.env` stays ephemeral (GAP-QUALITY-02).
- **No-op rule (safe pre-deploy):** `settings_store.available()` is true only when `/data` exists
  and `os.path.ismount('/data')`; otherwise `save()` returns False without writing, `load()`
  returns `{}` for the missing file, `main._save_persisted_state` logs at debug, and
  `persist_restore.main` logs and exits 0. Nothing is created under a missing `/data`.
  `pi-persist.service` also carries `ConditionPathIsMountPoint=/data`, so before the partition
  exists the unit is *skipped* rather than reported failed.
- **Settings schema:** `version`, `quality_tier` (1/2/3), `camera_name`, `snapshot_interval`
  (10..600), `snapshot_upload_enabled`, `timelapse_interval` (1..3600), `timelapse_enabled`,
  `timelapse_fps` (1..30), `rtsp_mode` (1/2), `webrtc_mode` (0/1). `save` is atomic (same-dir
  temp + `os.replace`) and corrupt files are quarantined to `<path>.bad`.
- **Wiring:** `main.py` loads `state.json` at startup and `state.apply_persisted` applies only
  present/valid keys (reusing the setters where they exist); every successful mutation in
  `handle_event` (camera name, snapshot/timelapse intervals and enables, quality persist path,
  RTSP/WebRTC modes) calls `_save_persisted_state`.
- **Restore:** `persist_restore.py` (root, `pi-persist.service`, `RequiresMountsFor=/data`,
  `Before=` the three camera units) ensures `/data/{sdcard,pibuddycam}`, chowns to `SERVICE_USER`,
  bind-mounts the store, re-materializes `quality.env`/`rtsp.mode` from `state.json`, and prunes
  the oldest `/data/sdcard/timelapse/*.jpg` frames when `/data` free space is below 300 MB
  (`.avi`/CSV never deleted). `deploy.sh` installs+enables the unit and activates the fstab entry
  only when `/dev/mmcblk0p3` exists (derives the real PARTUUID, skips when already present).
- **Tests:** `tests/app/test_settings_store.py` (round-trip, version, missing/corrupt→`.bad`,
  unavailable no-op, atomic-on-replace-failure), `tests/app/test_state.py::PersistedStateTests`
  (contents + valid/invalid apply), `tests/app/test_timelapse.py::MainTimelapseWiringTests`
  (AST: `_save_persisted_state` is the injected persist callback; `handle_event` routes
  ≥6 mutations through `settings_coordinator`; startup loads and restores via the coordinator),
  `tests/app/test_persist_restore.py` (`frames_to_prune` oldest-first,
  `quality_env_values`, import safety).
- **Live verification 2026-09-20:** the SD was repartitioned offline (p2 → 10.3G, `mmcblk0p3` 4G
  ext4 LABEL `PERSIST` PARTUUID `46f0d7c3-03`), then a deploy added
  `PARTUUID=46f0d7c3-03 /data ext4 defaults,noatime 0 2`. Verified after a real reboot:
  `findmnt /data` = `/dev/mmcblk0p3`; `/mnt/sdcard` = `/dev/mmcblk0p3[/sdcard]` (bind);
  `pi-persist.service` `Result=success`; `state.json` (`{"quality_tier": 2}`) restored → `quality.env`
  = `1280x720` → `rpicam-vid … --width 1280 --height 720` (the encoder really starts at the
  persisted resolution), and the app logged `Loaded persisted settings: quality_tier`; frames +
  `.avi` + `.timelapse_videos.csv` written under `/mnt/sdcard/timelapse` survived the reboot and
  `file_list_entries()` still returned the `.avi`; `smbd` active with `[sdcard]` `force user =
  <operator>`; `quality.live.env` was absent after the reboot (still ephemeral, GAP-QUALITY-02).
- **Remaining:** none for the persistence scope. Optional follow-ups: an in-app live UI check of
  the save wiring (only the AST tests cover the mutation call sites today), and a `dosfsck`/fsck
  note for the new partition (ext4, journaled, fsck order 2 in fstab).

### GAP-DEVICE-01 — Reboot command behavior

- [x] **P2 · Closed 2026-09-20: live-verified — Connect's "Restart Camera" rebooted the Pi**
- **Live 2026-09-20:** clicking Connect's **"Restart Camera"** control (with its confirm dialog) sent the
  reboot trigger; the Pi rebooted immediately (SSH dropped, uptime reset to ~1 min), came back, all
  services `active`, `/c/info` returned `200` (`origin='OTHER', registered=True`), and
  `/boot/firmware/bootlog.txt` recorded the boot. The 60 s rate limit and the refusal paths remain
  covered by `test_device_control.py` (`RebootGuardTests`).
- **Firmware behavior:** remote reboot trigger reboots the device and reports the appropriate result
  before disconnect. **[confirmed]**
- **Current behavior:** advertises `CameraReboot` but does not dispatch the trigger.
- **Connect impact:** the Connect reboot control silently fails.
- **Implementation choice:** either authorize a narrowly scoped systemd reboot path with rate
  limiting and acknowledgment, or remove the advertised capability. Owner chose implement.
- **Acceptance:** command is authenticated, rate-limited, acknowledged, and invokes only the intended
  reboot action in an integration harness.
- **Implementation:** [`device_control.py`](../app/device_control.py)
  holds a pure predicate `can_reboot(last, now, min_interval)` and `request_reboot(state, reboot_fn,
  now=None)`, which records the accepted monotonic time on the shared `CameraState`
  (`state.last_reboot_monotonic`) before invoking the injected `reboot_fn`. The minimum spacing is
  `DEFAULT_REBOOT_MIN_INTERVAL_SECONDS = 60` (long enough for the Pi to drop the Socket.IO
  connection and come back). `main.py`'s dispatcher sends `trigger.REBOOT` through
  `request_reboot` with a narrowly scoped `reboot_device()` that runs only
  `['sudo','systemctl','reboot']`; the outcome is logged and success is never faked. A second
  request inside the window, a `False` return, and a raised exception all return `False` without
  rebooting. Tests: `test_device_control.py` (`RebootGuardTests`, `MainWiringTests`).
  **Remaining:** the live
  reboot is obviously unverified, and the firmware-style per-action `client_trigger` result code
  (GAP-SIO-01) is still not sent.

### GAP-DEVICE-02 — IR, speaker, fan and MicroSD feature truthfulness

- [x] **P2 · Closed 2026-09-20 (Wave 2): truthful `MicroSd` lives in `extended_status.4`; `camera_status` fields are pruned/moot**
- **RE 2026-09-20 (Wave 2, direct 3.1.6 decompiler/ELF) [confirmed]:** the truthful `MicroSd`
  status is in **`extended_status.4`** (already emitted correctly by `timelapse.storage_status`),
  **not** `camera_status`. `camera_status` fields 1/3 are `ir_mode`/`speaker_volume`, whose
  features are pruned (GAP-CAP-01); fields 4-6 are unresolved but moot. **No action needed.**
- **RE 2026-09-20 (Wave 1, direct 3.1.6 decompiler/ELF) [confirmed]:** the `camera_status`
  descriptor is **`0x3f6cd0`** (6 fields): 1 uvarint @0x00, 2 submsg @0x08, 3 uvarint @0x10,
  4 uvarint @0x14, 5 uvarint @0x18, 6 uvarint @0x1c. The **tag -> hardware binding**
  (IR/speaker/fan/MicroSD) still needs the sender `FUN_000a1394` and remains **unresolved**;
  keep the hardcoded IR/speaker bytes pinned rather than guessed.
- **Firmware behavior:** applies IR/day-night mode, speaker volume, fan control, and MicroSD status
  where hardware supports them. **[confirmed]**
- **Current behavior:** advertises these capabilities; IR is logged and ignored, and the others have
  no control path.
- **Connect impact:** controls can be displayed but never work; status values may imply nonexistent
  hardware.
- **Implementation:** remove unsupported hardware capabilities unless Buddy classification requires
  them; otherwise return truthful unavailable state rather than fake successful application.
- **Acceptance:** Connect UI and status expose only supportable operations, with no silent success.
- **Implementation:** `CameraState` now carries explicit
  `ir_available`/`speaker_available`/`fan_available`/`microsd_available = False` and
  `ir_mode = None`. [`device_control.py`](../app/device_control.py)
  `apply_light_control` maps the recovered `configuration.light_control` values
  (`auto`/`day`/`night` -> 1/2/3, `FW-CONFIG:193-228`), logs that the Pi has no IR illuminator,
  returns `False`, and leaves `ir_mode` unavailable instead of claiming the mode was applied;
  `main.py`'s configuration handler routes `light_control` through it. Tests:
  `test_device_control.py` (`HardwareAvailabilityTests`). **Closed (Wave 2):** the truthful
  `MicroSd` status is the already-correct `extended_status.4` block, and the remaining
  `camera_status` fields are either pruned (`ir_mode`/`speaker_volume`) or moot, so the hardcoded
  IR/speaker bytes in [`status.py`](../app/status.py) are left pinned harmlessly
  (`test_status_hardware_bytes_unchanged_pending_descriptor`). **Correction:** capability
  removal under GAP-CAP-01 **is** done — `IrMode`/`SpeakerVolume`/`FanControl` were pruned from the
  advertised features (while `MicroSd` is kept for the emulated SD), so Connect no longer exposes
  those controls even though the status bytes remain pinned.

### GAP-WEBRTC-07 — Decide audio-track compatibility

- [x] **P2 · Closed 2026-09-20 (Wave 2): Connect accepts a video-only offer; audio is optional**
- **Resolution [confirmed]:** the Pi offers video-only and the stream **plays live in both the app
  and the browser**, so Connect accepts a video-only offer — audio is optional/not required. Document
  video-only as intentional; no audio path is needed.
- **Firmware behavior:** contains WebRTC audio support including AAC-HBR/MPEG4-GENERIC at 48 kHz
  stereo, with G.726, PCMA, and PCMU paths also present. The recovered primary video profile remains
  H.264 constrained baseline. **[confirmed in firmware; not required by current Connect]**
- **Current behavior:** publishes a video-only WebRTC pipeline and cannot accept or answer an audio
  media section with a microphone track. This is intentional.
- **Connect impact (resolved):** none — Connect negotiates video-only and the stream plays.
- **Implementation choice (decided):** keep video-only; document it as intentional.
- **Acceptance (met):** a real Connect offer negotiates successfully with a video-only media
  section and plays in the app and browser.
- **Code:** [`webrtc.py`](../app/webrtc.py)

### GAP-IDENTITY-01 — Firmware fallback when `wlan0` MAC retrieval fails

- [x] **P3 · Implemented and live-verified (config precedence)**
- **Firmware behavior:** formats `wlan0` MAC as uppercase colon-separated text and hashes it with MD5;
  if MAC retrieval fails, it generates a random ten-character seed and hashes that. **[confirmed]**
- **Current behavior:** exact normal MAC path is implemented, but a missing/invalid `wlan0` MAC raises
  and prevents startup.
- **Connect impact:** no effect on the target Pi while `wlan0` exists; affects recovery or alternate
  hardware. A newly generated fallback also requires a newly paired token or stable persistence.
- **Implementation:** decide whether to reproduce and persist a fallback identity or fail closed with
  a clear diagnostic; never silently rotate a fingerprint bound to an existing token.
- **Acceptance:** normal-path vectors remain exact; failure behavior is deterministic and documented.
- **Code:** [`identity.py`](../app/identity.py),
  [`main.py`](../app/main.py)
- **Implementation:** [`identity.py`](../app/identity.py) adds
  `fingerprint_from_seed` (lowercase MD5 of the exact seed text), `generate_fallback_seed`, and
  `load_or_create_fallback_seed`, which persists the seed at `/etc/pibuddycam/identity.fallback`
  (`PIBUDDYCAM_IDENTITY_FALLBACK` override) and reuses a valid existing seed verbatim so a bound token's
  fingerprint is never silently rotated. `main.get_network_info` now resolves the fingerprint via
  `identity.resolve_fingerprint`: an explicit `config.ini` `[identity] fingerprint` wins (the value
  the registered token is bound to; live-verified 2026-09-18), then the MAC derivation, then this
  persisted seed. It reports an empty MAC when none is read. **Assumption:** the exact firmware alphabet
  of `FUN_000997f8(..., 10, 1)` is unrecovered; alphanumeric is used. This note describes the
  legacy `config.ini` developer path; appliance identity is stored in durable TOML under `/data`.
  Tests: `test_identity.py` (`FallbackSeedTests`).

### GAP-IDENTITY-02 — Deploy exact fingerprint only with a fresh token

- [x] **P3 · Resolved 2026-09-20: already using the `wlan0`-MAC-derived fingerprint (token bound; live ACK 0)**
- **Live state 2026-09-20 (verified):** the deployed `config.ini` has **no `[identity] fingerprint`**
  line and there is **no fallback-seed file**, so `identity.resolve_fingerprint` derives from `wlan0`
  (`<wlan0-mac>`) → `md5("<WLAN0-MAC>")` = `<md5(WLAN0-MAC)>`. Auth
  succeeds (`camera_authentication` ACK `0`) and `/c/info` is `200` — i.e. **the token is already
  bound to the MAC-derived fingerprint**, so the firmware-equivalent identity is in place and **no
  migration is needed**. The runbook below applies only if the fingerprint is ever changed.
- **RE 2026-09-20 (Wave 1, direct 3.1.6 decompiler/ELF) [confirmed]:** `FUN_00096cd8` is
  `generateFingerPrint`; it reads **`wlan0`** via `SIOCGIFHWADDR` (`FUN_00097e78`), formats
  `"%02X:%02X:%02X:%02X:%02X:%02X"` (uppercase, colon-separated, 17 chars), and hashes with MD5
  lowercase hex (`FUN_00097a4c`); the fallback is a **random 10-char seed** when `wlan0` is
  unavailable. The fingerprint is **bound to the token server-side on first contact**; changing it
  later yields `400 {"detail":"Invalid fingerprint"}` / `403` — recovery requires a fresh token.
- **Firmware/source behavior:** lowercase MD5 of exact uppercase `AA:BB:CC:DD:EE:FF` text.
  **[confirmed]**
- **Deployment state:** the deployed token is bound to the **static fingerprint in
  `config.ini`**; the source now honors that value (`identity.resolve_fingerprint`) so the
   registered identity keeps working. **Live-verified 2026-09-18:** `/c/info` → `200`
   (`origin='OTHER', registered=True`), snapshots → `200`, `camera_authentication` ACK `0`
   (success; the earlier note recording `1` was a misread — `1` is "not authorized"). A
  deploy that switched to the MAC-derived fingerprint instead caused
  `400 {"detail":"Invalid fingerprint"}` / `403`; the configured value was restored.
- **Historical legacy operation (optional):** the step-by-step `config.ini` migration runbook is in
  [`next-steps.md`](next-steps.md) ("Identity migration runbook"): read
  `/sys/class/net/wlan0/address`; mint a fresh Connect token via the Buddy3D add-camera flow
  (`origin: OTHER`); remove `[identity] fingerprint` from `config.ini` and set the fresh token;
  deploy with the legacy developer workflow; verify `/c/info` 200 (`origin=OTHER`,
  `registered=True`), `PUT /c/snapshot` 200, `camera_authentication` ACK `0`, and the
  `X-Camera-Fingerprint` header = `md5("<UPPERCASE:COLON:MAC>")`.
- **Acceptance:** `/c/info`, snapshot, and `camera_authentication` all succeed using the derived
  fingerprint under the fresh token; then repeat the viewer registry/auth checks.

### GAP-IDENTITY-03 — Track the physical Wi-Fi MAC/OUI difference

- [x] **P3 · Closed 2026-09-20: documented per-device difference, not a defect; OUI not inspected and the registry-gate theory is superseded**
- **Resolution 2026-09-20:** the Pi reports and hashes its real `wlan0` MAC (`<wlan0-mac>`), so
  it exposes a Raspberry-Pi OUI — the same algorithm the firmware uses, on different hardware. The
  firmware never inspects the OUI (it hashes whatever `SIOCGIFHWADDR` returns), and the only place it
  could conceivably have mattered — the Connect camera-service registry gate — is **superseded**
  (WebRTC works live in the app and browser, GAP-WEBRTC-03). **No action.** Keep the experiment note
  below only if a genuine Buddy3D MAC/OUI ever becomes available from reliable evidence.
- **RE 2026-09-20 (Wave 1, direct 3.1.6 decompiler/ELF) [confirmed]:** the firmware does **not**
  inspect the OUI. It formats and hashes whatever `wlan0` reports, so the Pi-vendor OUI is an
  expected per-device difference, **not a defect**. The `smsc95xx` cmdline MAC is the `eth0`
  placeholder and is irrelevant to this path.
- **Firmware behavior:** reports and hashes the genuine camera's `wlan0` MAC. Buddy3D hardware uses a
  Realtek Wi-Fi chipset, but no authoritative genuine-camera OUI has been recovered. **[confirmed for
  MAC source/chipset; OUI unknown]**
- **Current behavior:** reports and hashes the Raspberry Pi's real `wlan0` MAC, therefore exposing a
  Pi-vendor OUI even though the derivation algorithm now matches firmware.
- **Connect impact:** normally a per-device MAC difference is expected. It remains a narrowly scoped
  enrollment hypothesis only because the camera-service registry gate is unexplained; the only
  untested place it could matter is the Connect registry gate, which stays open pending a
  genuine-OUI source.
- **Implementation/experiment:** first test the exact Pi MAC-derived fingerprint with a fresh token.
  Only test a spoofed OUI if a genuine Buddy3D MAC/OUI is obtained from reliable evidence; keep MAC,
  reported metadata, fingerprint preimage, and fresh token mutually consistent.
- **Acceptance:** record the exact redacted test preimage/OUI class, fresh-token binding, `/c/info`
  result, camera registry lookup, and viewer-auth ACK so the experiment is reproducible.

### GAP-STATUS-04 — Timezone representation

- [x] **P3 · Implemented and live-verified**
- **Firmware behavior (WP-8):** `FUN_000b1dc8` detects the timezone from the web API (`timezone.prusa3d.com`, JSON `timezone` when `status == success`, follows a 301 `Location`), `FUN_000b1620` swaps the `UTC+`/`UTC-` prefix into the POSIX form, `FUN_000b170c` writes `/etc/TZ` (64-char cap), and `FUN_000b130c` reads it back for status tag 5.10.1.
- **Implementation:** `timezone.py` (detect/parse/convert/read/write), `CameraState.tz_name`, `main.detect_timezone` (runs at startup over the shared aiohttp session), `status` reports the detected value; `write_tz_file` falls back to the unit's passwordless `sudo tee` so `/etc/TZ` is actually written. Tests: `tests/app/test_timezone.py`.
- **Live-verified 2026-09-19:** `timezone: API 'UTC+2' -> reported 'UTC-2'`, `/etc/TZ` = `UTC-2`, status sent, `/c/info` 200.
- **Still open:** golden capture from a genuine 3.1.6 device (not available offline); non-UTC± (IANA) values pass through unchanged.
- **Firmware behavior:** detects timezone through its configured/web timezone service and reports
  firmware state. **[confirmed at service level; exact status string format needs fixture]**
- **Current behavior (implemented, live-verified 2026-09-19):** detects the timezone from the web
  API, converts it, writes `/etc/TZ`, and reports the detected value (`UTC+2` → `UTC-2`).
- **Connect impact (resolved):** representation matches the service result.
- **Before (superseded):** sent `time.tzname[0]`, commonly an abbreviation such as `CET`/`CEST`,
  plus a fixed status value.
- **Implementation:** confirm whether firmware reports an IANA name, abbreviation, or service result
  before changing the field.
- **Acceptance:** representation matches a real-camera or assignment-path fixture for the same zone.
