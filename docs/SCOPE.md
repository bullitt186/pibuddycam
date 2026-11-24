# Scope

What this project is for, so contributions go in the same direction. Changing this file is a
maintainer decision, recorded as an ADR in the same pull request (PR hygiene checks this).
Planned work is in the [roadmap](roadmap.md) and the current state in [status](status.md); this
page does not repeat them.

## Purpose

Owners of a Prusa printer who want a camera in Prusa Connect without buying Prusa's Buddy3D
camera. PiBuddyCam turns a Raspberry Pi Zero 2 W with a CSI camera into such a camera by speaking
the Buddy3D cloud protocol, reverse-engineered from its firmware, and adds local RTSP, ONVIF,
MQTT, a web console and timelapses. It ships as a flashable appliance image with signed OTA
updates ([README](../README.md)).

## In scope

- Firmware parity with the Buddy3D camera protocol, tracked item by item in the
  [gap tracker](reverse-engineering/gap-tracker.md).
- Local interfaces: RTSP, JPEG snapshot, ONVIF, MQTT with Home Assistant discovery, the web
  console and timelapses.
- The appliance: the SD-card image, setup wizard, read-only system partition, recovery and signed
  OTA updates with rollback.
- Camera sensors that pass the capture matrix ([roadmap](roadmap.md#later)).

## Out of scope

- Prusa firmware, binaries or decompiled output in the repository: they are not ours to
  distribute; behaviour is described and cited by function address instead
  ([CONTRIBUTING.md](../CONTRIBUTING.md#what-never-goes-into-the-repository)).
- Guessed protocol fields or enum values: behaviour that is not backed by evidence is not
  implemented ([evidence labels](reverse-engineering/README.md#evidence-labels)).
- Hardware other than the Raspberry Pi Zero 2 W, and features the original camera has no
  counterpart for beyond the local interfaces above: Open: to be decided by the maintainer.

## Direction

See the [roadmap](roadmap.md): hardware verification of 90°/270° rotation, a release acceptance
matrix, and closing the partial protocol gaps from evidence. Whether gaps of the kind
"implement or stop advertising" (OTA, timelapse, reboot, WebRTC audio, IR/speaker/fan/MicroSD)
are implemented is an owner decision.

## Principles

- Evidence first: every protocol claim is labelled **[confirmed]** or **[assumption]**.
- An appliance that survives power loss: read-only system, signed updates the owner approves,
  automatic rollback.
- Standard library first in the application; third-party packages are hash-locked in
  `image/requirements.lock`.
- No personal data and no firmware in the repository.
- One topic, one home in the documentation.
