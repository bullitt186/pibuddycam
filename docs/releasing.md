# CI, releases and OTA publishing

This is the canonical description of how this repository is tested and released. Private runner
host details (hostname, service management, key location) live in the git-ignored
`.agent/pi-ops.md`. Never copy them into tracked files.

## Workflows

| Workflow | Trigger | Runs on | What it does |
|---|---|---|---|
| `.github/workflows/ci.yml` | push to any branch, pull request | `ubuntu-latest` | host gate on Python 3.12 + 3.13: unit tests, byte-compile, `bash -n` of all scripts, secret scan of changed files, `git diff --check` |
| `.github/workflows/release.yml` | tag `vX.Y.Z` (stable) or `alpha-vX.Y.Z` (alpha prerelease) | host gate on `ubuntu-latest`, then `[self-hosted, linux, arm64]` | builds the appliance image natively, assembles and signs the OS-image and application releases, validates them, uploads a workflow artifact, publishes the GitHub Release |
| `release.yml` (manual) | `workflow_dispatch` with a `version` input | same | **dry run:** everything above except publishing. Use it to prove the runner after workflow, image or runner changes |

The host gate installs PyYAML plus `minisign`, `mtools`, `dosfstools` and `zstd`, so the image
config, signature and boot-partition tests run instead of being skipped. The application itself
stays stdlib-only. Only 2 tests are expected to skip. They are mutually exclusive
"minisign absent" cases.

## Keeping CI green

- Run `python3 -m unittest discover -s tests` and `python3 -m compileall -q app tests`
  before pushing (see `CLAUDE.md`).
- **A local pass is not enough if a file is only on your disk.** `.gitignore` excludes `*.png`,
  `*.img`, `*.env` and similar patterns. A file the build or tests need must be tracked, with a
  negation rule if necessary. `test_image_subtree_required_files_exist` fails locally when a
  required image file is git-ignored and untracked. Extend its list when you add a required file.
- Tests that need an optional tool (PyYAML, minisign, openssl, sfdisk, …) must use
  `unittest.skipUnless`. Then add the tool to the CI install step so the test actually runs there.
- The secret scan in CI covers changed non-test, non-doc files. Names such as
  `token=…`/`password=…` in shell or YAML trip it and the `image/` personal-data test. Pick neutral
  variable names.

## Release runner

- There is one owner-operated native arm64 Debian Trixie runner, registered to this repository with
  the labels `self-hosted, Linux, ARM64` and running as a systemd service. The release job refuses
  any non-arm64 runner. Never substitute QEMU or foreign-architecture builds.
- The repository is **public**. Fork pull-request workflows require approval for **all** external
  contributors (Settings → Actions → General). Keep that setting, because otherwise a fork PR could
  run code on the self-hosted runner.
- The job expects these on the runner. Each can be overridden with the repository variable of the
  same name:
  - `RPI_IMAGE_GEN_DIR` (default `/data/build/rpi-image-gen`): a checkout at the commit pinned in
    `image/rpi-image-gen.lock`.
  - `WORKROOT` (default `/data/build/work`): build work area.
  - `RELEASE_SIGNING_KEY_FILE` (default `/data/build/keys/release-signing.key`, mode `0600`): the
    minisign secret key. It is used only when the `RELEASE_SIGNING_KEY` secret is unset, which is
    the current setup, so the key never has to be stored in GitHub. The job copies it into
    `$RUNNER_TEMP` and deletes the copy.
- The runner service's PATH lacks the `sbin` directories. The job adds them itself, because the
  image build needs `mkdosfs`, `sfdisk` and `mke2fs`.
- `pip download` of the offline wheelhouse runs under the runner's own Python 3.13. Environment
  markers in `image/requirements.lock` are evaluated against that interpreter.

## Cutting a release

1. Land the change on `main` with CI green.
2. Optionally run the dry run: `gh workflow run release.yml --ref main -f version=X.Y.Z`.
3. Tag and push: `git tag -a vX.Y.Z -m "…" && git push origin vX.Y.Z`. Use `alpha-vX.Y.Z` for the
   alpha channel. The version must be strict `X.Y.Z` with no suffix.
4. The release publishes the compressed image, SBOM, Imager manifest, the application bundle,
   `update-manifest.json` and their `.minisig` signatures. The workflow then verifies that the
   published assets exactly match `dist/`.

**Minimum image version:** the workflow sets the application manifest's `min_image_version` to
the release's own version. The updater compares it with the installed image version: the
`--current-image-version` argument, else the `PIBUDDYCAM_IMAGE_VERSION` environment variable, else
(since 1.4.0) the `version` in the image's own `/usr/share/pibuddycam/build-info.json`. Only a strict
`X.Y.Z` counts. Development images report `0.0.0+local`, which skips the check. So a CI-built bundle installs only on an image of the same or a newer version. An
application-only release meant for older images must be built manually (below) with an explicit
`--min-image-version`. Pre-rename images (before 1.4.0) don't run this check, but they can't run a
1.4.0+ bundle either: `image_guard` stops it and the old updater rolls back.

**Image-owned changes** (systemd units, packages, `/usr/libexec` helpers, udev/NM rules, boot
config) only reach devices through a newly flashed image. The OTA bundle cannot change them.

## Manual fallback (application bundle only)

Use this when the runner is unavailable or a lower `min_image_version` is needed:

1. Build an aarch64/cp313 wheelhouse with a **Python 3.13** pip. For example, create a venv with
   `uv venv -p 3.13 --seed`, then run
   `pip download --require-hashes --only-binary=:all: --platform manylinux_2_17_aarch64 --platform manylinux2014_aarch64 --platform manylinux_2_28_aarch64 --python-version 3.13 --implementation cp --abi cp313 --abi abi3 --abi none -r image/requirements.lock -d wheels`.
   A pip running under another Python evaluates the markers wrongly.
2. Run `image/scripts/make-app-release.sh --version X.Y.Z --out-dir dist --wheels wheels --url-base https://github.com/<owner>/<repo>/releases/download/vX.Y.Z --key <minisign secret key> [--min-image-version …]`.
3. Verify with `minisign -V -p image/keys/pibuddycam-release.pub` and create the release with
   `gh release create vX.Y.Z dist/*`. Pushing that tag also triggers `release.yml`. Its publish step
   then updates the same release, so decide which path owns a given tag.

## OTA on the device

Devices check the manifest configured as `PIBUDDYCAM_UPDATE_MANIFEST_URL` in the root-owned
`/etc/pibuddycam-updater.conf`. Since 1.4.0 the image writes the project's moving GitHub URL,
`https://github.com/bullitt186/pibuddycam/releases/latest/download/update-manifest.json`, which
follows each new stable release. A build can override it with the environment variable
`PIBUDDYCAM_UPDATE_MANIFEST_URL_DEFAULT`, and an empty value disables the check. Every bundle and manifest is verified against the embedded public key
`image/keys/pibuddycam-release.pub`.

Building or publishing a release never authorizes installing it on the owner's device. The owner
triggers OTA themselves.
