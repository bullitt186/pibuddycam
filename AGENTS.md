# Agent instructions

PiBuddyCam turns a Raspberry Pi Zero 2 W into a camera for Prusa Connect by speaking the Buddy3D
camera protocol. It includes local RTSP, ONVIF, MQTT, a web console, a flashable image and signed
OTA updates. This file is the only agent entry point. It holds the rules; the linked pages hold
the knowledge. Humans read the same docs: start at [docs/README.md](docs/README.md).

## Rules that always apply

1. **Authorization boundary.** Editing and testing repository files is always fine. Touching a
   live device, a Connect account or token, firmware, the release runner, repository settings or
   release tags is not. Do these only when the user explicitly asks, and announce disruptive
   steps first.
2. **No private data, no firmware.** Never commit:
   - personal data: IPs, MACs, SSIDs, hostnames, usernames, home paths, tokens, captures, HAR
     files, screenshots;
   - Prusa firmware, binaries or decompiled output.

   Redact values to `<PLACEHOLDER>` form. See [CONTRIBUTING.md](CONTRIBUTING.md#what-never-goes-into-the-repository).
3. **Evidence honesty.** Label protocol claims **[confirmed]** or **[assumption]**, never guess a
   field number or enum value, and follow the evidence precedence in
   [docs/reverse-engineering/README.md](docs/reverse-engineering/README.md#evidence-labels).
4. **Owner decisions.** Where a gap offers "implement or stop advertising", the owner decides:
   OTA, timelapse, reboot, WebRTC audio, and IR/speaker/fan/MicroSD. Ask; don't pick.
5. **One topic, one home.** Update the page that owns a topic (see [docs/README.md](docs/README.md))
   in the same change as the code. Don't duplicate content into new files. Obsolete material goes
   to `_archive/`, never to a second live copy.
6. **Validate before claiming done.**
   ```sh
   python3 -m unittest discover -s tests -t . && python3 -m compileall -q app tests
   ```
   There is no formatter or type checker, so never claim one ran. Details are in
   [docs/development.md](docs/development.md#tests).

## Job guides

Read the guide for your task before starting:

| Task | Guide |
|---|---|
| Close a firmware-parity gap or change protocol behaviour | [docs/agents/protocol-gap-work.md](docs/agents/protocol-gap-work.md) |
| Change the app, web console or tests | [docs/agents/app-development.md](docs/agents/app-development.md) |
| Change the image, units, packages or boot behaviour | [docs/agents/image-appliance.md](docs/agents/image-appliance.md) |
| CI, releases, OTA publishing, the release runner | [docs/agents/releases-ci.md](docs/agents/releases-ci.md) |
| Work on a physical device (only when asked) | [docs/agents/live-hardware-ops.md](docs/agents/live-hardware-ops.md) |

## Where things are

`app/` runtime · `image/` SD-card image · `tests/{app,image,docs,e2e}` · `docs/` documentation
(`reverse-engineering/`, `agents/`) · `research/` Ghidra helpers · `_archive/` historical, never
edit to "fix" it · `.agent/pi-ops.md` the maintainer's private runbook (git-ignored; may not exist).
