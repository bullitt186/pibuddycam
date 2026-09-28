# Firmware-parity gap tracker (3.1.6)

Behavioral differences between the fully decompiled Buddy3D Camera firmware `3.1.6`
and the Raspberry Pi impersonator in [`app/`](../../app/).

This document tracks only behavior, state, settings, and parameters visible to Prusa Connect
or affecting a Connect-requested operation. Rockchip-specific implementation details are out of
scope unless they change the wire behavior. Firmware `3.1.6` has no observed cloud-protocol
change from `3.1.5`; the gaps below are implementation gaps, not newly introduced 3.1.6
requirements. **[confirmed]**

Closed gaps are summarised at the end; their full evidence lives in
[`_archive/docs/gap-tracker-closed.md`](../../_archive/docs/gap-tracker-closed.md). The decompiler
evidence behind each gap is in [firmware behaviour](firmware-behaviour.md). How to work a gap
(including for agents) is described in [docs/agents/protocol-gap-work.md](../agents/protocol-gap-work.md).

## Status legend

| Marker | Meaning |
|---|---|
| `[ ]` | Open |
| `[~]` | Partially implemented or implemented but not verified |
| `[x]` | Matched and verified |
| `[-]` | Deliberately not implemented; wire behavior must still be truthful |
| `BLOCKED` | Cannot be exercised end-to-end until an external prerequisite is resolved |

Priority describes likely Prusa Connect impact:

- **P0** — prevents or is likely to prevent a core Connect operation.
- **P1** — produces incorrect control/state behavior visible to Connect.
- **P2** — observable compatibility or parameter difference with limited core impact.
- **P3** — cosmetic, diagnostic, or defensive parity.

Evidence labels follow the rest of this repository: **[confirmed]** means directly traced in
firmware, observed live, or both; **[assumption]** identifies the narrowest remaining inference.

# Open and partial gaps

## P0 — core operation gaps

### GAP-TRIGGER-01 — Decode and dispatch the requested trigger

- [~] **P0 · Implemented (9291968): recovered descriptor 0x3f6f14; policy actions (fw_update/timelapse) and client_trigger results still open**
- **RE 2026-09-20 (Wave 1) [confirmed]:** the per-action result message is the 6-field
  `client_trigger` descriptor **`0x3f6f58`** (strings 1/2/4, uvarints 3/5/6). The **emit mechanism
  is a Socket.IO EVENT (`client_trigger`), not an ack** — the ack callbacks are the server's
  receipt ack. Nine sender wrappers were recovered (error codes: SD not mounted / SD RO / low
  space / timelapse SD not mounted / RTSP start failed; FW upgrade progress 1..6;
  timelapse-video-make status 1 IN_PROGRESS / 2 FINISHED), but **which tag carries code vs
  progress vs message is unresolved** `[assumption]` — do not implement a wire format yet.
- **RE 2026-09-20 (Wave 2, direct 3.1.6 decompiler/ELF) [confirmed]:** the `client_trigger`
  descriptor `0x3f6f58` tag semantics are now recovered — **tag5 uvarint = result/error code**
  (`FUN_000a2d68`=2, `FUN_000a2ca0`=5), **tag6 uvarint = upgrade/progress value** (`FUN_000a2e3c`=1,
  `FUN_000a2fec`=2, `FUN_000a2f80`=3), **tag3 uvarint = timelapse-video-make status**
  (`FUN_000a2bbc`=1 IN_PROGRESS / 2 FINISHED). tag1 = constant, tag4 = `FUN_0008286c()`
  (token-shaped), tag2 = a shared global. The message/subtype split among the string tags 1/2/4
  remains `[assumption]`. It is emitted as a Socket.IO **event**, not an ack. **Do not implement a
  wire emission:** the string tags are unresolved and this Connect version exposes no
  make-video/file-list UI to consume it; emission is deferred until the string tags are pinned and
  a consumer exists.
- **Firmware behavior:** decodes the trigger type and performs only the requested action: get
  features, get status, get protocol information, get snapshot, enable/disable snapshot upload,
  enable/disable timelapse, make timelapse video, list timelapse files, reboot, start firmware
  update, or start/stop RTSP. **[confirmed]**
- **Current behavior:** extracts a probable request ID, sends `status`, `protobuf_version`, and
  `features`, then uploads a snapshot for every trigger.
- **Connect impact:** incorrect responses and side effects; settings and control actions may appear
  accepted while doing nothing.
- **Implementation:** recover the exact trigger message descriptor and enum values; introduce a
  typed dispatcher; invoke only the requested action; emit the corresponding firmware-style
  result/error message.
- **Acceptance:** fixture tests for every recovered trigger prove that only its intended action is
  called and the correct response event/payload is emitted.
- **Code:** [`main.py`](../../app/main.py),
  [`trigger.py`](../../app/trigger.py)
- **Implementation:** [`trigger.py`](../../app/trigger.py)
  decodes the recovered descriptor `0x3f6f14` (`decode_trigger`, returning a tag-keyed
  `TriggerMessage` with a normalized tag-11 `request_id`) and produces an ordered action plan
  (`trigger_actions`) that fires only the exact documented `(tag, value)` pairs. `main.py`'s
  trigger handler now performs only the planned actions: `status`/`features`/`protocol_info`
  (correlated on tag 11), immediate `snapshot`, snapshot upload enable/disable, and RTSP
  start/stop through `rtsp_control.apply_mode` with persistence. The policy actions `fw_update`,
  `reboot`, and `timelapse_enable/disable/make/file_list` are recognized and logged as not
  implemented; they no longer cause an unrelated response or a fake success. Tag 13 is decoded
  and logged only. Tests: `test_trigger.py`. Remaining: per-action `client_trigger`
  result/error codes (GAP-SIO-01) and the policy decisions for OTA/reboot/timelapse
  (GAP-OTA-01/GAP-DEVICE-01/GAP-TIMELAPSE-01). Trigger result acks are not sent because the
  installed `python-socketio` trigger handler signature carries no ack callback.

### GAP-CONFIG-01 — Replace guessed configuration decoding with the recovered schema

- [~] **P0 · Corrected 2026-09-19: the SIO `configuration` event is a nested protobuf, NOT JSON; video_quality mapped, others in progress**
- **Correction (live-verified):** the earlier "JSON" conclusion was wrong for the Socket.IO path. Live Connect setting changes arrive as a **nested protobuf** (descriptor `0x3f73a4`, 9 fields; handler `FUN_000a7940` decodes it and dispatches by name — `'video_quality'`, `'light_control'`, `'motor_controll'`, `'set_snapshot_upload_interval'`, `'set_timelaps_interval'`). `FUN_000a89e0` is **not** a function; Ghidra had not defined the real dispatcher, which was recovered by forcing a function at its ARM prologue. The JSON parser (`nlohmann`, `FUN_0006f9dc`) is only the QR/manual-config path. The JSON handler rejected every live setting change ("not valid JSON").
- **Mapped (live):** `tag8.1` = video quality enum (1=SD/2=HD/3=FHD) — implemented in `main.py` via `handle_quality`. Verified: the encoder actually changes tier; the WebRTC viewer must stop/start the stream to pick up the new resolution (the peer connection is built with the resolution at offer time) — acceptable.
- **Mapped (live 2026-09-19):** `tag3.5` = `set_snapshot_upload_interval`. Connect's cameras page **"Displayed Frame Update Interval"** slider sends `configuration {3: {5: <seconds>}}`; the Pi previously logged and ignored it. Direct evidence: `FUN_000a7940` reads tag3.5, logs `"Upload interval: %d seconds"` / `"Setting upload interval: %d seconds"`, rejects outside **10..600** (`"Invalid upload interval: %d"`), and dispatches the name `set_snapshot_upload_interval`. Wired to `state.set_snapshot_interval`; **verified live**: slider → `{3: {5: 20}}` → `Config: snapshot_upload_interval (tag3.5) → 20s` and the snapshot cadence settled to exactly 20 s. Note: the runtime interval is not persisted (reverts to `config.ini` on reboot) — persistence is a follow-up.
- **In progress:** `tag3` carries the remaining settings (subfields 4/11/12 observed live).
- **RE 2026-09-20 (Wave 1, direct 3.1.6 decompiler/ELF) [confirmed]:** the `tag3` submessage
  descriptor is **`0x3f6d2c`** (11 fields): 1 submsg @0x04, 2 uvarint @0x0c, 3 submsg @0x14,
  **4 uvarint @0x1c = `light_control`**, **5 uvarint @0x20 = `set_snapshot_upload_interval`
  (10..600)**, 7 uvarint @0x24, 8 uvarint @0x28, 9 uvarint @0x2c, **10 string @0x30 =
  `set_printing_job_name`**, **11 uvarint @0x38**, **12 uvarint @0x3c**. Because tags 11/12 are
  **uvarints**, the tracker's earlier "RTSP candidate" label for them is **unconfirmed** here and
  **refuted** in Wave 2 (below); do not implement an RTSP mapping from them. The
  dispatch-name pool also includes `set_volume` (5..100) + `play_voice`, `set_camera_name`,
  `set_rtsp_server_mode` (AUTO/ON/OFF), `set_webrtc_mode`, `set_timelaps_interval`,
  `set_timelaps_video_fps`, and the quality+TURN lock.
