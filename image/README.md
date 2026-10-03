# `image/`: the PiBuddyCam SD-card image

Everything needed to build, validate and package the Raspberry Pi Zero 2 W image: the
[rpi-image-gen](https://github.com/raspberrypi/rpi-image-gen) composition, a custom image layer,
the factory installer, first-boot helpers, and the release scripts. It contains no secrets and
generates no device identity. How to cut a release is in
[docs/releasing.md](../docs/releasing.md); how the result behaves at runtime is in
[docs/architecture.md](../docs/architecture.md).

## Build

Images are built **natively on arm64 Debian trixie** (for example on a Raspberry Pi 5).
`scripts/build-image.sh` refuses foreign-architecture builds and any rpi-image-gen checkout other
than the one pinned in `rpi-image-gen.lock` (tag `v2.8.0`, commit `262d4df5…`).

```sh
git clone --branch v2.8.0 --depth 1 https://github.com/raspberrypi/rpi-image-gen /path/to/rpi-image-gen
RPI_IMAGE_GEN_DIR=/path/to/rpi-image-gen image/scripts/build-image.sh
```

The build fails if the populated `ROOT` exceeds 75% of its 4 GiB, or if the image doesn't fit an
8 GB card. `SOURCE_DATE_EPOCH` comes from the source commit. ext4 UUIDs still vary between builds,
so output is not byte-identical.

## Layout

```text
image/
  rpi-image-gen.lock            pinned upstream revision
  requirements.in / .lock       Python runtime dependencies (hash-locked)
  config/pibuddycam-pi-zero2w.yaml  image composition (packages, hostname, users)
  layer/                        custom rpi-image-gen layers and hooks
    pibuddycam-suite.yaml         trixie minbase + NetworkManager
    pibuddycam-image.yaml         MBR 3-partition image layer
    genimage.cfg.in.ext4, setup.sh, pre-image.sh, post-build.sh, mke2fs.conf
  assets/                       installed into the image
    install-factory-app.sh        installs app/ to /opt/pibuddycam, units, helper, venv, config
    launcher.sh                   runs releases/current or the factory copy
    pibuddycam-priv, sudoers/     fixed-verb root helper and its sudoers rule
    pibuddycam-data-grow.sh       first-boot growth of PERSIST
    systemd/                      image-only units and drop-ins
    udev/, networkmanager/        camera and GPIO device access, stable Wi-Fi MAC, NTP dispatcher
    build-info.py                 writes /usr/share/pibuddycam/build-info.json
    icon/pibuddycam.png           Imager icon
  imager/os-list.template.json  Raspberry Pi Imager manifest template
  keys/                         release-signing public key (see keys/README.md)
  scripts/                      build-image, validate-image, make-release, make-app-release, scan-secrets
```

## Disk layout

MBR with exactly three primary partitions, `PERSIST` last so it can grow:

| # | Size in image | Filesystem | Label | Mount |
|---|---:|---|---|---|
| 1 | 512 MiB | FAT32 | `BOOT` | `/boot/firmware` |
| 2 | 4 GiB | ext4 | `ROOT` | `/`, **read-only** |
| 3 | 512 MiB | ext4 | `PERSIST` | `/data`, grown to the end of the card on first boot |

- **Stable references.** The fixed MBR disk signature makes PARTUUIDs deterministic
  (`<signature>-01/02/03`), so `cmdline.txt` and `fstab` never reference `/dev/mmcblk0pN`. The
  signature is a build constant, not a device identity.
- **Writable state.** `/var` and `/etc/pibuddycam` are tmpfs; everything durable lives on `/data`.
- **`overlayroot`.** The package is installed but not active. Don't rely on overlay semantics.

## Boot order

```text
local-fs.target
  → pibuddycam-data-grow.service → data-ready.target
     → NetworkManager.service
     → pibuddycam-boot-mode.service → pibuddycam.target   (or the setup hotspot while unclaimed)
        → pi-persist → rpicam-source → pibuddycam-rtsp → pibuddycam-ha-rtsp → pibuddycam
```

- **Shared unit files.** The application units in `app/systemd/` are installed verbatim; extra
  ordering is added only through drop-ins in `assets/systemd/`.
- **Isolated auxiliaries.** Admin, updater and MQTT units are `Wants=` under
  `pibuddycam.target`, never `Requires=`, so their failures stay isolated.

## Validation

```sh
python3 -m unittest discover -s tests/image -t .    # host tests for everything in image/
image/scripts/validate-image.sh --image <built.img> --mount-root <mounted root.ext4>
```

`validate-image.sh` checks a built image offline: units, ownership and modes, the launcher wiring,
the venv against the lock, the signing key, TLS provisioning and the absence of secrets. Run it as
root with a full `PATH` (it needs `dumpe2fs` and `mtools`). It is necessary but not sufficient:
see the [hardware field notes](../docs/hardware.md#field-notes-from-hardware-bring-up) for what
only real hardware catches.

## Release artifacts

`scripts/make-release.sh` turns one built `.img` into the published OS-image set. It needs no root
and no network:

```text
pibuddycam-pi-zero2w-<version>.img.xz(.sha256|.minisig)   compressed image (xz -T1 -9e)
pibuddycam-pi-zero2w-<version>.spdx.json                  SPDX 2.3 SBOM (from --packages/--build-info)
pibuddycam-pi-zero2w-<version>.packages.txt               installed-package manifest
pibuddycam-os-list.json, pibuddycam.png                    Raspberry Pi Imager manifest and icon
```

`scripts/make-app-release.sh` builds the signed OTA bundle (`pibuddycam-app-<version>.tar.zst`
plus `update-manifest.json`); see [releasing](../docs/releasing.md).

- **Imager manifest.** `imager/os-list.template.json` is rendered with JSON-encoded tokens (URLs,
  sizes, SHA-256s, version, device tags) and validated against the artifacts.
  - The device tag is `pi3-64bit`. The official Imager list tags the Zero 2 W as the Pi 3 family,
    with no Zero-2-W-only tag, so Imager may also offer the image for a Pi 3. It is validated for
    the Zero 2 W only.
  - `init_format` is `systemd`. Override it with `PIBUDDYCAM_IMAGER_INIT_FORMAT`.
- **Channels.** Release images (the default) have SSH off. `build-image.sh --dev` with
  `PIBUDDYCAM_DEV_SSH_PUBKEY_FILE` builds a developer image: `ssh.service` enabled, key-only login
  for `pibuddydev` with passwordless `sudo`, `channel: dev` in `build-info.json`. Never publish it.
  See [development](../docs/development.md#testing-on-hardware).
- **Signing.** With `--key` the scripts require `minisign` and sign every artifact. Only the public
  key is in the repository (see [keys/](keys/README.md)).
- **Secret scan.** `scripts/scan-secrets.sh` runs over every produced artifact and fails the
  release on any match. It checks for:
  - private keys, minisign secret keys and SSH host keys;
  - machine-ids;
  - Wi-Fi PSKs and `.nmconnection` profiles;
  - Prusa tokens and MQTT passwords;
  - personal usernames and home paths.

  Three cases are allowed: `<PLACEHOLDER>` values, the repository owner inside
  `github.com/<owner>/` URLs, and the maintainer's exact public GitHub handle. Override the
  patterns with `SCAN_PERSONAL_USER_PATTERN` and `SCAN_PUBLIC_HANDLE_PATTERN`. The scanner source and the synthetic test fixtures naturally
  match their own patterns, so scan artifacts, not the scanner.
