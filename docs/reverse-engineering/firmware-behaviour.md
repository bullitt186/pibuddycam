# Firmware behaviour recovered from 3.1.6

The decompiler evidence behind the wire contract. [protocol.md](protocol.md) states *what* goes over
the wire. This page records *where in the firmware* each rule was recovered and the exact branch
behaviour an implementation has to reproduce. Function names refer to the local 3.1.6 export
described in [methods](methods.md).


The complete 3.1.6 export is expected at:

```text
$FW_WORKDIR/decompiled-3.1.6-full/functions/
```

References below use `file:line` from that export. Function names are still Ghidra-generated, so
the VMA in the filename is the stable identifier. The line numbers refer to the checked export from
2026-09-18; if it is regenerated, search for the VMA/function name and the shown branch/constants.

Do not implement a field or enum from an older prose note when it conflicts with this direct 3.1.6
control flow. In particular, the direct quality mapping was rechecked while preparing this tracker
and corrects a stale mapping in older notes.

### Decompiled source index

| Evidence ID | Firmware behavior | 3.1.6 decompiled source |
|---|---|---|
| `FW-ID-MAC` | Read interface MAC via ioctl and format uppercase colon-separated text | `00097e78__FUN_00097e78.c:23-54` |
| `FW-ID-SEED` | Request `wlan0` identity; random ten-character fallback | `00096cd8__FUN_00096cd8.c:17-76` |
| `FW-ID-MD5` | MD5 input and lowercase 32-character hex output | `00097a4c__FUN_00097a4c.c:24-49` |
| `FW-INFO-BUILD` | Build `/c/info`, including current resolution/options/features and HTTP request | `00062d74__FUN_00062d74.c:64-329` |
| `FW-INFO-LOOP` | Dirty flag, retry countdown, one-second service loop | `00063bfc__FUN_00063bfc.c:23-94` |
| `FW-SNAPSHOT` | Capture JPEG, derive fingerprint, construct/upload HTTP request | `0005f42c__FUN_0005f42c.c:31-128` |
| `FW-CONFIG` | Parse and dispatch QR/configuration fields | `0006cf34__FUN_0006cf34.c:49-310` |
| `FW-QUALITY-DIRECT` | Raw change-quality event and optional persistence | `00072f08__FUN_00072f08.c:16-115` |
| `FW-QUALITY-DIMS` | Internal quality value to dimensions | `0007d7c4__FUN_0007d7c4.c:10-28` |
| `FW-QUALITY-PB` | Protobuf quality enum to internal raw value | `000a76c8__FUN_000a76c8.c:16-79` |
| `FW-QUALITY-STRING` | Protobuf quality enum to SD/HD/FHD string | `000a11f4__FUN_000a11f4.c:12-25` |
| `FW-AUTH` | Build and emit two-field camera authentication | `000a3058__FUN_000a3058.c:39-118` |
| `FW-PB-VERSION` | Build version message and conditional request correlation | `000a3570__FUN_000a3570.c:43-130` |
| `FW-STATUS` | Build the complete 0x1d0-byte status struct | `000a1394__FUN_000a1394.c:133-499` |
| `FW-FEATURES` | Build/hash/encode supported-feature message | `000a8ed0__FUN_000a8ed0.c:76-282` |
| `FW-TRIGGER-STRINGS` | Trigger action names referenced by the dispatcher | `$FW_WORKDIR/cam-3.1.6/lp_app.strings:11249,11534-11535,11896,13934,14127,15086,15152,15309-15310` |
| `FW-TIMELAPSE-SEND` | Build, fragment, encode, and emit timelapse file-list response | `000a1fa8__FUN_000a1fa8.c:52-192` |
| `FW-TIMELAPSE-REGISTER` | Register timelapse-related event callbacks | `000a5208__FUN_000a5208.c:26-558` |
| `FW-WEBRTC-SEND` | Encode and emit outgoing WebRTC answer/candidate | `000a3e90__FUN_000a3e90.c:5-166` |
| `FW-WEBRTC-TYPE` | Numeric message-type conversion | `000b6d9c__FUN_000b6d9c.c:13-46` |
| `FW-WEBRTC-CANDIDATE` | Translate local ICE candidate and call WebRTC sender | `000b75e0__FUN_000b75e0.c:29-60` |
| `FW-WEBRTC-MODE` | Apply enable/disable, start/stop service, persist mode | `000b94ac__FUN_000b94ac.c:16-52` |
| `FW-WEBRTC-GATE` | Reject disabled offers and enqueue enabled peer work | `000b996c__FUN_000b996c.c:39-107` |
| `FW-RTSP-INIT` | Load RTSP mode and register start/stop/mode callbacks | `000b0834__FUN_000b0834.c:24-125` |

### Exact recovered configuration dispatch

`FUN_0006cf34` is not a generic JSON handler. It constructs allowed-value/action tables and calls
the shared dispatcher `FUN_0006c0c8`; string pointers in its literal pool resolve to the following
rules. This is the minimum behavior the typed impersonator dispatcher must reproduce.

