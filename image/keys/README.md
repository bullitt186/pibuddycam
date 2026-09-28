# Release signing keys

- `pibuddycam-release.pub` — the minisign **public** key. Committed. It is embedded
  in the image, where the updater uses it to verify every bundle and manifest, and the release
  pipeline checks its own output against it.
- The matching **secret** key is NOT in this repository. It lives only in the
  release environment (see `docs/releasing.md`). Never commit it, log it,
  copy it into an image, or place it on a device.

Public key ID: `704A1F5710E6EA94` (since 1.4.0; the retired development key was
`48680BE111FEB8E3`).

```sh
# sign (release environment only)
minisign -S -s <release-signing.key> -m <file>
# verify
minisign -V -p image/keys/pibuddycam-release.pub -m <file>
```

Devices trust the key embedded in their image, so rotating it takes effect only with a new
image, and older images can no longer verify new releases. Rotate only with a new image
release, and record it in the changelog.