- **RE 2026-09-20 (Wave 2, direct 3.1.6 decompiler/ELF) [confirmed]:** `tag3.4` = `light_control`,
  `tag3.5` = `set_snapshot_upload_interval` (10..600), and `tag3.10` = `set_printing_job_name` are
  confirmed. `tag3.7` is plausibly `set_volume` (5..100) `[assumption]`. **Tags `3.11`/`3.12` are
  uvarints and the "RTSP candidate" label is REFUTED/unconfirmed** — do not implement an RTSP
  mapping there. The RTSP mode is a small enum toggle elsewhere (`iStack_74` 1/2/3); the exact tag
  remains `[assumption]`.
- **Recovered (2026-09-19, direct decompile):** the config dispatcher is `FUN_000a7940` (`0xa7940`-`0xa885b`; Ghidra had not defined it as a function — recovered by forcing a function at the ARM prologue `push {r4-r9,sl,fp,lr}` and decompiling). It decodes descriptor `0x3f73a4` and dispatches by name: **top-level field 2 (struct offset `0x14`) = `set_timelaps_interval`** (dispatcher logs `"Timelapse interval: %d seconds"`), field 8 = video quality (`Video quality: %d`), the field-9 region = `set_timelaps_video_fps` (`"Timelapse video FPS: %d"`), and field 5 = `set_webrtc_mode` / `set_rtsp_server_mode`. Live Connect sends `configuration {2: <seconds>}` when the timelapse interval changes; `main.py` now maps it to `state.set_timelapse_interval` (1..3600).
- **Implementation:** `main.py`'s `configuration` handler now parses JSON only (the generic protobuf fallback and all numeric-tag lookups are removed) and dispatches the recovered table exactly: `rtsp` on/off, `webrtc` on/off (with the paired `webrtc on → RTSP disabled` rule), `video_quality` sd/hd/fhd, `light_control` auto/night/day, `camera_name`, `snapshot_interval` 10..600, `start_fw_update` start, and the leading `code` rejecting `"42"`/`"66"`.
- **Still open:** a redacted golden JSON capture from a genuine 3.1.6 device to pin exact value casing; `start_fw_update` remains a truthful no-op (owner decision).
- **Firmware behavior:** decodes a protobuf configuration message and dispatches named settings
  including `rtsp`, `webrtc`, `video_quality`, `start_fw_update`, `light_control`, `camera_name`,
  and `snapshot_interval`. **[confirmed]**
- **Current behavior (implemented):** decodes the nested protobuf `0x3f73a4` and dispatches the
  recovered table: top-level field 2 (`set_timelaps_interval`), `tag8.1` video quality,
  `tag3.4` `light_control` (truthful unavailable — no IR hardware), `tag3.5`
  `set_snapshot_upload_interval` (10..600), and the JSON name-keyed fields (`camera_name`,
  `rtsp`, `webrtc` incl. the paired `webrtc on → RTSP disabled` rule, `video_quality`,
  `start_fw_update` decline, `code` guard). `tag3.11/12` are logged as an unmapped candidate;
  they are **uvarints** (`0x3f6d2c`), so the earlier "RTSP candidate" reading is **refuted**
  (Wave 2); the RTSP mode is a small enum toggle elsewhere.
- **Connect impact (resolved):** live setting changes are applied and persisted rather than
  misidentified; the `tag3.11/12` mapping remains unknown and is logged, not guessed.
- **Before (superseded):** first attempted JSON decoding, then applied a generic flat protobuf
  decoder with guessed numeric tags, no presence tracking, enum types, signed integer handling,
  or nested-message schema; a valid Connect configuration payload could be misidentified,
  ignored, or interpreted as a different setting.
- **Implementation:** define the exact configuration schema and typed decoder from the nanopb
  descriptor/callback paths; preserve optional-field presence; reject malformed values exactly
  where firmware does.
- **Acceptance:** captured or constructed firmware-compatible payloads for every setting decode to
  the expected typed action; malformed/easter-egg guard payloads reproduce firmware rejection.
- **Code:** [`main.py`](../../app/main.py),
  [`proto.py`](../../app/proto.py)

## P1 — Connect-visible control and state gaps

### GAP-WEBRTC-04 — Apply `set_webrtc_mode`

- [~] **P1 · Implemented; live verification pending**
- **Firmware behavior:** protobuf field 1 value `1` starts/enables the WebRTC service; `0` stops and
  disables it. Mode and runtime status are separate values and gate inbound offers. **[confirmed]**
- **Current behavior (implemented):** `webrtc_control.apply_mode` decodes field 1, starts/stops the
  `PrusaWebRTC` loop, tracks `state.webrtc_mode` and `state.webrtc_status` separately, gates
  inbound offers, and reports the real mode/status in `status`.
- **Connect impact (resolved):** Connect can control the service and sees the actual mode/runtime
  state.
- **Before (superseded):** the event was logged but had no state or service effect; status always
  reported mode `1`, status `1`.
- **Implementation:** decode field 1, persist/track mode, start or stop WebRTC resources, reject
  offers while disabled, and report actual mode/runtime status.
- **Acceptance:** enable/disable fixtures alter the gate and subsequent `status` payload exactly as
  expected.
- **Code:** [`signaling.py`](../../app/signaling.py),
  [`signaling.py`](../../app/signaling.py)
- **Implementation:** [`webrtc_control.py`](../../app/webrtc_control.py)
  decodes field 1 (not the `0x08` tag byte), applies the `FUN_000b94ac` enable/disable state machine,
  and exposes the `FUN_000b996c` gate (`offer_allowed`). `main.py`'s offer handler consults the gate,
  and `set_webrtc_mode` / `configuration.webrtc` start/stop the `PrusaWebRTC` GLib loop with
  `state.webrtc_mode` and `state.webrtc_status` tracked separately. Tests:
  `test_webrtc_control.py`. **Correction:** the paired rule where `configuration.webrtc=on` also
  forces RTSP disabled **is implemented** (`main.py` `handle_event`, `Config: webrtc on → RTSP
  forced disabled (paired rule)`), consistent with GAP-CONFIG-01; WebRTC mode is persisted via
  `settings_store` / `state.json` (GAP-PERSIST-01), not memory-only.

### GAP-WEBRTC-05 — Honor transport policy, TTL, SDP plan, scoped quality, FPS and scope

- [~] **P1 · TURN/scoped-quality lock implemented; remaining policy fields confirmed but not enforced**
- **RE 2026-09-20 (Wave 1, direct 3.1.6 decompiler/ELF) [confirmed]:**
  - Descriptor **`0x3f7680`** (9 fields): 1/2/3 strings (token/client/session); **4 submessage @0x1c
    (16 B, SDP/candidate)**; **5/6/7 uvarints @0x2c/0x30/0x34**; **8 submessage @0x3c (16 B, ICE
    config)**; **9 submessage @0x50 (20 B = the scalar policy group)**.
  - Inbound handler is **`FUN_000a43e0`** (unexported; recovered via Ghidra): it decodes the
    message, reads tag5 (`+0x2c`), tag6 (`+0x30`, checked `==1`), tag7, and tag9's fields, then
    calls the gate/enqueue **`FUN_000b996c`**.
  - Live offers carry keys `[1,2,3,5,7,8,9]` with **tag5 present, tag6 absent, tag7=2, tag9=11 B**
    -> Connect sends only a subset of the descriptor.
  - The **TURN/scoped-quality lock is in the CONFIG path** `[confirmed]`: `FUN_000a7940` quality
    branch calls `FUN_000b5ad4` -> `FUN_000b4f90` reads the "TURN client online" flag at
    **singleton+0x278**; if 0 -> allowed; else a cap check `FUN_000b51e8`; apply `FUN_000b4e98`
    (writes singleton+0x130). Log string `DAT_000a89c4`.