The names in the table are not guesses based on local-variable order: they were obtained by
resolving `DAT_0006d9f4..DAT_0006daac` through the ELF literal pool into `.rodata`, then checking
the resulting strings against `lp_app.strings`. The integer action values are the immediate values
written beside those string pointers in `FW-CONFIG`.

| Incoming field | Incoming value | Internal action/value | Direct evidence |
|---|---|---|---|
| `rtsp` | `on` | `set_rtsp_server_mode(2)` | `FW-CONFIG:75-107` |
| `rtsp` | `off` | `set_rtsp_server_mode(1)` | `FW-CONFIG:92-107` |
| `webrtc` | `on` | `set_webrtc_mode(1)`; also forces RTSP disabled through the paired rule | `FW-CONFIG:108-140` |
| `webrtc` | `off` | `set_webrtc_mode(0)` | `FW-CONFIG:125-140` |
| `video_quality` | `sd` | internal raw quality `5` | `FW-CONFIG:141-173` |
| `video_quality` | `hd` | internal raw quality `6` | `FW-CONFIG:141-173` |
| `video_quality` | `fhd` | internal raw quality `7` | `FW-CONFIG:141-173` |
| `start_fw_update` | `start` | firmware-update action | `FW-CONFIG:174-192` |
| `light_control` | `auto` | light mode `1` | `FW-CONFIG:193-228` |
| `light_control` | `night` | light mode `3` | `FW-CONFIG:193-228` |
| `light_control` | `day` | light mode `2` | `FW-CONFIG:193-228` |
| `camera_name` | non-empty string | call camera-name setter; empty value only logs warning | `FW-CONFIG:237-259` |
| `snapshot_interval` | integer `10..600` | call interval setter with seconds | `FW-CONFIG:260-277` |

The leading `code` handling rejects the special values `"42"` and `"66"` before normal dispatch:
`FW-CONFIG:49-74` and `FW-CONFIG:286-300`. The one-second waits after individual actions are visible
at `FW-CONFIG:102-105`, `135-138`, `168-171`, `187-190`, and `229-233`.

The exact on-wire numeric tags for every configuration field are **not yet documented with the same
confidence as the semantic dispatch table**. The implementer must dump the configuration nanopb
descriptor or use a redacted captured payload before replacing the current guessed tags. Do not
infer tags from the order of the table above.

### Exact recovered MAC/fingerprint path

`FUN_00096cd8` constructs the interface-name string `"wlan0"` and passes it to `FUN_00097e78`
(`FW-ID-SEED:17-23`). `FUN_00097e78` then:

1. Opens `socket(AF_INET=2, SOCK_STREAM=1, 0)` (`FW-ID-MAC:23`).
2. Copies at most 15 bytes of the interface name into `ifreq` (`38`).
3. Calls `ioctl(fd, 0x8927, &ifreq)` (`39`); `0x8927` is Linux `SIOCGIFHWADDR`.
4. Formats the six returned bytes with the literal
   `%02X:%02X:%02X:%02X:%02X:%02X` (`46-47`).
5. Returns an empty string on socket/ioctl failure (`24-44`).

Back in `FUN_00096cd8`, an empty MAC causes `FUN_000997f8(..., 10, 1)` to generate the ten-character
fallback (`FW-ID-SEED:25-56`). `FUN_00097a4c` hashes the complete seed and writes exactly 16 digest
bytes as two lowercase hex characters each: the 16-iteration loop and `snprintf(dst, 3, "%02x", b)`
are at `FW-ID-MD5:24-45` (the `%02x` literal is resolved from `DAT_00097b34`).

### Exact recovered video-quality mapping

There are three distinct representations; mixing them caused the existing handler bug:

| Representation | SD | HD | FHD | Evidence |
|---|---:|---:|---:|---|
| Protobuf quality enum | `1` | `2` | `3` | `FW-QUALITY-PB:16-69` |
| Internal/raw event byte | `5` | `6` | `7` | `FW-QUALITY-PB:22-69`, `FW-CONFIG:141-173` |
| Resolution | `640×480` | `1280×720` | `1920×1080` | `FW-QUALITY-DIMS:12-23` |

`FUN_00072f08` receives the raw byte at line 16, no-ops when it already equals the stored value at
lines 22-26, handles raw 6/7/5 at lines 32-109, calls the live resolution changer, and persists only
when `*param_3 != 0` at lines 46-51, 68-73, and 93-98. It updates the in-memory current value only
after a successful change at lines 54-57, 76-78, and 101-103.

Implementation translation must therefore be:

```text
raw byte 5 -> protobuf enum 1 -> SD  -> 640x480
raw byte 6 -> protobuf enum 2 -> HD  -> 1280x720
raw byte 7 -> protobuf enum 3 -> FHD -> 1920x1080
```

### Exact recovered WebRTC mode and offer gate

