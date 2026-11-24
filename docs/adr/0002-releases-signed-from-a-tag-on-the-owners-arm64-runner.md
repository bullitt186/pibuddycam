# ADR-0002: Releases signed from a tag on the owner's arm64 runner

- **Status:** Accepted
- **Date:** 2026-11-24

## Context

Recorded after the fact from [docs/releasing.md](../releasing.md) and
`.github/workflows/release.yml`. PiBuddyCam ships an SD-card image and an OTA application bundle
for a Raspberry Pi Zero 2 W. Devices install only bundles whose signature matches the public key
in `image/keys/pibuddycam-release.pub`. The image must be built natively on arm64; the signing key
must never be stored in the repository, in logs or in the image. The version exists only in the
tag: no file in the repository carries it, and the workflow writes it into the release manifest.
The repo-kit release standard expects a release pull request that bumps version files.

## Decision

We will publish releases only from a pushed tag (`vX.Y.Z`, or `alpha-vX.Y.Z` for the alpha
channel; strict `X.Y.Z`). `release.yml` builds the image and the bundle on the owner-operated
self-hosted arm64 runner, signs them with minisign using the key kept on that runner, verifies
the published assets and publishes the GitHub release. The `CHANGELOG.md` section for the version
lands on `main` through a pull request before the tag; there is no version file to bump.
Because the runner is self-hosted and the repository is public, workflows of every outside
contributor need the maintainer's approval before they run.

## Consequences

Tags and releases need the maintainer's explicit request ([docs/agents/releases-ci.md](../agents/releases-ci.md)).
The manual fallback in `docs/releasing.md` exists for runner outages. A release only installs on
an image of the same or a newer version unless it is built manually with a lower
`min_image_version`. Changing the runner, the key handling or the approval setting needs a new ADR.

## Alternatives considered

- Hosted runners with QEMU: foreign-architecture builds are refused by the release job.
- A version file bumped in a release pull request: would duplicate the tag, which is the single
  source of the version.