- **RE 2026-09-20 (Wave 2, direct 3.1.6 decompiler/ELF) [confirmed]:** the per-field names are
  now recovered from the handler `FUN_000a43e0` + descriptors — **tag7 = Client type, tag5 = Msg
  type, tag2 = client id, tag4.f1 = SDP, tag8.2 = Transport policy, tag8.3 = TTL**, and the tag9
  submessage (`0x3f7624`, 5 uvarints) = **tag9.1 Quality, tag9.2 FPS, tag9.3 Plan, tag9.4 TTL,
  tag9.5 Scope**; "VideoCfg" is hardcoded to `1`. **Note the TURN lock above.** Transport
  policy/TTL/FPS/plan/scope are **decoded but not enforced** (Connect sends only a subset), so they
  remain documented-not-enforced rather than guessed. Do not implement them.
- **Implementation (TURN/scoped-quality lock):** `CameraState.turn_online` is set when an inbound
  viewer ICE candidate is of type `relay` (a TURN client) and cleared on WebRTC stream end and
  peer teardown (`on_stream_ended`/`_teardown` -> `on_teardown`). The quality-apply path
  (`main.handle_quality` -> `quality_control.handle_quality`) consults the pure
  [`quality.quality_change_allowed`](../../app/quality.py): while a TURN client is online a
  raise (`requested > current`) is rejected with the recovered warning log and no live/persist
  effect; lowering/equal and the no-TURN case are unchanged. Tests:
  `tests/app/test_turn_quality_lock.py`.
- **Firmware behavior:** consumes inbound fields for transport policy, TTL, video configuration,
  plan, quality, FPS, scope/lifetime, and ICE configuration. It applies per-client quality limits
  and locks incompatible quality changes while a TURN client is active. **[confirmed]**
- **Current behavior (corrected):** the TURN/scoped-quality lock is now enforced (above). The
  remaining policy fields are decoded but not enforced. **The stale claim that `scope` is read from
  field 12 is wrong and is removed:** `proto.py` has no `scope` key at all, and field 12 of the old
  flat table is not the camera-side schema (the inbound message is the 9-field descriptor above,
  whose field 9 is the scalar policy group and tag9.5 is the scope). Video is always 30 FPS and uses
  the locally persisted global quality.
- **Connect impact:** relay-only requests, bandwidth limits, session lifetimes, and requested video
  profiles are not honored; only the fields Connect actually sends are candidates for enforcement.
  The TURN quality lock now matches firmware.
- **Implementation (remaining):** enforce transport policy and TTL, configure plan/FPS, and add
  per-client scoped-quality arbitration. **Only RE-backed, Connect-observed fields may be
  enforced**; leave the rest documented as unenforced.
- **Acceptance:** parameterized tests demonstrate distinct ALL/RELAY, TTL, SD/HD/FHD, and FPS
  behavior; TURN sessions block global quality changes like firmware (covered by
  `tests/app/test_turn_quality_lock.py`).
- **Code:** [`proto.py`](../../app/proto.py),
  [`webrtc.py`](../../app/webrtc.py)

### GAP-WEBRTC-06 — Emit `webrtc_connection_info`

- [~] **P1 · Implemented but GATED OFF: live server rejected the numeric encoding (field types unconfirmed)**
- **Live 2026-09-20:** with the numeric encoding (fields 2/3 as bytes) the Pi emitted
  `{1: client_id, 2: 6, 3: 6}` after ICE completed and the server answered the `error` event
  **`webrtc_connection_info - Error: Read past limit`** — so fields 2/3 are most likely
  **strings**, not bytes (the original `protocol.md` note). The numeric-vs-string question must be
  settled from a genuine capture or the C++ message's `.proto`; per the no-guess rule the sender is
  kept but **disabled by default** (`PRUSA_WEBRTC_CONNECTION_INFO=1` gates it). Separately, the
  GStreamer `get-stats` scan did not find the selected pair on the Pi (emitted a misreporting 6/6),
  so the extractor now **skips** rather than misreport when the pair is not extractable.
- **RE 2026-09-20 (Wave 1, direct 3.1.6 decompiler/ELF) [confirmed]:**
  - The sender is **`FUN_000be3f8`** (the older `FUN_000a3998` citation is wrong). It logs
    `"Sending connection type event for client %s: %d <-> %d"` and
    `"Failed to get selected candidate pair for %s"`, runs **after candidate-pair selection**, and
    when no pair exists forces both types to **6**.
  - It calls `FUN_000be270` -> the encoder `FUN_000be050`/`FUN_000bdd3c`: field 1 = client id
    string, field 2 = local type byte, field 3 = remote type byte. Fields 4/5/6 are not populated
    by this path; their semantics are `[assumption]`/unset.
  - Candidate-type translator **`FUN_000b5098`**: input 1 -> **1 HOST**, 2 -> **2
    SERVER_REFLEXIVE**, 3 -> **3 PEER_REFLEXIVE**, 4 -> **4 RELAYED**, 0 -> **5 UNDEFINED**,
    anything else -> **0 UNKNOWN**; forced **6** when there is no pair.
  - **Wire type correction:** fields 2/3 are numeric bytes, not strings. `protocol.md` previously
    rendered them as `string local_type`/`string remote_type`; corrected 2026-09-20.
- **Firmware behavior:** after ICE selection, emits `webrtc_connection_info` containing client ID,
  local candidate type, and remote candidate type (`HOST`, `SERVER_REFLEXIVE`, `PEER_REFLEXIVE`,
  `RELAYED`, etc.). **[confirmed]**
- **Current behavior (implemented):** `webrtc_lifecycle.candidate_type_code` maps the ICE `typ`
  token to the recovered code space; `webrtc.py` reads the selected candidate pair from webrtcbin
  `get-stats` on `notify::ice-connection-state` reaching CONNECTED/COMPLETED and calls
  `signaling.send_webrtc_connection_info` (event `webrtc_connection_info`, fields 1/2/3 only).
  Missing stats log and skip; a present-but-empty pair emits 6/6, matching `FUN_000be3f8`.
- **Connect impact (resolved on the wire):** Connect receives the connection telemetry it expects
  and can distinguish direct from relayed sessions; live verification pending.
- **Implementation:** obtain selected candidate-pair stats from GStreamer, map candidate types to
  the firmware numeric enum, encode the three populated fields, and emit it after selection.
- **Acceptance:** direct and forced-relay tests produce the correct event and candidate types.
- **Implementation:** helpers in
  [`webrtc_lifecycle.py`](../../app/webrtc_lifecycle.py) (`candidate_type_code`,
  `parse_candidate_type`, `connection_info_payload`), emission in
  [`webrtc.py`](../../app/webrtc.py) (`_emit_connection_info`/`_on_stats_ready`),
  sender in
  [`signaling.py`](../../app/signaling.py) (`send_webrtc_connection_info`), wired from
  [`main.py`](../../app/main.py) (`on_connection_info`). Tests:
  `tests/app/test_webrtc_connection_info.py`. **Assumption:** the exact GStreamer `get-stats`
  structure shape is version-dependent; the extractor is a best-effort recursive scan and skips
  the event when the selected pair cannot be identified (never guesses).

### GAP-STATUS-02 — Correct request correlation in `status`

- [~] **P1 · Implemented and unit-tested (d1ec311); fixture/live comparison pending**
- **Firmware behavior:** conditionally supplies field 10 from the request/correlation value when the
  corresponding presence flag is set. Initial live captures also show the Socket.IO SID in this
  position, so the exact source depends on send context. **[confirmed for conditional firmware
  path; initial-vs-response selection needs a fixture]**
- **Current behavior (implemented):** `build_status_message` sets field 10 from `request_id` for a
  request-triggered status, and falls back to the Socket.IO SID when a trigger carries no
  request-id. (The old "unsolicited initial status" path no longer exists — nothing is sent
  post-auth, GAP-AUTH-01.)
- **Connect impact (resolved):** a requested status response correlates with the request that
  caused it.
- **Before (superseded):** `send_status(request_id=...)` accepted a request ID but the status
  encoder ignored it and always encoded the Socket.IO SID.
- **Implementation:** distinguish unsolicited initial status from request-triggered status and set
  field 10 from the correct context.
- **Acceptance:** initial-status and request-response fixtures encode different expected field 10
  values and match a real-camera capture or descriptor-driven test.
- **Code:** [`signaling.py`](../../app/signaling.py)

### GAP-SNAPSHOT-02 — Implement snapshot enable/disable triggers

