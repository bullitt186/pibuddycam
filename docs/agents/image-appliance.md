# Guide: image, units and appliance behaviour

For changes in `image/` and `app/systemd/`. The image is described in
[image/README.md](../../image/README.md), runtime behaviour in [architecture](../architecture.md),
and hard-won defects in the [hardware field notes](../hardware.md#field-notes-from-hardware-bring-up).
Read the field notes before changing units, packages, the camera path or networking.

## Who owns a change decides how it ships

| Change | Ships via | Source of truth |
|---|---|---|
| Python code, web assets | Signed OTA application release | `app/` |
| Units, the privileged helper, sudoers/udev/NetworkManager rules, packages, boot configuration, `launcher.sh`, the updater's factory copy, the signing public key | **New image** (reflash) | `image/` plus the shared `app/systemd/` |
| Device configuration and state | The app or console writer | `/data/pibuddycam/` on the device |

An OTA bundle can never change image-owned files. Say so when a change needs a new image, and note
it in `CHANGELOG.md`.

## Rules

- **Filesystem.** `ROOT` is mounted read-only and `/var` and `/etc/pibuddycam` are tmpfs; every
  durable byte goes to `/data`. Don't design for `overlayroot`: it is installed but inactive.
- **Keep units shared.** Install `app/systemd/` units verbatim. Add image-only ordering through
  drop-ins in `image/assets/systemd/`, and keep auxiliary units `Wants=`, never `Requires=`.
- **Validate before claiming.** Run `python3 -m unittest discover -s tests/image -t .`, and extend
  `validate-image.sh` when you add something the image must contain. The validator is necessary
  but not sufficient; real hardware finds more.
- **Required files are tracked.** An asset the build needs must be committed (watch `.gitignore`
  patterns) and listed in the required-files test.
- **Building an image needs native arm64.** Use the release runner's dry run (see
  [releases-ci.md](releases-ci.md)) or a machine the user names. Never claim an image works
  without a hardware result.
