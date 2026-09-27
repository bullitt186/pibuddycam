# Archive: historical, unmaintained

Everything in this folder is kept only for context and credit. **Nothing here is current, tested
or supported**, and its links and paths may be broken. The maintained documentation starts at the
[project README](../README.md) and [docs/](../docs/README.md).

| Path | What it was | Superseded by |
|---|---|---|
| `docs/journal/` | Early reverse-engineering notes (firmware 3.1.5 era), first impersonator spec, first build plan | [docs/reverse-engineering/](../docs/reverse-engineering/README.md) |
| `docs/next-steps.md` | Rolling work plan up to 2026-09 | [docs/roadmap.md](../docs/roadmap.md), [gap tracker](../docs/reverse-engineering/gap-tracker.md) |
| `docs/implementation.md` | "Build your own impersonator" tutorial for the early single-script app | [docs/architecture.md](../docs/architecture.md), [docs/development.md](../docs/development.md) |
| `docs/public-appliance-distribution-plan.md` | Design plan for the appliance (image, onboarding, OTA, MQTT) | Implemented. See [architecture](../docs/architecture.md) and [integrations](../docs/integrations.md) |
| `docs/home-assistant-onvif-implementation-plan.md` | Implementation plan for ONVIF/Home Assistant support | Implemented. See [integrations](../docs/integrations.md) |
| `docs/camerainfo-verification.md` | Ghidra work plan for the `CameraInfo` message | Done; results are in [protocol](../docs/reverse-engineering/protocol.md) |
| `docs/status-history.md`, `docs/gap-tracker-closed.md` | Dated session logs and evidence for closed firmware-parity gaps | [docs/status.md](../docs/status.md), [gap tracker](../docs/reverse-engineering/gap-tracker.md) |
| `legacy-dev-install/` | Pre-appliance developer install (`deploy.sh`, `bootstrap.sh`, `config.ini`), the `config.ini` importer and an unused pairing-QR parser | The flashable image ([install](../docs/install.md)) |
| `proxy/` | Rust tool that bridged an already-registered genuine camera's cloud stream to local RTSP/MQTT. Not built or tested since 2026-07 | — |