- [~] **P1 · Implemented and unit-tested (9291968); live verification pending**
- **Firmware behavior:** `enable_snapshot_upload` and `disable_snapshot_upload` control the periodic
  uploader independently of immediate get-snapshot requests. **[confirmed]**
- **Current behavior (implemented):** trigger tags 4/5 values `1`/`2` set the shared
  `state.snapshot_upload_enabled`, which `periodic_snapshot_allowed` reads; immediate
  get-snapshot remains independent of that switch.
- **Connect impact (resolved):** remote snapshot control starts/stops the periodic uploader.
- **Before (superseded):** the periodic loop always ran unless locally paused for RTSP/WebRTC;
  trigger enable/disable was not decoded, so remote snapshot control did nothing.
- **Implementation:** add an explicit upload-enabled state and handle both trigger values; reflect it
  in status while preserving immediate snapshot behavior.
- **Acceptance:** disable stops periodic uploads, get-snapshot still performs its defined action, and
  enable resumes the configured cadence.
- **Implementation:** recovered trigger tags 4/5 values `1`/`2` now map to
  `snapshot_enable`/`snapshot_disable` and are applied through
  [`trigger.py`](../../app/trigger.py) `apply_snapshot_upload`, which sets the shared
  `state.snapshot_upload_enabled` that `periodic_snapshot_allowed` already reads. Immediate
  get-snapshot is independent of that switch and keeps only the existing WebRTC pause. Status has
  no recovered field for this flag, so none is emitted rather than inventing one. Tests:
  `test_trigger.py` (`SnapshotControlTests`). Live cadence verification on the Pi remains
  pending.

## P2 — parameter and semantic differences

### GAP-QUALITY-02 — Reproduce the quality persistence flag and recover event wiring

- [~] **P2 · Partial: live/persist split, failure rollback, and appliance privilege path implemented and tested; event→flag wiring still unrecovered**
- **Live failure evidence 2026-09-25:** Connect's nested configuration reached the running
  appliance and decoded as quality `3`, but the service account's legacy direct
  `sudo systemctl restart ...` calls were rejected by the image's deliberately narrow sudoers
  policy (`command not allowed`), so the encoder was never reconfigured. The quality restart now
  uses the image's fixed-verb `pibuddycam-priv quality-restart` action. This preserves the privilege
  boundary while restarting exactly `rpicam-source.service` and `pibuddycam-ha-rtsp.service`, followed
  by `try-restart pibuddycam-rtsp.service` so a disabled Prusa RTSP mode stays disabled.
- **Live-verified 2026-09-25 (`c1d3e76`, application release `1.0.4`):** an authenticated
  Connect viewer sent the real nested `configuration` form (`tag8.1=2`, then `tag8.1=3`). The
  running OV5647 encoder changed from `1920×1080` to `1280×720` and back to `1920×1080`; both RTSP
  endpoints then reported H.264 at `1920×1080`, snapshots continued returning 200, all four
  runtime services remained active, and the journal contained no `command not allowed` failure.
  Local verification: 1,574 Python tests passed (2 skipped), plus `compileall`.
- **RE status 2026-09-20 (Wave 2) [confirmed]:** the persist flag is a **per-payload** flag for
  `change_video_size` — `FUN_00072f08` calls the persistence setter only when `*param_3 != 0` — not
  a registration-time constant. The `save_video_size` handler is in an unexported gap. Best
  hypothesis: `save_video_size` = persist, `change_video_size` = live-only `[assumption]`; **do not
  guess a flag.**
- **Firmware behavior:** the recovered handler `FUN_00072f08` always attempts the live resolution
  change for raw values `5`, `6`, or `7`. It additionally calls the persistence setter only when
  `*param_3 != 0`, and updates its in-memory current value only after the live changer succeeds.
  **[confirmed]** The exact assignment of flag `0`/`1` to the indirectly registered
  `change_video_size` and `save_video_size` Socket.IO callbacks is **not yet confirmed**.
- **Current behavior:** our code persists on both configuration quality paths (`tag8.1` and the JSON
  `video_quality`), while both direct events are currently mapped live-only in
  `QUALITY_EVENT_PERSIST` (`change_video_size`/`save_video_size` -> `False`) and do not read the
  per-payload flag. Either refine later to read the payload flag or keep documenting it; do not
  infer the flag from the event name.
- **Connect impact:** persistence occurs even on the non-persisting firmware path, and failed live
  changes can still be recorded as if they succeeded.
- **Implementation:** split `apply_live_quality(raw)` from `persist_quality(raw)`. Make live apply
  return success; update shared current state only on success; invoke persistence only when the
  recovered callback supplies a nonzero flag. Do not assign the flag by event name until the
  registration callback/capture establishes it.
- **Acceptance:** direct handler tests prove that flag `0` performs a successful live change with no
  write, flag `1` performs the same live change plus one write, and live-change failure performs
  neither the current-state update nor persistence. A separate fixture pins each event name to its
  recovered flag.
- **Code:** [`main.py`](../../app/main.py)

### GAP-RTSP-02 — Track configured mode separately from runtime state

- [~] **P2 · Implemented; live verification pending**
- **Firmware behavior:** handles disabled/enabled modes (`1`/`2` on the recovered direct event),
  starts/stops the server, tracks clients, and reports mode/status/URL dynamically. **[confirmed]**
- **Current behavior (implemented):** direct and configuration-form commands share
  `rtsp_control.apply_mode`, which starts/stops `pibuddycam-rtsp.service`, sets `state.rtsp_mode`, and
  resolves `state.rtsp_running` from `systemctl is-active`; the configured mode persists at
  `/etc/pibuddycam/rtsp.mode` (and in `state.json` via GAP-PERSIST-01).
- **Connect impact (resolved):** reported and actual RTSP state stay consistent.
- **Before (superseded):** direct start/stop called systemd but status remained hardcoded and
  configuration-form RTSP changes were only logged, so reported and actual state could diverge.
- **Implementation:** define persisted/configured mode and actual service state; handle direct and
  configuration-form commands through one path; choose boot behavior from mode.
- **Acceptance:** disable survives the intended persistence boundary, status follows service state,
  and both command forms behave identically.
- **Implementation:** [`rtsp_control.py`](../../app/rtsp_control.py)
  decodes the direct field-1 mode, maps `configuration.rtsp` `on`/`off` to `2`/`1`, and applies both
  through one `apply_mode` path that starts/stops `pibuddycam-rtsp.service`, sets `state.rtsp_mode`, and
  resolves `state.rtsp_running` from `systemctl is-active` (falling back to the commanded state when
  the unit cannot be probed). The configured mode persists at `/etc/pibuddycam/rtsp.mode`
  (`PIBUDDYCAM_RTSP_MODE_FILE` override) and is read at startup. On the appliance, persistence is owned
  by `/data/pibuddycam/state.json`; `/etc/pibuddycam` is tmpfs re-materialized at boot. The older
  lower-filesystem/`deploy.sh` statement applied only to the legacy developer install. Tests:
  `test_rtsp_control.py`. Default mode when the file is absent is `2` (enabled), matching the
  shipped unit; the firmware's shipped default remains unrecovered. Client tracking is unchanged
  (`/proc/net/tcp`).

### GAP-SNAPSHOT-03 — Match JPEG encoding quality

- [~] **P2 · Implemented and unit-tested (d1ec311); live verification pending**
- **Firmware parameter:** snapshot JPEG conversion uses quality `95`. **[confirmed]**
- **Current parameter (implemented):** GStreamer `jpegenc quality=95` (`camera.py`).
- **Connect impact (resolved):** JPEG quality and payload size match firmware.
- **Before (superseded):** GStreamer `jpegenc quality=85`.
- **Implementation:** set 95 unless Pi bandwidth/CPU testing justifies and documents a deliberate
  deviation.
- **Acceptance:** encoder configuration and a captured image report quality target 95.
- **Code:** [`camera.py`](../../app/camera.py)

### GAP-SNAPSHOT-04 — Match snapshot scheduling and concurrent-stream behavior

- [~] **P2 · Implemented and unit-tested; live concurrent-stream verification open**
- **Firmware behavior:** coordinated hardware channels allow snapshot service state to be controlled
  independently from RTSP/WebRTC. **[confirmed at service/state level]**
- **Current behavior:** the monotonic start-to-start deadline is implemented. Periodic and explicit
  snapshots no longer consult RTSP/WebRTC activity: JPEG capture, RTSP, and WebRTC all consume the
  shared `stream_mux.py` source, so starting a stream does not intentionally pause Connect uploads.
  WebRTC teardown still clears lifecycle state, but snapshot scheduling is independent of it.
