#!/bin/bash
# post-build hook for the PiBuddyCam image layer.
#
# rpi-image-gen v2.8.0 invokes IMAGE_ASSET/post-build.sh with the target
# filesystem path after bdebstrap completes (bin/runner phase_args post-build).
# Removes every build-time identity so the released image carries none.
set -eu

fs="$1"

# No persistent machine-id: leave it empty and let systemd generate a fresh
# one on first boot (systemd-machine-id-setup).
rm -f "$fs/etc/machine-id" "$fs/var/lib/dbus/machine-id"
install -m 0644 /dev/null "$fs/etc/machine-id"
ln -sf /etc/machine-id "$fs/var/lib/dbus/machine-id"

# No build-time SSH host key. The reused openssh-server layer already removes
# the postinst keys; ssh-hostkeys-generate.service regenerates them on first
# boot only if SSH is later enabled.
rm -f "$fs"/etc/ssh/ssh_host_*

# SSH must be disabled by default (AC-13/AC-20). The reused openssh-server layer
# runs `enable-units ssh ssh-hostkeys-generate.service` AFTER our image-layer
# customize hook, so the disable must happen here, after every layer. `disable`
# (not `mask`) keeps Raspberry Pi Imager able to re-enable SSH later.
#
# The developer channel (build-image.sh --dev) is the only exception: its
# build-info.json says channel=dev and install-factory-app.sh has installed the
# key-only sshd drop-in, so SSH stays enabled. Anything else, including a missing
# or unreadable build-info.json, takes the release path.
SSH_UNITS="ssh.service ssh.socket ssh-hostkeys-generate.service"
channel="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("channel",""))' \
   "$fs/usr/share/pibuddycam/build-info.json" 2>/dev/null || true)"
if [ "$channel" = dev ]; then
   [ -f "$fs/etc/ssh/sshd_config.d/10-pibuddycam-dev.conf" ] || {
      echo "pibuddycam-image: ERROR: dev channel without the key-only sshd drop-in" >&2
      exit 1; }
   chroot "$fs" systemctl enable ssh.service ssh-hostkeys-generate.service >/dev/null
   if ! ls "$fs/etc/systemd/system/"*.wants/ssh.service >/dev/null 2>&1; then
      echo "pibuddycam-image: ERROR: dev channel but ssh.service is not enabled" >&2
      exit 1
   fi
   echo "pibuddycam-image: dev channel: SSH enabled (key-only)"
else
for unit in $SSH_UNITS sshd.service; do
   rm -f "$fs/etc/systemd/system/"*.wants/"$unit"
done
for unit in $SSH_UNITS sshd.service; do
   for link in "$fs/etc/systemd/system/"*.wants/"$unit"; do
      if [ -e "$link" ] || [ -L "$link" ]; then
         echo "pibuddycam-image: ERROR: SSH enablement symlink survived: $link" >&2
         exit 1
      fi
   done
done
fi

# No Wi-Fi profile or captured connection.
rm -f "$fs"/etc/NetworkManager/system-connections/*.nmconnection 2>/dev/null || true

# The service account is non-login and its home is /opt/pibuddycam; drop any
# home directory created by the generic user-provisioning layer.
rm -rf "$fs/home/pibuddycam"
