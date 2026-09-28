# Contributing to PiBuddyCam

Thanks for your interest! Bug reports, hardware test results, documentation fixes, protocol
evidence and code are all welcome. By taking part you agree to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Ways to help

- **Report a bug.** Use the [bug report form](https://github.com/bullitt186/pibuddycam/issues/new/choose)
  and attach the redacted diagnostics from the web console (*System → Diagnostics*).
- **Test hardware.** Results for other camera sensors, card sizes, onboarding routes and 90°/270°
  rotation directly advance the [roadmap](docs/roadmap.md).
- **Protocol evidence.** Firmware analysis or captures that resolve an item in the
  [gap tracker](docs/reverse-engineering/gap-tracker.md). Read
  [reverse engineering](docs/reverse-engineering/README.md) first.
- **Code and docs.** Pick an open issue or a roadmap item. For anything large, open an issue first
  so we can agree on the approach.

## Getting set up

You need Git and Python 3.12 or newer; the tests use only the standard library.

```sh
git clone https://github.com/bullitt186/pibuddycam.git && cd pibuddycam
python3 -m unittest discover -s tests -t .
```

[docs/development.md](docs/development.md) covers the repository layout, the optional test tools,
the web console's end-to-end tests, building an image and testing on a Pi.
[docs/architecture.md](docs/architecture.md) explains how the pieces fit together.

## Making a change

1. **Start a branch.** Create it from `main`. Keep each pull request focused on one change.
2. **Add or update tests.** Protocol changes need decoded or golden-byte tests; state changes
   need transition tests, including failures.
3. **Update the documentation page that owns the topic** in the same pull request. Each topic has
   one home, listed in [docs/README.md](docs/README.md); don't copy content into a second place.
   User-visible changes get a line under *Unreleased* in [CHANGELOG.md](CHANGELOG.md).
4. **Run the checks:**
   ```sh
   python3 -m unittest discover -s tests -t .
   python3 -m compileall -q app tests
   for f in image/scripts/*.sh app/*.sh; do bash -n "$f"; done
   ```
5. **Write clear commit messages.** An imperative subject (≤ 72 characters) says what changed; the
   body says *why*.
6. **Open the pull request** using the template. CI must pass. Workflows from first-time and
   external contributors need a maintainer's approval before they run.

Changes to image-owned parts (systemd units, packages, the privileged helper, boot configuration)
only reach devices with a new image. Say so in the pull request.

## Protocol claims must be honest

Mark anything about Prusa's protocol or firmware as **[confirmed]** (observed live or traced in
the firmware) or **[assumption]**. Never guess a field number or enum value. When evidence
contradicts a document, fix the document in the same change. See
[evidence labels](docs/reverse-engineering/README.md#evidence-labels).

## What never goes into the repository

- **Firmware.** Prusa firmware, binaries, and extracted or decompiled output of any kind.
  Describe behaviour and cite function addresses instead.
- **Personal data.** Real tokens, fingerprints, MAC or IP addresses, SSIDs, hostnames, usernames,
  home paths, packet captures, HAR files, screenshots. Redact values to `<PLACEHOLDER>` form.
- **Secrets.** Signing keys, `secrets.toml`, `.env` files.

CI runs a secret scan on every change, but it is a safety net, not a guarantee. If you find
something sensitive already in the repository, report it privately (see [SECURITY.md](SECURITY.md)).

## Releases

Maintainers cut releases by pushing a `vX.Y.Z` tag. The pipeline builds and signs the image and the
OTA bundle on an arm64 runner (see [docs/releasing.md](docs/releasing.md)).

## Coding agents

AI coding agents follow [AGENTS.md](AGENTS.md), which links to the same documentation with
agent-specific guides.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
