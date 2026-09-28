# Reverse engineering

How the genuine Prusa Buddy3D camera talks to Prusa Connect, recovered from its firmware (through
`3.1.6`) and from live traffic. This is the knowledge PiBuddyCam implements. You don't need it to
*use* PiBuddyCam, only to change or verify its protocol behaviour.

| Page | What it answers |
|---|---|
| [protocol.md](protocol.md) | **The wire protocol** (source of truth): registration, HTTP uploads, Socket.IO events, protobuf messages, WebRTC |
| [firmware-behaviour.md](firmware-behaviour.md) | Where in the firmware each rule was recovered, and the exact branch behaviour to reproduce |
| [gap-tracker.md](gap-tracker.md) | Remaining differences between the firmware and PiBuddyCam: the protocol work queue |
| [pairing-qr.md](pairing-qr.md) | Schema of the "Add WiFi Camera" pairing QR |
| [dead-ends.md](dead-ends.md) | Wrong assumptions and red herrings, corrected. Check here before re-deriving something |
| [firmware-versions.md](firmware-versions.md) | What changed between analysed firmware releases |
| [methods.md](methods.md) | How to reproduce the analysis: extraction, Ghidra, scripts, techniques |
| [sources.md](sources.md) | Where the firmware and every external reference came from |

## Evidence labels

Claims carry a label:
- **[confirmed]** means directly traced in the firmware, observed live, or both.
- **[inferred]** or **[assumption]** marks the narrowest remaining inference. An assumption is
  never an implementation specification.

When pages disagree, trust them in this order:
1. Direct 3.1.6 decompiler evidence or genuine-camera captures, as cited in
   [firmware-behaviour.md](firmware-behaviour.md) and the [gap tracker](gap-tracker.md).
2. Confirmed statements in [protocol.md](protocol.md) and [status](../status.md).
3. Everything else.

Fix the lower-ranked page in the same change.

## Legal boundary

The firmware, its binaries and any decompiled output are Prusa Research's property and are **not**
in this repository. Pages cite function addresses and describe behaviour; they never contain copied
code. See [NOTICE.md](../../NOTICE.md).