`FUN_000b94ac` implements `set_webrtc_mode`:

```text
requested = payload field/value byte
if runtime_status(+0x13e) == 0 and requested != 0:
    persist mode 1
    start WebRTC service
elif runtime_status(+0x13e) != 0 and requested == 0:
    persist mode 0
    stop WebRTC service
mode(+0x13d) = requested
persist mode again
```

Direct lines: `FW-WEBRTC-MODE:16-27`, `35-40`, and `49-52`.

`FUN_000b996c` then gates peer creation. At `FW-WEBRTC-GATE:39-48`, it returns failure when both
`mode(+0x13d)` and `runtime_status(+0x13e)` are zero or when the client/request pointer is null.
At lines 54-101 it copies request/client/session/ICE parameters into a 0x5c-byte work item and
queues it at singleton offset `+0x140`. The impersonator should not replace this with an always-on
status claim; mode, runtime state, and peer work are separate concepts.

### Recovered WebRTC message contract

The incoming camera-side message layout recovered from the parser/descriptor is:

```protobuf
message CameraWebRtcInbound {
  string request_id = 1;
  uint32 msg_type = 2;          // 3=offer, 4=candidate
  string client_id = 3;
  string sdp_or_candidate = 4;
  uint32 transport_policy = 5;
  uint32 ttl = 6;
  uint32 video_cfg = 7;
  uint32 plan = 8;
  string quality = 9;
  uint32 fps = 10;
  uint32 scope_or_ttl2 = 11;    // semantic name still needs final descriptor annotation
  IceConfig ice_config = 12;
}
```

Outgoing answer/candidate layout is different and already implemented correctly:

```protobuf
message CameraWebRtcOutbound {
  string request_id = 1;
  uint32 msg_type = 2;          // 2=answer, 4=candidate
  string payload = 3;
}
```

`FW-WEBRTC-SEND:105-107` encodes into a 3000-byte buffer and lines 120-160 emit the binary event.
`FW-WEBRTC-TYPE:23-44` explicitly accepts numeric types `2`, `4`, and `1`; other nonzero values take
the invalid branch. `FW-WEBRTC-CANDIDATE:29-52` extracts the candidate metadata and routes it through
the same sender.

ICE construction is additionally supported by these firmware strings and paths:

- `No ICE servers received, using default configuration`
- `Adding TURN: %s:%d User: %s (Type: %d)`
- transport policies `RELAY`, `ALL`, and default/all
- default STUN servers `stun.l.google.com:19302`, `stun1.l.google.com:3478`, and
  `stun2.l.google.com:5349`

The nested `IceConfig` descriptor is still a required recovery item. Do not invent its numeric
subfield tags from the log format; dump the descriptor before implementing `GAP-WEBRTC-01`.

### Recovered `/c/info` construction and retry behavior

`FUN_00062d74` performs the following in order:

1. Reads token and network identity; aborts the body-build path if either required string is empty
   (`FW-INFO-BUILD:64-88`).
2. Builds `config` fields and current resolution (`FW-INFO-BUILD:89-173`). Width and height are
   `0x780`/`0x438` = `1920`/`1080` in the shown FHD path (`131-143`).
3. Builds one current-resolution object and inserts it into `options.available_resolutions`
   (`174-204`). This is direct evidence for one entry, not the older three-entry assumption.
4. Builds `capabilities` (`205-212`).
5. Calls the feature-list builder, wraps/parses it as JSON, and inserts `features`
   (`213-227`).
6. Serializes the body (`228-231`), derives the fingerprint (`242-251`), creates the HTTP request
   and headers (`249-271`), and sends it.
7. Clears the dirty/retry flag only when the response begins with the success text checked at
   `292-295`; non-success is logged at `314-318`.

The service loop sleeps one second (`FW-INFO-LOOP:33-39`). When the dirty flag at `+0x0d` is set,
it calls `/c/info` when countdown `+0x44` reaches zero, then reloads the countdown to 10 if still
dirty (`52-61`). A separate snapshot timer is maintained at offsets `+0x04/+0x08` and invokes the
snapshot uploader at `70-75`.

The concrete Connect request target is:

```http
PUT /c/info
User-Agent: Buddy3D Camera
Token: <opaque Connect token>
Fingerprint: <lowercase MD5 hex>
Content-Type: application/json
```

```json
{
  "config": {
    "path": "private",
    "name": "Buddy3D Camera",
    "driver": "private",
    "model": "Buddy3D-C1",
    "firmware": "3.1.6",
    "manufacturer": "Niceboy",
    "trigger_scheme": "THIRTY_SEC",
    "resolution": {"width": 1920, "height": 1080},
    "network_info": {
      "wifi_mac": "AA:BB:CC:DD:EE:FF",
      "wifi_ipv4": "192.0.2.1",
      "wifi_ssid": "<SSID>"
    }
  },
  "options": {
    "available_resolutions": [
      {"width": 1920, "height": 1080}
    ]
  },
  "capabilities": ["trigger_scheme"],
  "features": ["<ordered feature names>"]
}
```

