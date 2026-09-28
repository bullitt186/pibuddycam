# Roadmap

Planned and open work, highest payoff first. The current state is in [status](status.md). Protocol
items are tracked in detail in the [gap tracker](reverse-engineering/gap-tracker.md).
Contributions are welcome: see [CONTRIBUTING.md](../CONTRIBUTING.md).

## Next

1. **Verify 90°/270° rotation on hardware.** Check that `libcamerasrc` exists, that the ISP
   exposes a rotate control, that the encoder takes 1080×1920 portrait, and what the software
   fallback costs in CPU. See [hardware](hardware.md#camera-and-rotation).
2. **Release acceptance matrix.** Cover fresh cards and card sizes, every onboarding route,
   Home Assistant + Prusa coexistence, and power-loss and recovery. See
   [status: acceptance](status.md#acceptance).
3. **Close the partial protocol gaps from evidence.** GAP-QUALITY-02 (per-payload persist flag)
   comes first. Never infer event wiring or field types; recover them from descriptors or
   captures (see [methods](reverse-engineering/methods.md)).

## Later

- **Understand Connect's camera-service registry**, which is behind the "Other cameras" state. It
  needs a read-only comparison with a token known to be in the registry (e.g. from a genuine
  camera) or information from Prusa. See [status](status.md#the-current-connect-limitation).
- Support more camera sensors once each passes the capture matrix.