- **Implementation (local, live verification pending):** `scheduling.py` uses a monotonic
  start-to-start deadline (`next_deadline`), so capture/upload duration no longer inflates cadence
  and an interval change catches up immediately. `CameraState.periodic_snapshot_allowed()` now
  reflects only the configured snapshot-upload enable flag, and the explicit trigger path has no
  streaming guard. Tests: `test_scheduling.py`, `test_state.py`, and
  `test_onvif.py::MainWiringTests`.
- **Connect impact:** normal snapshot cadence during local RTSP is now live-verified; the same path
  has source-level coverage for WebRTC but still needs a simultaneous live Connect/App session.
- **Partial live verification (2026-09-20, `312b59b`):** while a GStreamer client held the deployed
  Home Assistant RTSP endpoint (`:8555/live`) open for 25 seconds, Connect snapshots returned 200 at
  21:00:31 and 21:00:41 (configured 10-second cadence). The shared-mux RTSP half is therefore live
  verified; simultaneous Prusa WebRTC plus snapshots is still required before closing the gap.
- **Before (superseded):** periodic snapshots were skipped while RTSP/WebRTC was active and, with the
  then-current WebRTC lifecycle bug, indefinitely after one offer.
- **Remaining evidence:** record simultaneous snapshot timestamps plus RTSP and WebRTC playback on
  the Pi, including a quality change, before changing this item to closed.
- **Acceptance:** snapshots continue at configured cadence during RTSP and WebRTC without camera
  contention.
- **Code:** [`main.py`](../../app/main.py)

### GAP-AUTH-01 — Require successful authentication ACK

- [~] **P2 · Wire behavior live-verified 2026-09-20; startup recovery fix implemented and unit-tested 2026-09-25, appliance redeploy pending**
- **Firmware behavior:** continues its post-authentication flow only on the successful ACK path.
  **[confirmed]**
