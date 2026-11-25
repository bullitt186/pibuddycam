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
- **Code and docs.** Pick an open issue labelled `accepted` or a roadmap item. For a feature or
  behaviour change, open an issue first and wait until the maintainer labels it `accepted` (see
  *How ideas become changes* below); what fits is in [docs/SCOPE.md](docs/SCOPE.md).

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
   for f in image/scripts/*.sh app/*.sh image/assets/*.sh image/assets/pibuddycam-priv \
            image/assets/networkmanager/dispatcher.d/*; do bash -n "$f"; done
   python3 scripts/check_lint.py      # needs requirements-dev.txt and shellcheck
   npm ci && npm run lint             # ESLint for app/web/
   mypy                               # type check of app/ (mypy.ini)
   ```
5. **Write clear commit messages.** [Conventional Commits](https://www.conventionalcommits.org/)
   (`feat:`, `fix:`, `docs:`, …) with an imperative subject (≤ 72 characters) that says what
   changed; the body says *why*.
6. **Open the pull request** using the template; it is the Definition of Done below as a
   checklist. Say whether the change needs a new image. CI must pass. Workflows from external
   contributors need a maintainer's approval before they run.

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
OTA bundle on an arm64 runner (see [docs/releasing.md](docs/releasing.md) and
[ADR-0002](docs/adr/0002-releases-signed-from-a-tag-on-the-owners-arm64-runner.md)).

## Coding agents

AI coding agents follow [AGENTS.md](AGENTS.md), which links to the same documentation with
agent-specific guides.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).

<!-- repo-kit:begin v2 (managed, do not edit; https://github.com/bullitt186/repo-kit) -->
## Definition of Done

This section is shared by all repositories of this maintainer
([repo-kit](https://github.com/bullitt186/repo-kit)). The maintainer reviews
every pull request, from people and from coding agents, against exactly this
list. `AGENTS.md` explains how to get there.

A pull request is done when:

1. **Fit** – it fits `docs/SCOPE.md` and solves an issue labelled `accepted`
   (a small fix as defined in `AGENTS.md` needs no issue); a change of the scope
   comes with an ADR.
2. **Scope** – it does one thing; unrelated changes and unrequested extras are
   left out.
3. **Design** – the design is written down (issue, pull request or commit
   message); an ADR in `docs/adr/` is added or updated where `AGENTS.md` asks for
   one, and no *Accepted* ADR is contradicted.
4. **Tests** – behaviour changes and bug fixes have tests that fail without the
   change, for the right reason; expected values come from a specification or
   real data; surviving mutants in changed code are killed or explained.
5. **Docs** – README, `docs/`, examples and `CHANGELOG.md` (`[Unreleased]`,
   written for users) describe the new state; upgrade notes exist for breaking
   changes.
6. **Checks** – the fast and the full check pass locally, and all CI checks of the
   [CI standard](https://github.com/bullitt186/repo-kit/blob/main/docs/CI_STANDARD.md),
   including `PR hygiene`, are green on the current default branch.
7. **Hygiene** – no secrets, personal data or generated artefacts; Conventional
   Commits (`feat:`, `fix:`, `docs:`, …).
8. **Template** – the pull request template is filled in truthfully.

### How ideas become changes

1. Open an issue that describes the problem. The maintainer labels it `accepted`
   (fits the scope, a pull request is welcome), `out-of-scope` (with the reason),
   `needs-info` or `decision`.
2. Build only after `accepted`, following `AGENTS.md`. Link the issue with
   `Closes #N` (GitHub) or `Refs #N` (Forgejo).
3. To move the project in a new direction, propose a change of `docs/SCOPE.md`
   as an issue first; if accepted, it lands with an ADR.

### What needs the maintainer

Decided by the maintainer and possibly slower: breaking changes, claims about
hardware or external systems that need evidence from a real device, new runtime
dependencies, scope changes, large pull requests, and anything that changes
releases or repository settings. Say so in the pull request.

### Labels

| Label | Meaning |
|---|---|
| `accepted` | in scope; a pull request is welcome |
| `out-of-scope` | does not fit `docs/SCOPE.md` |
| `needs-info` | waiting for information from the reporter |
| `needs-hardware` | needs a test on a device or system the maintainer does not have |
| `blocked` | waits for another issue or pull request (linked) |
| `decision` | needs a maintainer decision |
| `breaking` | changes behaviour users rely on |
| `adr` | adds or changes an architecture decision record |
| `no-test-change`, `skip-changelog`, `large-change` | maintainer waiver for a PR hygiene rule |
<!-- repo-kit:end -->