The resolution values above describe the shown FHD state; they must be sourced from current state,
not hardcoded independently in each message.

### Recovered status construction rules

`FUN_000a1394` zeroes the full `0x1d0`-byte nanopb structure before populating it
(`FW-STATUS:133-138`). This means optional-field presence is deliberate; an empty encoded submessage
is not automatically equivalent to an absent firmware field.

Key directly visible state translations:

| Status source | Firmware translation | Evidence |
|---|---|---|
| Unannotated three-state enum from `FUN_00071a4c` | internal `2→3`, `3→1`, `1→2`, else `0`; do not call this video quality until the nested descriptor is recovered | `FW-STATUS:155-168` |
| Boolean/service states | internal `0→2`, `1→1`, else `0` | `FW-STATUS:183-192`, `246-265`, `296-301`, `365-384` |
| Four-state enum | internal `0..3→1..4`, else `0` | `FW-STATUS:193-210` |
| RTSP mode/status/URL | reads two enum getters and builds URL from network identity | `FW-STATUS:331-362` |
| WebRTC mode/status | reads two WebRTC singleton getters and translates each to `2/1/0` | `FW-STATUS:363-385` |
| Current quality wrapper | uses configured getter or actual encoder channel getter | `FW-STATUS:386-399` |
| Conditional correlation field | populated only when `param_1[0x12] != 0`, value from `param_1[0x11]` | `FW-STATUS:413-418` |

The exact nested tag-to-local-variable mapping beyond the already recovered top-level layout must
come from the nanopb descriptor, not stack-variable order. Until a golden fixture exists, keep
`GAP-STATUS-03` open and do not describe inferred nested values as exact.

The recovered top-level status envelope is:

```protobuf
message CameraInfoMessage {
  // field 1 descriptor exists but SendCameraInfoMessage does not populate it
  TimelapseStatus timelapse_status = 2;
  CameraStatus camera_status = 3;
  NetworkInfo network_info = 4;
  ExtendedStatus extended_status = 5;
  // field 6 descriptor exists but is not populated here
  // field 7 descriptor exists but is not populated here
  string token = 8;
  SystemInfo system_info = 9;
  string correlation = 10;       // conditional; context-dependent SID/request value
  VideoQuality video_quality = 11; // nested field 1: 1=SD, 2=HD, 3=FHD
}
```

Presence flags observed in `FW-STATUS` correspond to top-level fields 2, 3, 4, 5, 9, and 11;
token field 8 is assigned from the HTTP configuration getter, and field 10 is conditional. The
impersonator must omit fields that firmware leaves absent rather than encoding arbitrary empty
submessages.

### Recovered outbound identity/metadata messages

These are the implementation targets established jointly by nanopb descriptors, sender assignment
paths, and the prior redacted real-camera capture:

```protobuf
message CameraAuthentication {
  string fingerprint = 1;
  string token = 2;
}

message ProtobufSchemaVersion {
  string token = 1;
  string version = 2;       // literal "4.4"
  string request_id = 3;    // present only for correlated reply
  // field 4 callback remains null / omitted
}

message CameraSupportedFeatures {
  // field 1 omitted
  string token = 2;
  string firmware = 3;      // "3.1.6"
  string hardware = 4;      // selected hardware-version string, e.g. "NB.1.1.0"
  string protocol = 5;      // "4.4"
  string features_json = 6; // bracket-wrapped ordered JSON list
  string features_md5 = 7;  // lowercase MD5 of exact field-6 bytes
  string request_id = 8;    // correlated reply when present
}
```

`FW-AUTH:46-58` obtains the two identity strings and passes the initialized structure to nanopb.
`FW-PB-VERSION:54-68` assigns the token/version values and conditionally sets the request field at
lines 60-65. `FW-FEATURES:106-123` obtains token/identity and constructs the exact bracket-wrapped
feature bytes; line 119 invokes the hash operation over those bytes. Lines 191-207 assign firmware,
hardware/model/protocol/hash sources before nanopb encoding.

The feature payload bytes whose MD5 is sent are exactly the no-space bracketed form:

```json
["SocketCom","UploadInterval","TimelapseEn","TimelapseInterval","TimelapseVideoMake","TimelapseFileList","VideoStream","RtspStream","GetSnapshot","IrMode","SpeakerVolume","WiFi","FwVer","HwVer","CameraName","MicroSd","FwUpdate","CameraReboot","McuTemp","VideoQuality","WebRtc","TurnVideoQualityChange","trigger_scheme","FanControl"]
```

`RotationX` and `RotationZ` are inserted only when motor hardware is detected; they are absent for
the motorless C1 target.

### Recovered timelapse file-list sending behavior

`FUN_000a1fa8` is the actual sender; `FUN_000a5208` primarily registers timelapse callbacks.
The sender:

1. Adds token and optional request correlation (`FW-TIMELAPSE-SEND:63-71`).
2. Retrieves/builds the CSV-derived file-list string and returns failure if empty (`72-84`).
3. Computes whether the complete response fits in 1024 bytes. One fragment is used below `0x401`;
   otherwise fragment count is `(size >> 10) + 1` (`85-96`).
4. Prefixes each fragment with its one-based fragment number and total, takes a substring, and
   assigns it to the response structure (`97-120`).
5. Encodes each response into a `0x400`-byte nanopb buffer (`120-142`), emits it (`145-179`), then
   waits 50 ms before the next fragment (`184-186`).

The exact four-field descriptor annotations are now recovered: field 1 = the
`"<page>;<total>\n<chunk>"` fragment, field 2 = the HTTP token, field 3 = request_id (set only when
present), field 4 = never populated by this sender. An empty list emits no message at all (there is
no zero-entry encoding to send). **[confirmed]**

### Areas where decompilation still does not remove all ambiguity

These are explicit recovery prerequisites, not permission to guess:

| Area | Known exactly | Still required before implementation |
|---|---|---|
| Trigger dispatcher | **Recovered 2026-09-18:** descriptor `0x3f6f14` (13 fields, tags 1–5/8–15) and the `FUN_000a963c` per-field `== 1`/`== 2` dispatch; `trigger.py` implements it. **2026-09-20:** result event is the `client_trigger` EVENT with descriptor `0x3f6f58` (not an ack). **Wave 2:** tag5 = result/error code, tag6 = upgrade/progress value, tag3 = timelapse-video-make status | Tag 13 string semantics; multi-field processing order; the message/subtype split among the string tags 1/2/4 `[assumption]` |
| Configuration | **Resolved 2026-09-19: nested protobuf** (descriptor `0x3f73a4`, dispatcher `FUN_000a7940`); live-mapped `tag8.1` = video quality and top-level field 2 = `set_timelaps_interval` (`FUN_000a7940` logs `"Timelapse interval: %d seconds"`); the nlohmann JSON parser is only the QR/manual-config path. **2026-09-20:** `tag3` descriptor `0x3f6d2c` (11 fields) recovered. **Wave 2 confirmed:** `tag3.4` = `light_control`, `tag3.5` = `set_snapshot_upload_interval` (10..600), `tag3.10` = `set_printing_job_name`. `tag3.7` is plausibly `set_volume` (5..100) `[assumption]`. | `tag3.11/12` are **uvarints** — the "RTSP candidate" label is **refuted**; the RTSP mode is a small enum toggle elsewhere (`iStack_74` 1/2/3, exact tag `[assumption]`); a redacted golden capture |
| Status | Top-level fields, struct size, many getters/translations, request correlation, and now the nested descriptor tables (dumpable). `extended_status.4` is the truthful SD storage block; `camera_status` fields 1/3 are `ir_mode`/`speaker_volume` (features pruned) | Semantic tag-to-field annotation for every claimed nested value; `camera_status` fields 4-6 unresolved but moot (GAP-DEVICE-02 closed) |
| ICE config | The inbound `webrtc` message is 9 fields with nested submessages (`0x3f7680`, handler `FUN_000a43e0`), **not** the flat 12-field log string; tag4/tag8/tag9 offsets and sizes recovered. **Wave 2 name binding confirmed:** tag2 = client id, tag4.1 = SDP, tag5 = msg type, tag7 = client type, tag8.2 = transport policy, tag8.3 = TTL, tag9 submessage (`0x3f7624`, 5 uvarints) = tag9.1 quality / tag9.2 FPS / tag9.3 plan / tag9.4 TTL / tag9.5 scope; "VideoCfg" hardcoded to 1 | Enforcement only: transport policy/TTL/FPS/plan/scope are decoded but **not enforced** (Connect sends only a subset); do not guess the unobserved fields |
| Timelapse list | **Resolved 2026-09-19:** descriptor `0x3f701c` field 1 = `"<page>;<total>\n<chunk>"`, field 2 = HTTP token, field 3 = request_id (optional), field 4 never set; `FUN_000a1fa8` sender; `FUN_000ad7ec` composes `<name>;<status>\n` entries from the `*.avi` scan (`FUN_000ac934`) + `.timelapse_videos.csv` status (`FUN_000ac134`, default `U`) | None for the envelope; an empty list sends no message |
| `client_trigger` | Dedicated 6-field descriptor `0x3f6f58` (strings 1/2/4, uvarints 3/5/6); emitted as a Socket.IO **event**, ack callbacks are the server receipt ack; nine sender wrappers recovered. **Wave 2 confirmed:** tag5 = result/error code (`FUN_000a2d68`=2, `FUN_000a2ca0`=5), tag6 = upgrade/progress value (`FUN_000a2e3c`=1, `FUN_000a2fec`=2, `FUN_000a2f80`=3), tag3 = timelapse-video-make status (`FUN_000a2bbc`=1 IN_PROGRESS / 2 FINISHED); tag1 = constant, tag4 = `FUN_0008286c()` (token-shaped), tag2 = shared global | The message/subtype split among the string tags 1/2/4 is `[assumption]`; emission is **deferred** until those strings are pinned and a consumer exists (this Connect version exposes no make-video/file-list UI) |
| RTSP port | **Resolved Wave 2 [confirmed]:** the shipped firmware default is `554` (`FUN_000b04d4` logs `"RTSP server started on port %d"` with the literal `0x22a`=554). The Pi intentionally listens on `8554` (privileged-port avoidance) and advertises `rtsp://<ip>:8554/live`, which Connect consumes (the local stream works) | Documented intentional Pi exception; no firmware-parity change |
| WebRTC audio | **Resolved Wave 2 [confirmed]:** the Pi offers video-only and the stream plays live in both the app and the browser, so Connect accepts a video-only offer | Audio is optional/not required; video-only is intentional (GAP-WEBRTC-07 closed) |
| Signaling session lifecycle (residual long-run question) | **Updated 2026-09-25:** removing the unsolicited post-auth burst (`ba48dc8`, live-verified 2026-09-20) fixed the earlier immediate server close. A later full-image run exposed a separate startup recovery bug: awaiting disconnect inside the Socket.IO `connect` callback could strand the initial connection before the supervisor started, leaving snapshots healthy but all Socket.IO controls silent. `GAP-AUTH-01` now bounds the initial attempt, returns normally from failed auth callbacks, requires the auth-success flag in session health, and replaces unusable clients with exponential backoff (15s→120s). | Whether the service can still be closed by a server-side eligibility/session policy over a long run is **not proven**. **Ruled out:** registration/identity (current token independently ACKed `0`), protobuf field order, the SD-vs-image Socket.IO dependency delta, handshake URL, token `origin`, headers/transport, and stale-session resume. Any remaining close needs a fresh capture. |

