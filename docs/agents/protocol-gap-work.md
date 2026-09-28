# Guide: firmware-parity and protocol work

For work that changes how PiBuddyCam talks to Prusa Connect. The knowledge lives in
[docs/reverse-engineering/](../reverse-engineering/README.md); this page is the workflow.

## Closing a gap

Work on one named `GAP-*` item from the [gap tracker](../reverse-engineering/gap-tracker.md), or
on an explicitly stated group of them.

1. **Read first.** Read the gap, its evidence in
   [firmware-behaviour.md](../reverse-engineering/firmware-behaviour.md), the relevant part of
   [protocol.md](../reverse-engineering/protocol.md), and [dead-ends.md](../reverse-engineering/dead-ends.md).
   Then read the current code.
2. **Check prerequisites.** If a descriptor, capture, hardware observation or owner decision the
   gap depends on is missing, recover or request it and leave the gap open. Fields marked
   `descriptor required`, `BLOCKED` or **[assumption]** are not specifications.
3. **Implement the smallest change** that reproduces the documented behaviour. Values published
   on several surfaces come from the shared runtime state (`state.py`). Keep Pi-specific
   mechanics behind the wire behaviour.
4. **Test at the wire level.** Wire changes need decoded or golden-byte fixtures; state changes
   need transition tests, including failure branches.
5. **Validate** (see [AGENTS.md](../../AGENTS.md)).
6. **Record the result.** Update the gap's checkbox with the commit, test names, verification
   date and evidence label. Update `protocol.md` and [status](../status.md) when externally
   visible behaviour changes. When a gap closes, move its evidence to
   `_archive/docs/gap-tracker-closed.md` and leave a one-line entry in the tracker.

Live verification needs a device and the user's explicit request (see
[live-hardware-ops.md](live-hardware-ops.md)).

## Firmware analysis

- Follow [methods.md](../reverse-engineering/methods.md). The firmware and every export stay
  outside the repository.
- **Prefer GhidrAssistMCP** (`mcp__ghidrassist__*` tools) when available. It queries the
  maintainer's open Ghidra GUI session on `lp_app` and is much faster than headless runs. Its MCP
  configuration is local and not tracked. Fall back to the headless scripts in `research/ghidra/`.
- Cite evidence by function VMA (`FUN_000a1394`) and constant, never by export line number.
