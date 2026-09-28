# Guide: app, web console and tests

For changes in `app/` and `tests/`. The module map and conventions are in
[app/README.md](../../app/README.md); tests, CI and hardware testing are in
[development.md](../development.md).

- **Settings path.** Every setting change goes through `settings_coordinator.py`, whichever
  surface it comes from (Connect, MQTT, console, boot restore). Don't add a second write path to
  `state.json` or the config files.
- **Dependencies.** Standard library first. A new third-party package means a change to
  `image/requirements.in`/`.lock` (hash-locked) and usually a new image. Flag it to the user.
- **Privilege.** Anything that needs root becomes a verb of the image's fixed helper
  (`privileged.py` ↔ `image/assets/pibuddycam-priv`). That is an image change; see
  [image-appliance.md](image-appliance.md).
- **Tests.**
  - Put tests in `tests/app/test_<module>.py`, using only the standard library.
  - Changes to `app/web/` or the admin HTTP API also run the Playwright suite
    ([development](../development.md#tests)).
  - A file that exists only on your disk (git-ignored) passes locally and fails in CI.
- **User-visible changes** update the owning page in the same commit (for example
  [user-guide](../user-guide.md) for a console setting, [integrations](../integrations.md) for
  MQTT) and add a line to `CHANGELOG.md` under *Unreleased*.
- **Commit only what the user asked for**, in focused commits whose messages explain *why*.