### Gap-to-firmware cross-reference

Use this table to jump from a tracker item to the recovered implementation evidence. A row marked
`descriptor required` intentionally has no invented field map; recovering that descriptor is part of
closing the gap.

| Gap | Primary firmware evidence | Remaining ambiguity, if any |
|---|---|---|
| `GAP-TRIGGER-01` | `FW-TRIGGER-STRINGS`; recovered descriptor `0x3f6f14` in `trigger.py`; `client_trigger` string at `lp_app.strings:10503`; Wave-2 tag semantics (tag5 code / tag6 progress / tag3 timelapse status) | String-tag message split `[assumption]`; emission deferred until pinned and a consumer exists |
| `GAP-CONFIG-01` | `FW-CONFIG` and the exact dispatch table above; `tag3` descriptor `0x3f6d2c` (11 fields); Wave-2 confirmed `tag3.4`/`tag3.5`/`tag3.10` | `tag3.11/12` are uvarints — "RTSP candidate" **refuted**; `tag3.7` = `set_volume` `[assumption]`; RTSP mode enum `iStack_74` exact tag `[assumption]` |
| `GAP-WEBRTC-01` | WebRTC contract above; `FW-WEBRTC-GATE:54-101` copies ICE/session data | Nested `IceConfig` descriptor required |
| `GAP-WEBRTC-02` | `FW-SNAPSHOT:62-77`; `FW-WEBRTC-GATE:79-101` | Pi sharing architecture is implementation-specific |
| `GAP-WEBRTC-03` | `FW-WEBRTC-GATE`; peer queue at `101`; mode stop path `FW-WEBRTC-MODE:35-40` | Exact peer TTL worker should be traced while implementing |
| `GAP-WEBRTC-04` | `FW-WEBRTC-MODE`, exact pseudocode above | None for enable/disable behavior |
| `GAP-WEBRTC-05` | Descriptor `0x3f7680` (9 fields; handler `FUN_000a43e0`); tag9 submessage `0x3f7624` (5 uvarints); `FW-WEBRTC-GATE:54-101`; TURN lock `FUN_000a7940`->`FUN_000b5ad4`/`FUN_000b4f90` (flag at singleton+0x278), implemented via `state.turn_online` + `quality.quality_change_allowed` | Field names **confirmed** (tag5 msg type, tag7 client type, tag2 client id, tag4.1 SDP, tag8.2 transport policy, tag8.3 TTL, tag9.1-5 quality/FPS/plan/TTL/scope, VideoCfg=1). Transport policy/TTL/FPS/plan/scope are decoded but **not enforced** (Connect sends only a subset); only Connect-observed fields may be enforced |
| `GAP-WEBRTC-06` | Sender `FUN_000be3f8`; encoder `FUN_000be050`/`FUN_000bdd3c`; candidate translator `FUN_000b5098`; descriptor `0x3f65c8` | GStreamer selected-pair extraction is version-specific (best-effort, skips if unavailable) |
| `GAP-STATUS-01` | `FW-STATUS`, translation table above | Some nested tag annotations still require fixture |
| `GAP-STATUS-02` | `FW-STATUS:413-418`; `FW-PB-VERSION:60-65` | Initial SID versus requested correlation needs fixture |
| `GAP-SNAPSHOT-01` | `FW-CONFIG:260-277`; `FW-INFO-LOOP:64-94` | Exact timer scaling constant should be named, not guessed |
| `GAP-SNAPSHOT-02` | `FW-TRIGGER-STRINGS:11249,11534`; snapshot service loop `FW-INFO-LOOP:64-94`; recovered descriptor `0x3f6f14` tags 4/5 | Live cadence verification |
| `GAP-INFO-01` | `FW-INFO-BUILD`; `FW-INFO-LOOP:42-61` | None for retry/dirty behavior |
| `GAP-CAP-01` | `FW-FEATURES`; feature builder call `FW-INFO-BUILD:213-227` | Capability-removal effect needs live Connect test |
| `GAP-QUALITY-01` | `FW-QUALITY-PB`, `FW-QUALITY-DIRECT`, `FW-QUALITY-DIMS` | None; mapping is exact |
| `GAP-QUALITY-02` | Live-change branches `FW-QUALITY-DIRECT:32-109`; persistence flag branches at `46-51,68-73,93-98` | The flag is **per-payload** for `change_video_size`, not a registration-time constant; best hypothesis `save_video_size`=persist / `change_video_size`=live-only `[assumption]`; do not guess a flag |
| `GAP-QUALITY-03` | Current-value reads `FW-QUALITY-DIRECT:22-31`; status getter `FW-STATUS:386-399` | None for shared-state requirement |
| `GAP-RTSP-01` | `FW-RTSP-INIT:24-42`; status URL `FW-STATUS:331-362`; default `554` from `FUN_000b04d4` (`"RTSP server started on port %d"`, literal `0x22a`) | **Closed [x]:** 8554 is an intentional Pi exception (privileged-port avoidance), consumed by Connect |
| `GAP-RTSP-02` | `FW-RTSP-INIT`; direct/config action table in `FW-CONFIG` | Callback bodies are split by Ghidra and should be retyped |
| `GAP-SNAPSHOT-03` | Snapshot capture path `FW-SNAPSHOT:62-77`; libjpeg quality 95 trace in `journal/findings.md:893-899` | Reconfirm quality argument if snapshot backend is replaced |
| `GAP-SNAPSHOT-04` | Snapshot timer `FW-INFO-LOOP:64-94`; capture `FW-SNAPSHOT:62-77` | Concurrent Rockchip channels do not prescribe Pi architecture |
| `GAP-HTTP-01` | HTTP request/header build `FW-SNAPSHOT:99-116`; `Expect` string in `lp_app.strings:3993` | Exact HTTP-library automatic behavior may differ |
| `GAP-HTTP-02` | Response/service loop `FW-INFO-BUILD:292-318`, `FW-INFO-LOOP`; blocked-upload string `lp_app.strings:8304` | Exact retry policy for every status code needs call-path trace |
| `GAP-INFO-02` | Dynamic getters throughout `FW-INFO-BUILD:89-227` | Nested JSON key names are already fixed in `protocol.md` |
| `GAP-AUTH-01` | `FW-AUTH`; auth event string `lp_app.strings:10355` | ACK callback branch needs explicit decompile annotation |
| `GAP-CONTROL-01` | `FW-CONFIG:237-259`; status name getter in `FW-STATUS:235-244` | Persistence backend is Pi-specific |
| `GAP-OTA-01` | `FW-CONFIG:174-192`; `start_fw_update` at `lp_app.strings:15084`; OTA endpoint/response keys in `journal/findings.md:1173-1181` | Full OTA state machine still needs focused call-path annotation |
| `GAP-TIMELAPSE-01` | `FW-TIMELAPSE-SEND`, `FW-TIMELAPSE-REGISTER`; action strings `lp_app.strings:15309-15310` | None for the list envelope; make-video/list not yet exercised by the app |
| `GAP-DEVICE-01` | `reboot_device` at `lp_app.strings:14127`; trigger dispatcher recovery item | Trigger enum/result response required |
| `GAP-DEVICE-02` | `camera_status` descriptor `0x3f6cd0` (6 fields); `FW-CONFIG:193-228`; advertised list from `FW-FEATURES`; truthful `MicroSd` is in `extended_status.4` | **Closed [x]:** `camera_status` fields 1/3 are `ir_mode`/`speaker_volume` (features pruned); fields 4-6 unresolved but moot; no action |
| `GAP-WEBRTC-07` | Codec/SDP strings summarized in `journal/findings.md:1040-1057` | **Closed [x]:** the Pi's video-only offer plays live in the app and browser, so Connect accepts video-only; audio is optional/not required |
| `GAP-IDENTITY-01` | `FW-ID-MAC`, `FW-ID-SEED`, `FW-ID-MD5` | None for algorithm; persistence policy is Pi-specific |
| `GAP-IDENTITY-02` | `FUN_00096cd8` (generateFingerPrint), `FW-ID-MAC`/`FW-ID-SEED`/`FW-ID-MD5`; `/c/info` use at `FW-INFO-BUILD:242-251` | **Resolved 2026-09-20:** already `wlan0`-MAC-derived with the token bound (live ACK `0`, `/c/info` 200); the runbook in `next-steps.md` applies only if the fingerprint is ever changed |
| `GAP-IDENTITY-03` | MAC source from `FUN_00096cd8`/`FW-ID-MAC`; model table in `firmware-versions.md` | **Closed 2026-09-20:** firmware does not inspect the OUI; the Pi-vendor OUI is a documented per-device difference and the registry-gate theory is superseded |
| `GAP-STATUS-03` | Entire `FW-STATUS`; zero-init/presence rule at `133-138` | Nested descriptor fixture required |
| `GAP-STATUS-04` | Time/status getter block `FW-STATUS:271-301` | Exact reported string needs getter rename/fixture |
| `GAP-NETWORK-01` | Network getter block `FW-STATUS:214-234`; `/c/info` network getters `FW-INFO-BUILD:145-173` | Signal conversion helper needs focused trace |
| `GAP-HTTP-03` | Long-running service loop `FW-INFO-LOOP`; HTTP build/send paths in `FW-INFO-BUILD` and `FW-SNAPSHOT` | Connection reuse is partly inside the bundled HTTP library |
| `GAP-SIO-01` | `client_trigger` at `lp_app.strings:10503`; sender variants listed in `journal/findings.md:376-381`; Wave-2 tag semantics (tag5 code / tag6 progress / tag3 timelapse status) | String-tag message/subtype split `[assumption]`; emission deferred until pinned and a consumer exists |