- **Current behavior:** only the exact success ACK `0` proceeds; no post-auth events are sent
  (the server's `trigger` polls drive `status`/`features`/`protobuf_version`).
- **Connect impact:** invalid/rejected sessions transmit nothing and back off; the bogus
  `send_sio_info` event and the unsolicited `protobuf_version` (which the server answered with
  `CameraIsNotSessionMemberError`) are gone.
- **Implementation:** require the exact success value `0` (`FUN_0009e53c`: `0`=OK, `1`=not
  authorized, `2`=error joining session); mark the session unauthenticated and let the supervisor
  replace/back off otherwise. Post-auth sends removed (firmware `FUN_000a05e4` only logs/resets
  counters).
- **Acceptance:** ACK `0` proceeds; ACK `1`, `2`, `5`, malformed values, and timeout do not send any
  post-auth event.
- **Reliability regression found 2026-09-25:** the full-image appliance continued uploading
  snapshots and serving all local H.264 endpoints, but Connect settings and WebRTC both stopped.
  Browser WebSocket capture showed the viewer sending its `webrtc` request with no camera-side
  frames. The registered appliance token/fingerprint authenticated from an independent client with
  ACK `0`, and both the SD card's known-working dependency versions and the image's newer versions
  behaved identically, ruling out registration, protobuf field order, and dependency drift.
- **Root cause/fix:** `main` starts `supervise()` only after the initial `sio.connect()` returns, but
  the auth error path previously awaited `sio.disconnect()` from inside python-socketio's own
  `connect` callback. A transient startup timeout/rejection could therefore strand the initial
  call before the supervisor existed. The initial attempt is now bounded at 25 seconds, auth
  failure returns from the callback without a nested disconnect, and supervisor health requires
  both an Engine.IO connection and an exact successful auth ACK. Tests cover the bounded initial
  attempt and reject a connected-but-unauthenticated session.
- **Code:** [`signaling.py`](../../app/signaling.py),
  [`signaling.py`](../../app/signaling.py)

### GAP-TIMELAPSE-01 — Implement or stop advertising timelapse

- [~] **P2 · Firmware-named frames/AVI live-verified; make-video verified directly; file_list not app-exercisable**
- **2026-09-19 (live symptom + descriptor re-trace):** Connect shows *"Time lapse not available, camera storage not detected, insert SD card"*. The app reads storage from the **`status` message**, specifically the `extended_status.4` storage block (descriptor `0x3f72b0`; firmware labels it the "video/timelapse mode/storage block"). The descriptor was recovered exactly as **tags 1-4 uvarint + tag 5 callback string**: tag 1 = SD mounted state (`FUN_000744ac`, translated `0->2`, `1->1`, else `0`; i.e. **1 = mounted/present, 2 = not mounted**), tags 2/3/4 = total/free/used MB (`FUN_000745e0`, `(f_bsize * f_blocks) >> 20` etc. via `statvfs64("/mnt/sdcard")`), tag 5 = mount-mode string (`FUN_00073914` -> `"RW"`/`"RO"`/`"UNKNOWN"`). **Correction:** the earlier note mapping `FUN_000abcb0`/`FUN_000abaf4` into this block was wrong — those are TimelapseService singleton getters that belong to top-level `timelapse_status` (field 2), and the `MODEL` string previously emitted on tag 5 was a misread.
- **Implementation (2026-09-19, local; not live-verified):** [`timelapse.py`](../../app/timelapse.py) provides the Pi storage policy — `sd_present` (`os.path.isdir` + `os.access(R_OK)` over `/mnt/sdcard`, matching the firmware's `FUN_00071bc0` accessibility check; the Pi has no block device), `sd_space` (`statvfs` MB with the firmware shift), `sd_mode` (`RW`/`RO`/`UNKNOWN`), and `storage_status` (the 5-tuple). [`status.py`](../../app/status.py) encodes it on `extended_status.4` and wires `timelapse_status` tags 1/2 to `state.timelapse_enabled`/`state.timelapse_interval` (`FUN_000abcdc`/`FUN_000abcb0`); [`signaling.py`](../../app/signaling.py) supplies live telemetry. `deploy.sh` now chowns the `/mnt/sdcard` mountpoint itself to the service user (not just `/mnt/sdcard/timelapse`) so the mode reports `RW`, and installs samba before writing its config. Tests: `tests/app/test_timelapse.py` (`StorageStatusTests`), `tests/app/test_status_schema.py` (storage-block and timelapse enable/interval).
- **Emulated SD (done, verified):** `/mnt/sdcard/timelapse` on the Pi (the exact firmware path), shared read/write over SMB as `\\<pi>\sdcard` (share `sdcard`; `smbd` on 139/445). `deploy.sh` provisions the dir + share + samba; `bootstrap.sh` installs samba. `MicroSd` is re-advertised.
- **Implementation:** `timelapse.py` (stdlib-only) provides interval/FPS validation, timestamped frame naming/storage, ordered listing, a minimal stdlib MJPEG-in-AVI writer, the `<name>:<status>` `.timelapse_videos.csv` index, and the firmware-shaped `file_list` fragmenter under `/mnt/sdcard/timelapse`. `CameraState` carries `timelapse_enabled/interval/fps`; `main.timelapse_loop` captures a frame on the interval while enabled; trigger tags 5 (enable/disable), 14 (make video) and 15 (file list) are wired. `signaling.send_file_list` emits the recovered `0x3f701c` envelope on the `file_list` event (field 1 = `"<page>;<total>\n<chunk>"` where `<chunk>` is one `<name>;<status>\n` per `.avi`, field 2 = HTTP token, field 3 = request_id when present, field 4 omitted); an empty list sends nothing. Tests: `tests/app/test_timelapse.py`, `tests/app/test_file_list.py`.
- **Live (Pi-side) 2026-09-19:** deployed via `deploy.sh` (overlay maintenance flow, services `active`); `/mnt/sdcard` is owned by the service user and writable; the deployed `timelapse.storage_status()` returns `(1, <total>, <free>, <used>, 'RW')` (present, RW); `smbd` active; `/c/info` 200 and `status` sent (387 bytes).
- **Live (app-side) confirmed 2026-09-19:** after the deploy, Connect shows timelapse **available**; the storage page displays SD size/used/free and the interval is configurable.
- **Live end-to-end test 2026-09-19:** enable/disable works via trigger tag 5 (`Trigger timelapse_enable`/`timelapse_disable`); frame capture works — 9 `frame_NNNNN.jpg` written to `/mnt/sdcard/timelapse` at the capture cadence (that run used the old `frame_NNNNN.jpg` naming, since superseded by `timelapse_<HH-MM-SS-mmm>.jpg`). Changing the app's interval sent `configuration {2: 30}` (previously ignored); now wired to `state.timelapse_interval` via the recovered `set_timelaps_interval` mapping (GAP-CONFIG-01). **Redeployed and re-verified:** the log shows `Config: timelapse_interval → 30s` and 7 frames landed exactly **35 s apart** (30 s interval + ~5 s capture), proving the interval now takes effect live.
- **Limitation (2026-09-19) [superseded by GAP-PERSIST-01]:** `/mnt/sdcard` was a plain directory on the read-only overlay root, so recordings lived in the tmpfs upper layer and were **lost on reboot** (the maintenance reboot wiped the first test's 9 frames). **Resolved 2026-09-20:** a 4 GB ext4 `PERSIST` partition at `/data` is bind-mounted to `/mnt/sdcard`, and frames + `.avi` + `.timelapse_videos.csv` were live-verified to survive a real reboot (GAP-PERSIST-01). Frames remain retrievable over SMB.
- **Pi-only extension (GPIO trigger and per-print sessions):** the owner requested a frame
  trigger from the Prusa GPIO Hackerboard. It is **not** a firmware behaviour: Connect still sees
  only the enable flag and the interval, and the wire protocol is unchanged. Frames can be taken
  per layer pulse (`gpio_trigger.py`), and an optional recording pin groups them into
  `session_<date>-<time>` folders whose built video `session_<date>-<time>.avi` sits in the
  timelapse root, so the firmware-shaped `file_list` (root `.avi` files only) lists it.
  **[assumption]** Connect accepts the `session_*.avi` names in its list; verify in the Connect UI.
  The GPIO chip label and edge behaviour are also unverified on hardware.
- **Firmware artifact naming (recovered 2026-09-19, decompile):** individual frames are JPEGs written by `FUN_000ac5d4` (`std::ofstream`, log `Saved jpeg frame to file: %s`) as **`timelapse_<HH-MM-SS-mmm>.jpg`** (time format `%02d-%02d-%02d-%03d` from `FUN_000ac2e8`), under `/mnt/sdcard/timelapse/`; the service path component at object `+0x10` (also reported as `timelapse_status` tag 4) is prepended when non-empty (`FUN_000ac4b4`). The assembled video is **`.avi`** with a hidden **`.timelapse_videos.csv`** index (`FUN_000ac134` writes `<name>:<status>` rows — `D`/`E`/`P` from `FUN_000aee3c`); the file-list composer `FUN_000ad7ec` enumerates the **`*.avi`** files (`FUN_000ac934`) and emits one **`<name>;<status>\n`** entry each, defaulting a name absent from the index to `'U'` (0x55; log `Timelapse videos with status: %s`). **Matched locally 2026-09-19:** `timelapse.frame_name`/`save_frame` use the exact timestamped `.jpg` name (collisions bump the millisecond), `build_avi` writes an MJPEG-in-AVI `timelapse_<HH-MM-SS-mmm>.avi` and appends a `<name>:D` (or `:E` on failure) row, and `read_video_index`/`file_list_entries` compose the `<name>;<status>` listing; `FUN_000a1fa8`'s `0x3f701c` list envelope is annotated in `protocol.md` and sent by `signaling.send_file_list`.
- **Live (artifact naming + interval) 2026-09-19:** redeployed (`5ad9263`) and re-tested. The app set the interval to 15 s then 10 s (`configuration {2: 15}` / `{2: 10}` → `Config: timelapse_interval → 15s/10s`), enabled timelapse via trigger tag 5, and 8 frames recorded as **`timelapse_<HH-MM-SS-mmm>.jpg`** (~15 s apart = 10 s interval + ~5 s capture), then disabled. Running `build_avi` on those 8 real frames produced `timelapse_<HH-MM-SS-mmm>.avi` (2.8 MB) that `file(1)` identifies as *"RIFF ... AVI, 1920 x 1080, 10.00 fps, video: Motion JPEG"*, with a `<name>:D` `.timelapse_videos.csv` row and `file_list_entries` = `<name>;D\n`. Also observed: `configuration` `tag3.10` carries the **active print-job name** (e.g. `Voron_Design_Cube_v8_0_4n_0_2mm_PC_COREONE_54m_b`; `unknown_timelapse` when idle).
- **Live status:** this Connect app version exposes **no make-video button and no file-list/recordings view**, so `tag 14`/`tag 15` and the `file_list` envelope **cannot be exercised through the app UI**. They are implemented and unit-tested (the make path is verified directly on the Pi via `build_avi`/`file(1)`); the `file_list` sender remains not app-exercised. Progress `client_trigger` remains under `GAP-SIO-01`.
- **Acceptance:** every advertised timelapse action has a schema fixture and either a working result (enable/disable, make, file list) or an explicit unsupported response.
- **Remaining:** progress `client_trigger` (under `GAP-SIO-01`); the `file_list` envelope has no app UI to exercise it (only a Socket.IO client replay could).
- **Firmware behavior:** controls enable/interval/FPS, stores frames, creates MJPEG output, indexes
  files, returns file list/status, and emits progress/error `client_trigger` messages. **[confirmed]**
- **Current behavior (historical):** previously advertised all four timelapse features and answered
  file-list requests with an untyped empty protobuf message; now implemented locally (see above).
- **Connect impact (historical):** exposed list/progress flows did not work; the list flow is now
  implemented locally and pending live verification, while progress `client_trigger` remains open.
- **Implementation choice:** implement a Pi storage-backed equivalent (chosen); an empty list sends
  no message, and the exact firmware `0x3f701c` list envelope is now encoded rather than an untyped
  empty message.
- **Acceptance:** every advertised timelapse action has a schema fixture and either a working result
  or an explicit firmware-shaped unsupported/error response.
- **Code:** [`main.py`](../../app/main.py)

### GAP-DEVICE-03 — Host stability: unexpected reboot + thermal throttling

- [~] **P2 · Investigated 2026-09-19; forensics + journal-flood mitigations deployed, hardware action open**
- **Reboot (~21:07 2026-09-19, legacy developer deployment):** cause **undeterminable** — volatile
  journald, `/tmp`, wtmp and cloud-init evidence was discarded; there is no RTC (boot clocks
  jumped +51/+88 min on NTP sync), and no
  `pstore`/ramoops. Current-boot `dmesg` shows no panic/oops, no under-voltage, no OOM, no mmc/ext4
  errors. Most likely an **external power cut** (the Pi is printer-powered by design per
  `CLAUDE.md`) or a **1-minute hardware-watchdog reset** (`RuntimeWatchdogUSec=1min`, `get_rsts=20`,
  non-default but not authoritatively decodable).
- **Throttling (high confidence, thermal):** `get_throttled` = `0x60002` → bit1 ARM frequency capped
  **now**, bit17 freq-cap latched, bit18 hard-throttle latched; **bits 0/16 (under-voltage) clear**.
  Temperature 81–84 °C continuously with **no cooling device**, vs a 85 °C `temp_limit`; real ARM
  clock 672–941 MHz against a 1 GHz request. Cause: 1080p30 H.264 (`rpicam-source`) + `main.py` +
  `stream_mux` on a passively cooled Pi Zero 2 W. Not destructive, but degrades streaming jitter.
- **Aggravators:** `systemd-remount-fs.service` fails in a loop (`overlay: No changes allowed in
  reconfigure`), which also fails the zram/swapfile units → **zero swap** on a 415 MB device.
- **Mitigations deployed (`bootlog.sh` + `systemd/bootlog.service`, `rpicam-source.service`):**
  a oneshot writes `date`/uptime/`get_rsts`/`get_throttled`/temp/dmesg to the real vfat
  `/boot/firmware/bootlog.txt` each boot (survives reboots); `rpicam-vid -v 0` stops the ~30 lines/s
  frame stats that evicted `pibuddycam` logs from the volatile journal within minutes.
- **Open (hardware/ops):** fit a heatsink/fan or reduce the stream (720p/15 fps); repair the failing
  swap/remount-fs units; keep an off-box logger (netconsole/serial) if a reboot must be captured
  precisely. Confirm the next reboot from `/boot/firmware/bootlog.txt`.

## P3 — parity and diagnostics

### GAP-STATUS-03 — Verify remaining status subfields against fixtures

- [~] **P3 · Nested schema recovered from the descriptor and three mismatches fixed**
- **Implementation:** dumped `CameraInfoMessage` @ `0x3f6e98` and its submessages and fixed three wire-type/tag mismatches in `status.py`: `timelapse_status` (descriptor `0x3f753c`) tag6 = fixed32/float, tag7 = uvarint (were swapped); `extended_status.4` (descriptor `0x3f72b0`) tags 1-4 uvarint + tag 5 string — the block is the SD storage block, not a model string (the earlier "model on tag 5" reading was corrected in GAP-TIMELAPSE-01); `extended_status.6` (descriptor `0x3f7278`) RTSP URL on tag 3 (was 4). Tests: `tests/app/test_status_schema.py`. Live-verified: `/c/info` 200, snapshots 200, status sent with no errors.
- **Still open:** a golden fixture captured from a genuine 3.1.6 status (not available offline) and the semantic annotation of every remaining nested tag.
- **Firmware behavior:** sends the recovered top-level fields 2, 3, 4, 5, 8, 9, 10 conditionally,
  and 11, with many nested values obtained from actual services/configuration. **[confirmed]**
- **Current behavior:** top-level structure is substantially reconstructed, but several nested values
  are inferred/defaulted, an empty second network submessage is emitted, and endpoint/timezone fields
  have not been compared byte-for-byte with a current real-camera payload.
- **Connect impact:** probably secondary because authentication and `/c/info` already work, but hidden
  classification or UI logic could inspect these values.
- **Implementation:** create canonical decoded fixtures from the decompiled assignment paths and, if
  obtainable, a redacted real-camera status capture; document every intentionally different hardware
  telemetry value.
- **Acceptance:** schema/field-presence comparison has no unexplained field, type, or semantic
  differences.
- **Code:** [`signaling.py`](../../app/signaling.py)

### GAP-NETWORK-01 — Verify Wi-Fi signal conversion and secondary network block

- [~] **P3 · Signal conversion fixed to the firmware formula**
- **Network:** recovered `FUN_00097b38`: the input is the **RSSI (dBm)** `level` column of `/proc/net/wireless`, converted as `rssi==0 || rssi<-99 -> 0`, `rssi>-51 -> 100`, else `(rssi+100)*2`. `network.py` implements `rssi_to_quality`/`parse_wireless_level`/`signal_quality_from_wireless`, and `signaling._signal_quality` now uses it instead of the previous linear 0–70 link-quality mapping. Tests: `tests/app/test_network.py`.
- **Still open:** a golden live status capture to confirm the exact value against a genuine camera; the empty secondary network submessage stays omitted (fixed earlier).
- **Firmware behavior:** reports current WLAN identity/address/signal and has descriptor space for
  additional network state. **[confirmed]**
- **Current behavior (implemented):** `network.signal_quality_from_wireless` uses the recovered
  firmware formula on the RSSI (dBm) level column, and the empty secondary network submessage is
  omitted.
- **Connect impact (resolved):** signal telemetry matches the firmware conversion; no spurious empty
  submessage.
- **Before (superseded):** mapped `/proc/net/wireless` quality linearly from 0–70 to 0–100 and emitted
  an empty field-2 network submessage.
- **Implementation:** trace the firmware signal conversion and the field-2 presence condition; omit
  the empty optional submessage if firmware omits it.
- **Acceptance:** field presence and signal values match a controlled RSSI/quality fixture.

### GAP-HTTP-03 — Reuse HTTP connections

- [~] **P3 · Implemented; live verification pending**
- **Firmware behavior:** long-running services reuse their HTTP/curl context and maintain service
  state. **[confirmed at architecture level]**
- **Current behavior (implemented):** `upload.make_session()` builds one bounded-timeout
  `aiohttp.ClientSession` that `main` creates once, passes to every request path, and closes in a
  `finally`; the request functions never build their own.
- **Connect impact (resolved):** connections are reused; live reuse/close-recovery still needs the
  Pi.
- **Before (superseded):** created a new `aiohttp.ClientSession` for each info and snapshot request,
  causing extra TLS handshakes and connection churn.
- **Implementation:** own one session for the application lifetime with bounded timeouts and clean
  shutdown.
- **Acceptance:** repeated uploads reuse connections and recover after server-side close.
- **Implementation:** `upload.make_session()` builds the single
  `aiohttp.ClientSession` with bounded `ClientTimeout`; `main` creates it once and passes it to
  `upload_snapshot`, `upload_info`, the info service loop, snapshots, and OTA, closing it in a
  `finally`. `upload_snapshot`/`upload_info` never construct a session. Tests:
  `test_capture_http.py::SessionReuseTests` (AST assertion). Live connection-reuse/close
  recovery still requires the Pi.

### GAP-SIO-01 — Firmware-style error and progress messages

- [~] **P3 · Tag semantics recovered; emission deferred (string-tag split `[assumption]` + no consumer)**
- **Partial (offline):** the generic sender is `FUN_000a2754`, confirmed by the emitted event name `client_trigger` and the 6-field descriptor `0x3f6f58`. It populates only a subset of the message: a constant (`DAT_000a2ab4`), the result of `FUN_0008286c` (token-shaped), and the result of `FUN_0009f69c(param_1)` (request-id-shaped); the ack callback is `DAT_000a2ae0/ae4`. Which tag carries the result/error/progress code is not yet mapped — do not guess.
- **RE 2026-09-20 (Wave 1, direct 3.1.6 decompiler/ELF) [confirmed]:** descriptor `0x3f6f58`
  = 6 fields (strings 1/2/4, uvarints 3/5/6). **The emit mechanism is a Socket.IO EVENT
  (`client_trigger`), not an ack**; the ack callbacks are the server's receipt ack. Nine sender
  wrappers were recovered (error codes: SD not mounted / SD RO / low space / timelapse SD not
  mounted / RTSP start failed; FW upgrade progress 1..6; timelapse-video-make status 1
  IN_PROGRESS / 2 FINISHED). **Which tag carries code vs progress vs message is unresolved**
  `[assumption]`; do not implement a wire format yet.
- **RE 2026-09-20 (Wave 2, direct 3.1.6 decompiler/ELF) [confirmed]:** tag semantics recovered —
  **tag5 uvarint = result/error code** (`FUN_000a2d68`=2, `FUN_000a2ca0`=5), **tag6 uvarint =
  upgrade/progress value** (`FUN_000a2e3c`=1, `FUN_000a2fec`=2, `FUN_000a2f80`=3), **tag3 uvarint =
  timelapse-video-make status** (`FUN_000a2bbc`=1 IN_PROGRESS / 2 FINISHED); tag1 = constant,
  tag4 = `FUN_0008286c()` (token-shaped), tag2 = a shared global. The message/subtype split among
  the string tags 1/2/4 is `[assumption]`. **Emission stays deferred** — the string tags are
  unresolved and this Connect version exposes no make-video/file-list UI to consume the event.
- **Firmware behavior:** uses `client_trigger` variants for generic result/error codes, OTA progress,
  and timelapse-video progress. **[confirmed]**
- **Current behavior:** never emits `client_trigger`.
- **Connect impact:** Connect receives no structured outcome for failed or asynchronous commands.
- **Implementation:** recover subtype fields/enums and centralize success/error/progress emission.
- **Acceptance:** each implemented asynchronous/control command emits the expected lifecycle.


## Confirmed matches — do not re-open without contrary evidence

- [x] Connect, not firmware, creates the random 20-character alphanumeric pairing token. Firmware
  consumes and persists it unchanged.
- [x] Camera authentication fields: fingerprint field 1, token field 2.
- [x] Fingerprint normal path: lowercase MD5 hex of uppercase colon-separated `wlan0` MAC text.
- [x] Snapshot and info paths: `PUT /c/snapshot` and `PUT /c/info`.
- [x] Snapshot/info identity headers: short `Token` and `Fingerprint` names.
- [x] OTA path and prefixed `X-Camera-*` header names.
- [x] `/c/info` nesting: camera attributes under `config`, current resolution under
  `options.available_resolutions`, `capabilities` and `features` as arrays.
- [x] `/c/info` currently sends one available-resolution entry, matching the recovered firmware build
  path rather than the older three-entry assumption.
- [x] Model/manufacturer/firmware/protocol constants: `Buddy3D-C1`, `Niceboy`, `3.1.6`, `4.4`.
- [x] Motorless feature list contents and ordering. `RotationX`/`RotationZ` are conditional in
  firmware and correctly absent for the impersonated C1.
- [x] `protobuf_version`: token field 1, version `4.4` field 2, optional request ID field 3.
- [x] `features` field 7 is the MD5 of the bracket-wrapped feature JSON, as confirmed by the firmware
  hash-building path and prior real-camera capture. It is **not** a second protocol-version field;
  stale documentation saying otherwise must not drive implementation.
- [x] Camera-side WebRTC numeric enum: `1=request`, `2=answer`, `3=offer`, `4=candidate`.
- [x] Inbound WebRTC offer uses client ID field 3 and SDP field 4.
- [x] Outbound WebRTC answer/candidate uses request ID field 1, numeric type field 2, payload field 3.
- [x] Quality dimensions: protobuf enum `1=640×480`, `2=1280×720`, `3=1920×1080`.
- [x] Raw quality command mapping: firmware uses `5=SD`, `6=HD`, `7=FHD`; implemented in `state.py`/`quality_control.py` (`d1ec311`).
- [x] Trigger message descriptor `0x3f6f14` (13 fields) and the `FUN_000a963c` per-field dispatch, recovered 2026-09-18 and implemented in `trigger.py` (`9291968`).
- [x] `ClientTrigger` descriptor `0x3f6f58` types (strings 1/2/4, uvarints 3/5/6) and Wave-2 tag
  semantics (tag5 = result/error code, tag6 = upgrade/progress value, tag3 = timelapse-video-make
  status); the message/subtype split among the string tags 1/2/4 is still `[assumption]`.
- [x] Inbound WebRTC field names (Wave 2): tag5 = msg type, tag7 = client type, tag2 = client id,
  tag4.f1 = SDP, tag8.2 = transport policy, tag8.3 = TTL; tag9 submessage (`0x3f7624`) =
  tag9.1 quality / tag9.2 FPS / tag9.3 plan / tag9.4 TTL / tag9.5 scope; "VideoCfg" = 1.
- [x] TURN/scoped-quality lock: while a relay (TURN) viewer candidate is online, the config quality
  path rejects a raise; implemented via `state.turn_online` + `quality.quality_change_allowed`.
- [x] Firmware RTSP default port is `554` (`FUN_000b04d4`, literal `0x22a`); the Pi's `8554` is an
  intentional privileged-port exception.
- [x] Connect accepts a video-only WebRTC offer (stream plays live in app and browser); audio is
  optional/not required.
- [x] Timelapse file-list descriptor `0x3f701c`: event `file_list`, field 1 = `"<page>;<total>\n<chunk>"`, field 2 = HTTP token, field 3 = request_id (optional), field 4 never set; an empty list sends nothing.
- [x] H.264 intent: constrained baseline, level 3.1, packetization mode 1.
- [x] Default snapshot interval is 10 seconds.
- [x] `set_rtsp_server_mode` direct values handled as `1=disabled`, `2=enabled`.

## Verification work required

The existing automated tests cover identity derivation and the basic camera-side WebRTC envelope
only. Add these suites as gaps are closed:

- [ ] Canonical protobuf fixtures for authentication, status, version, features, trigger,
  configuration, `client_trigger`, WebRTC ICE config, and connection info.
- [ ] Golden `/c/info` fixture and consistency checks against shared runtime state.
- [ ] HTTP mock tests for success, blocking/throttling, redirects, timeout, and retry.
- [ ] Stateful command tests covering trigger dispatch, interval, snapshot enable, RTSP, WebRTC,
  quality change/save, and unsupported-feature responses.
- [ ] WebRTC integration test using a local signaling/ICE harness, including forced TURN relay.
- [ ] Single-camera-owner test with concurrent snapshot, RTSP, and WebRTC consumers.
- [ ] Redacted live comparison against a genuine 3.1.6 camera if access becomes available.
- [ ] Fresh-token live test of the exact MAC-derived fingerprint and subsequent registry/viewer gate.

## Suggested implementation order

1. Recover typed trigger and configuration descriptors; add golden fixtures.
2. Introduce one shared runtime/configuration state and make status truthful.
3. Implement trigger actions for snapshot control, status/features/version, RTSP, and quality.
4. Feed WebRTC from the shared H.264 source and implement reliable teardown.
5. Parse/apply ICE/TURN, relay policy, TTL, FPS, plan, scope, and scoped quality.
6. Emit WebRTC connection information and `client_trigger` result/error messages.
7. Decide capability policy for OTA, timelapse, reboot, IR, speaker, fan, and MicroSD.
8. Align remaining parameters: raw quality mapping/persistence, RTSP port, JPEG quality, HTTP
   `Expect`, timezone, and network telemetry.
9. Migrate the live deployment to the firmware-derived fingerprint with a fresh token and repeat the
   camera registry/viewer-auth test.

## Closed gaps (evidence archived)

- [x] **GAP-WEBRTC-01**: Consume Connect-provided ICE server configuration. P0 · Closed 2026-09-20 (live-verified): Connect ICE config consumed; media plays live in app and browser
- [x] **GAP-WEBRTC-02**: Share the existing camera encoder instead of opening libcamera twice. P0 · Closed 2026-09-20 (live-verified): WebRTC consumes the shared `stream_mux` source
- [x] **GAP-WEBRTC-03**: Implement session lifecycle and teardown. P0 · Stream + teardown live-verified (app and browser) 2026-09-19
- [x] **GAP-STATUS-01**: Report actual dynamic camera state. P1 · Closed 2026-09-20 (live-verified): shared state drives quality/name/interval/WebRTC/RTSP
- [x] **GAP-SNAPSHOT-01**: Apply snapshot upload interval changes. P1 · Implemented and unit-tested (d1ec311); live cadence verified (20 s, GAP-CONFIG-01)
- [x] **GAP-INFO-01**: Refresh and retry `/c/info`. P1 · Closed 2026-09-20 (live-verified): periodic `/c/info` refresh
- [x] **GAP-CAP-01**: Stop overpromising unsupported features, or implement their wire behavior. P1 · Resolved 2026-09-19: prune the hardware-absent features
- [x] **GAP-QUALITY-01**: Correct the raw quality-byte mapping. P2 · Closed 2026-09-20 (live-verified): raw quality mapping `{5:1,6:2,7:3}` correct
- [x] **GAP-QUALITY-03**: Initialize and publish persisted quality. P2 · Closed 2026-09-20 (live-verified): persisted quality restored after a real reboot
- [x] **GAP-RTSP-01**: Align RTSP port and advertised URL. P2 · Closed 2026-09-20 (Wave 2): firmware default confirmed 554; 8554 is an intentional Pi exception
- [x] **GAP-HTTP-01**: Snapshot `Expect: 100-continue`. P2 · Closed 2026-09-20 (live-verified): HTTP handshake succeeds live
- [x] **GAP-HTTP-02**: Handle HTTP result classes and throttling. P2 · Closed 2026-09-20 (live-verified): identity headers accepted live
- [x] **GAP-INFO-02**: Keep `/c/info` dynamic values consistent. P2 · Closed 2026-09-20 (live-verified): dynamic `/c/info` values
- [x] **GAP-CONTROL-01**: Apply and publish camera-name changes. P2 · Closed 2026-09-20 (live-verified): camera-name durability
- [x] **GAP-OTA-01**: Implement truthful OTA behavior. P2 · Closed 2026-09-20: resolved by owner decision (truthful decline), not live-tested
- [x] **GAP-PERSIST-01**: Persist settings + timelapse storage on /data. P2 · Live-verified 2026-09-20: settings + timelapse store survive a reboot
- [x] **GAP-DEVICE-01**: Reboot command behavior. P2 · Closed 2026-09-20: live-verified — Connect's "Restart Camera" rebooted the Pi
- [x] **GAP-DEVICE-02**: IR, speaker, fan and MicroSD feature truthfulness. P2 · Closed 2026-09-20 (Wave 2): truthful `MicroSd` lives in `extended_status.4`; `camera_status` fields are pruned/moot
- [x] **GAP-WEBRTC-07**: Decide audio-track compatibility. P2 · Closed 2026-09-20 (Wave 2): Connect accepts a video-only offer; audio is optional
- [x] **GAP-IDENTITY-01**: Firmware fallback when `wlan0` MAC retrieval fails. P3 · Implemented and live-verified (config precedence)
- [x] **GAP-IDENTITY-02**: Deploy exact fingerprint only with a fresh token. P3 · Resolved 2026-09-20: already using the `wlan0`-MAC-derived fingerprint (token bound; live ACK 0)
- [x] **GAP-IDENTITY-03**: Track the physical Wi-Fi MAC/OUI difference. P3 · Closed 2026-09-20: documented per-device difference, not a defect; OUI not inspected and the registry-gate theory is superseded
- [x] **GAP-STATUS-04**: Timezone representation. P3 · Implemented and live-verified
