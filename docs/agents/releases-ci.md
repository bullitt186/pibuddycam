# Guide: CI, releases and OTA publishing

The process is in [releasing.md](../releasing.md) and CI in
[development](../development.md#continuous-integration). This page adds agent rules only.

- **Needs an explicit request:**
  - pushing a release tag (`vX.Y.Z`, `alpha-vX.Y.Z`);
  - publishing, editing or deleting a GitHub release;
  - changing repository or Actions settings;
  - touching the self-hosted runner or its signing key.

  A release request never implies installing it on a device: the owner installs OTA updates
  themselves.
- **Prefer the pipeline.** Tag and let `release.yml` build on the arm64 runner. Use the manual
  fallback only when asked or when the runner is unavailable. Run a `workflow_dispatch` dry run
  after changing workflows, image scripts or the runner.
- **Check CI.** CI must be green on the commit you tag. After pushing, watch the run. If it's red,
  root-cause and fix it; never skip or disable a test. Gate commits on the unit-test **exit code**,
  not on grepping its output.
- **Verify published assets.** Download them from the public URL, check the minisign signatures
  against `image/keys/pibuddycam-release.pub`, and confirm the manifest's `version`,
  `source_commit` and URLs.
- **Keys.** The signing key never enters the repository, logs or the image; the runner copies it
  into the job's temp directory. Runner and host specifics belong in the private
  `.agent/pi-ops.md`, never in tracked files.
- **Release notes.** Record user-visible changes in `CHANGELOG.md`, and say plainly when a
  release needs a new image.