## Firmware evidence anchors

The binary and decompilation remain outside this repository for copyright reasons. Reproduction and
address notes live in [`reverse-engineering.md`](methods.md), with the 3.1.6 delta in
[firmware versions](firmware-versions.md). Key recovered paths used by this tracker include:

| Behavior | Firmware 3.1.6 anchor |
|---|---|
| Camera status construction | `FW-STATUS` — `000a1394__FUN_000a1394.c:133-499` |
| Supported-feature construction/hash | `FW-FEATURES` — `000a8ed0__FUN_000a8ed0.c:76-282` |
| `/c/info` JSON/HTTP construction | `FW-INFO-BUILD` — `00062d74__FUN_00062d74.c:64-329` |
| `/c/info` dirty/retry loop | `FW-INFO-LOOP` — `00063bfc__FUN_00063bfc.c:23-94` |
| Quality apply/persistence flag | `FW-QUALITY-DIRECT` — `00072f08__FUN_00072f08.c:16-115` |
| Quality enum/raw conversion | `FW-QUALITY-PB` — `000a76c8__FUN_000a76c8.c:16-79` |
| Quality dimensions | `FW-QUALITY-DIMS` — `0007d7c4__FUN_0007d7c4.c:10-28` |
| Fingerprint seed/fallback | `FW-ID-SEED` — `00096cd8__FUN_00096cd8.c:17-76` |
| MAC retrieval/formatting | `FW-ID-MAC` — `00097e78__FUN_00097e78.c:23-54` |
| Fingerprint MD5/hex conversion | `FW-ID-MD5` — `00097a4c__FUN_00097a4c.c:24-49` |
| WebRTC answer/candidate sender | `FW-WEBRTC-SEND` — `000a3e90__FUN_000a3e90.c:5-166` |
| WebRTC offer gate/session enqueue | `FW-WEBRTC-GATE` — `000b996c__FUN_000b996c.c:39-107` |
| WebRTC enable/disable behavior | `FW-WEBRTC-MODE` — `000b94ac__FUN_000b94ac.c:16-52` |
| WebRTC inbound field names / tag9 policy group | handler `FUN_000a43e0`; descriptor `0x3f7680`; tag9 submessage descriptor `0x3f7624` (5 uvarints) |
| TURN/scoped-quality lock | `FUN_000a7940` -> `FUN_000b5ad4` -> `FUN_000b4f90` (flag at `singleton+0x278`), cap check `FUN_000b51e8`, apply `FUN_000b4e98`; log `DAT_000a89c4` |
| `client_trigger` result/progress/status tags | tag5 code `FUN_000a2d68`/`FUN_000a2ca0`; tag6 progress `FUN_000a2e3c`/`FUN_000a2fec`/`FUN_000a2f80`; tag3 timelapse status `FUN_000a2bbc`; sender `FUN_000a2754` |
| RTSP default port | `FUN_000b04d4` logs `"RTSP server started on port %d"` with literal `0x22a` = 554 |

When closing an item, record the implementing commit, tests, live verification date if applicable,
and whether the conclusion is **confirmed** or remains an **assumption**.
