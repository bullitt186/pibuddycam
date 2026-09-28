# Release signing keys

- `pibuddycam-release.pub` — the minisign **public** key. Committed. It is embedded
  in the image, where the updater uses it to verify every bundle and manifest, and the release
  pipeline checks its own output against it.
- The matching **secret** key is NOT in this repository. It lives only in the
  release environment (see `docs/releasing.md`). Never commit it, log it,
  copy it into an image, or place it on a device.

Public key ID: `48680BE111FEB8E3`

```sh
# sign (release environment only)
minisign -S -s <release-signing.key> -m <file>
# verify
minisign -V -p image/keys/pibuddycam-release.pub -m <file>
```

This is still the development key. Before a stable public release it will be replaced
with an owner-held key. Only the public half changes in the repository, but devices trust the key
embedded in their image, so the change takes effect with the next image.
