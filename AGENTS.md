# Agent instructions

<!-- repo-kit:begin v2 (managed, do not edit; https://github.com/bullitt186/repo-kit) -->
## How to work in this repository (all coding agents)

This block is shared by all repositories of this maintainer and kept in sync by
[repo-kit](https://github.com/bullitt186/repo-kit). Repository-specific
instructions follow below it. They add detail; where they contradict this block,
this block wins, unless an ADR in `docs/adr/` records the exception.

A pull request that follows this block passes review without rework. The
maintainer reviews against the same rules: the **Definition of Done** in
`CONTRIBUTING.md`.

### Build the right thing

1. Read this file to the end, `CONTRIBUTING.md`, `docs/SCOPE.md` and the
   architecture decision records in `docs/adr/` (start with `docs/adr/README.md`).
   An *Accepted* ADR and the scope are binding. If your change needs a different
   decision, write a new ADR that supersedes the old one, in the same pull request.
2. Work from an issue. A feature or behaviour change needs an issue the maintainer
   has labelled `accepted`; if there is none, open one, describe the problem (not
   only the solution) and wait. Only a **small fix** may skip the issue: it repairs
   documented behaviour, changes fewer than about 50 lines of code, and adds no
   dependency, interface, setting or file format.
3. Build what the issue asks for and nothing else. Ideas you have on the way become
   a new issue, not part of this pull request. No reformatting, renaming or
   refactoring that the change does not need.
4. Prefer the smallest complete change: reuse what the repository already has
   (helpers, patterns, dependencies) before adding anything; no new dependency for
   a few lines. Fix the root cause, not the symptom.
5. Never guess protocol values, API fields or hardware behaviour. Take them from a
   source and name it; mark anything unconfirmed as an assumption.

### Design first

Write the design down before the code: the behaviour that changes, the modules
and interfaces involved, and how it is tested. Put it in the issue or the pull
request description; for a small change the commit message is enough.

Write an ADR (`docs/adr/`, see its README) when the change:

- adds, removes or replaces a runtime dependency or external service,
- changes a public interface: API, CLI, configuration keys, file or data formats,
  entity or topic names, anything users or other systems rely on,
- changes the architecture: module boundaries, data flow, persistence, threading,
- sets a security, privacy or compatibility rule,
- reverses or replaces an earlier ADR, or changes `docs/SCOPE.md`.

Bug fixes and refactorings inside existing decisions need no ADR.

### Tests

Design the test cases from the specification, then implement the change together
with them. A one-test-at-a-time red/green cycle is not required.

- A bug fix starts with a regression test that reproduces the bug. Run it and
  confirm it fails **for the reason the bug describes**, not because of a build,
  import or signature error. Then fix the bug and commit the test with the fix.
- Expected values come from the specification: documentation, a protocol table,
  a manual, the issue, or a recorded real payload. Never from running the code
  under test and copying its output; such a test only proves the code does what
  it does.
- Assert observable results (output, state, sent request, raised error), not only
  that something was called.
- Tests are fast, deterministic and offline. A flaky test is a bug to fix, not to
  re-run.
- Changing an expected value in an existing test needs a reason in the commit
  message that names the source of the new value.
- Coverage shows what ran, not whether a test notices a wrong result. Run mutation
  testing on the code you changed (see Commands below); kill a surviving mutant
  with a test, or say in the pull request why it is equivalent.
- Do not weaken lint rules, thresholds or checks to make a change pass; fix the
  code. A justified exception gets a comment next to it and a note in the commit
  message. Thresholds are only ever raised.

### Documentation

Documentation changes in the same pull request as the behaviour it describes:
README, `docs/`, configuration examples, the ADRs and `CHANGELOG.md`. Outdated
documentation counts as a bug. Relative links in Markdown must point to files
that exist.

Changelog entries go under `[Unreleased]`, in the Keep a Changelog sections
(Added, Changed, Fixed, Removed, Security). Write them for the user of the
project: what changes for them and what they have to do, not which functions
moved. A breaking change gets an entry under "Upgrade notes" with the steps.
Name outside contributors and link their issue or pull request. Releases, tags
and version numbers are the maintainer's
([release standard](https://github.com/bullitt186/repo-kit/blob/main/docs/RELEASING.md)).

### Commits and branches

- One branch per topic from the current default branch; never commit to the
  default branch. Agents working in parallel use their own git worktree; never
  run checkout, switch, reset, stash or clean in a checkout someone else uses.
- Commit each completed step with a Conventional Commits message (`feat:`,
  `fix:`, `docs:`, `test:`, `refactor:`, `chore:`, `ci:`); the body says why.
  Code, comments, commit messages and pull requests are in English.
- Reference the issue: `Closes #N` on GitHub; `Refs #N` on Forgejo, which closes
  an issue as soon as a commit with `Closes` is pushed.
- Never commit or push on red; never use `--no-verify` or skip hooks; treat every
  hook finding as work to do.

### Before you open the pull request

- Run the repository's **fast check** before every commit and the **full check**
  when your change can affect what it covers (see Commands below). Both must pass.
- Merge or rebase onto the current default branch and run the checks again.
- Fill in the pull request template completely; tick only what you did and say
  why anything stays unticked.
- After pushing, watch CI yourself. A red check on your pull request is your work,
  also when it looks unrelated.
- No secrets, tokens, personal data, local paths, host names or IP addresses in
  code, tests, fixtures, logs, commits or pull request text.

### Continuous integration

CI follows the repo-kit [CI standard](https://github.com/bullitt186/repo-kit/blob/main/docs/CI_STANDARD.md):
tests, lint and format, type check, build, secret scan, dependency review, static
analysis and `PR hygiene`. PR hygiene fails when code changes come without a test
change or a `CHANGELOG.md` entry, when `docs/SCOPE.md` changes without an ADR,
when an ADR is malformed or missing from the index, when a changed Markdown file
has a broken relative link, when code from an outside contributor links no issue,
or when a pull request changes more than about 800 lines of code and tests. Only
the maintainer can waive a rule (labels `no-test-change`, `skip-changelog`,
`large-change`). Do not ask for a waiver when a test, entry or split is possible.

### Respect

Do not change CI configuration, branch protection, release workflows, licence,
`docs/SCOPE.md` or this block unless the issue asks for it. Maintainer decisions
recorded in issues and ADRs are not up for silent change.
<!-- repo-kit:end -->

## This repository

### Purpose

Scope and non-goals: [docs/SCOPE.md](docs/SCOPE.md).

PiBuddyCam turns a Raspberry Pi Zero 2 W into a camera for Prusa Connect by speaking the Buddy3D
camera protocol. It includes local RTSP, ONVIF, MQTT, a web console, a flashable image and signed
OTA updates. This file is the only agent entry point. It holds the rules; the linked pages hold
the knowledge. Humans read the same docs: start at [docs/README.md](docs/README.md).

### Layout

`app/` runtime · `image/` SD-card image · `tests/{app,image,docs,e2e}` · `docs/` documentation
(`reverse-engineering/`, `agents/`) · `research/` Ghidra helpers · `_archive/` historical, never
edit to "fix" it · `.agent/pi-ops.md` the maintainer's private runbook (git-ignored; may not exist).
The full table is in [docs/development.md](docs/development.md#repository-layout); decisions are in
[docs/adr/](docs/adr/README.md).

### Commands

| Task | Command |
|---|---|
| Install / set up | Git and Python 3.12 or newer; the unit tests need only the standard library. Optional, so fewer tests skip: `python3 -m pip install PyYAML==6.0.2`, `minisign`, `mtools`, `dosfstools`, `zstd`, and `python3 -m pip install --require-hashes -r image/requirements.lock` for the HTTP route tests. For lint and type check: `python3 -m pip install -r requirements-dev.txt` (ruff, mypy) and the runtime lock, `shellcheck`, Node.js 22 and `npm ci` (ESLint) |
| Fast check (before every commit, also the stop hook) | `python3 -m unittest discover -s tests -t . && python3 -m compileall -q app tests && python3 scripts/check_lint.py && npm run lint && mypy` (ruff lint, ruff format on the changed lines, shellcheck, ESLint for `app/web/`, mypy over `app/`) |
| Full check | the fast check, `for f in image/scripts/*.sh app/*.sh image/assets/*.sh image/assets/pibuddycam-priv image/assets/networkmanager/dispatcher.d/*; do bash -n "$f"; done`, and for changes to `app/web/` or the admin HTTP API the console end-to-end suite `NODE_PATH="$(npm root -g)" tests/e2e/run.sh` (set-up in [docs/development.md](docs/development.md#tests)) |
| Mutation testing of changed code | not set up yet ([#14](https://github.com/bullitt186/pibuddycam/issues/14)); until then, check new assertions by breaking the changed line by hand and confirming a test fails |


### Rules specific to this repository

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
6. **Image-owned changes.** Changes to systemd units, packages, the privileged helper or boot
   configuration reach devices only with a new image. Say so in the pull request and in the
   `CHANGELOG.md` entry ("needs a new image").
7. **Releases** are built and signed from a tag by `release.yml` on the owner's arm64 runner;
   the process is [docs/releasing.md](docs/releasing.md) and the agent rules are in
   [docs/agents/releases-ci.md](docs/agents/releases-ci.md)
   ([ADR-0002](docs/adr/0002-releases-signed-from-a-tag-on-the-owners-arm64-runner.md)).
   Release notes follow the repo-kit
   [release standard](https://github.com/bullitt186/repo-kit/blob/main/docs/RELEASING.md).
8. **Fork workflows.** The release runner is self-hosted, so workflows of every outside
   contributor need the maintainer's approval before they run. Keep that setting.
9. **CI standard exception: build.** The image builds only on the owner's arm64 runner from a tag
   or a manual dry run (`workflow_dispatch`), never on pull requests, because that runner must not
   run untrusted code. Run the dry run after changing workflows, image scripts or the runner.
10. **CI standard exception: lint baseline.** Existing code is not reformatted or fixed in bulk; see
    [docs/development.md](docs/development.md#tests) for the baseline and how to retire it.

#### Job guides

Read the guide for your task before starting:

| Task | Guide |
|---|---|
| Close a firmware-parity gap or change protocol behaviour | [docs/agents/protocol-gap-work.md](docs/agents/protocol-gap-work.md) |
| Change the app, web console or tests | [docs/agents/app-development.md](docs/agents/app-development.md) |
| Change the image, units, packages or boot behaviour | [docs/agents/image-appliance.md](docs/agents/image-appliance.md) |
| CI, releases, OTA publishing, the release runner | [docs/agents/releases-ci.md](docs/agents/releases-ci.md) |
| Work on a physical device (only when asked) | [docs/agents/live-hardware-ops.md](docs/agents/live-hardware-ops.md) |
