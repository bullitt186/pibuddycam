# Guide: working on a physical device

**Only when the user explicitly asks for live work.** Connection details and the private runbook
are in the maintainer's git-ignored `.agent/pi-ops.md`, which may not exist. The template is
[pi-ops.example.md](pi-ops.example.md). Hardware background is in [hardware](../hardware.md) and
[development: testing on hardware](../development.md#testing-on-hardware).

## The loop

1. Make the change in the repository with its tests, and commit the exact source you will deploy.
   The release manifest records its commit.
2. **App change:** build a signed bundle and install it through the updater
   (`pibuddycam-priv install-update`). Never patch `/opt/pibuddycam`: it is only the factory
   fallback.
3. **Image-owned file needed for the same test:** install the exact repository file on the device:
   ```sh
   mount -o remount,rw /
   install -o root -g root -m <mode> /tmp/<file> <destination>
   mount -o remount,ro /
   ```
   The write **persists**, so remount read-only even on failure, and make sure the identical change
   is in `image/`. A device-only fix is a bug.
4. **Verify:** active release (`readlink /data/pibuddycam/releases/current`), service health,
   snapshot, both RTSP ports, the console, the affected journal, the Connect behaviour, and that
   `ROOT` is read-only.
5. **Clean up:** remove temporary updater URLs and CAs. Record the evidence (version, date,
   result) in the relevant `GAP-*` item or doc.

## Care points

- **Announce disruptive steps before you run them:** reboot, stopping the setup network, a service
  restart. Stopping the setup network before the station link is proven drops the device off the
  network.
- **Config ownership.** `/data/pibuddycam/config/*.toml` must stay `pibuddycam`-owned (`0640`
  and `0600`). Never print them; they contain secrets.
- **The user flashes cards.** Flashing and moving cards is a user action. A flash recreates `/data`.
- **Diagnostics without a console.** Read `/boot/firmware/bootlog.txt`; the journal is in RAM
  and lost on reboot.
- **Never test power loss with a hard reset.** It has corrupted a card before. Use the documented
  acceptance procedure instead.
