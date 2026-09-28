# Security policy

## Reporting a vulnerability

Please **don't open a public issue** for security problems. Report them privately through
[GitHub Security Advisories](https://github.com/bullitt186/pibuddycam/security/advisories/new).
Include what you found, how to reproduce it, and the affected version (web console →
*System → Health & version*). You'll get an acknowledgement as soon as possible. Please give us a
reasonable time to fix the issue before disclosing it.

Report these privately too:
- any real credential, token, personal data or firmware content found in the repository;
- any problem that lets someone bypass update signature checks.

## Supported versions

Only the **latest release** receives fixes. Devices update over the air once the owner approves
an update; changes to image-owned parts need a reflash.

## Security model

- **Trusted LAN by design.** The web console requires a password and uses HTTPS with a
  device-generated certificate. RTSP (`8554`/`8555`), ONVIF and `/snapshot.jpg` are
  **unauthenticated** so Home Assistant and RTSP players can use them. Never expose ports 80, 443,
  8554 or 8555 to the Internet.
- **Setup.** The setup network is open while the device is unclaimed and turns off after setup.
  Do the setup in a trusted place.
- **Least privilege.** The runtime runs unprivileged; root actions go through a fixed-verb helper
  with no user-supplied arguments ([architecture](docs/architecture.md#privilege-boundary)).
- **Secrets.** They're stored in a `0600` file on the data partition, the admin password as a salted
  scrypt hash, and they're redacted from the UI, logs, diagnostics and MQTT.

## Verifying releases

Every release artifact and update manifest is signed with minisign. The public key is
[`image/keys/pibuddycam-release.pub`](image/keys/pibuddycam-release.pub) (key ID
`704A1F5710E6EA94`), and the same key is embedded in the image. The device's updater refuses
anything that doesn't verify.

```sh
minisign -V -p image/keys/pibuddycam-release.pub -m pibuddycam-pi-zero2w-<version>.img.xz
sha256sum -c pibuddycam-pi-zero2w-<version>.img.xz.sha256
```

The key was rotated for 1.4.0. Images before 1.4.0 trust the retired key `48680BE111FEB8E3`
and have to be reflashed anyway (see the [changelog](CHANGELOG.md)). A future rotation would
again reach devices only with a new image.
