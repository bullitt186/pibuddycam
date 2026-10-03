"""Host tests for ``image/scripts/validate-image.sh`` (AC-13, WP-2b).

Hermetic: no root, no mount, no loop device, no network. Partition-table tests
build a synthetic 3-partition MBR file with ``truncate`` + ``sfdisk`` in a temp
directory (and are skipped where ``sfdisk`` is absent). Rootfs tests build a
synthetic directory tree that represents the mounted ROOT filesystem so no mount
is needed. BOOT/PERSIST tests build real FAT/ext4 images with ``mkfs.fat`` +
``mcopy`` and ``mke2fs -d`` (skipped when those tools are absent).

All fixtures use synthetic placeholders only: no personal usernames, no real
MACs/SSIDs, no secret material.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "image" / "scripts" / "validate-image.sh"
REPO_SYSTEMD = REPO_ROOT / "app" / "systemd"
ASSET_SYSTEMD = REPO_ROOT / "image" / "assets" / "systemd"

HAS_SFDISK = shutil.which("sfdisk") is not None

MKFS_FAT = shutil.which("mkfs.fat") or shutil.which("mkfs.vfat")
HAS_BOOT_TOOLS = all(
    tool is not None for tool in (MKFS_FAT, shutil.which("mcopy"), shutil.which("mtype"))
)

MKE2FS = shutil.which("mke2fs")
HAS_PERSIST_TOOLS = (
    MKE2FS is not None and shutil.which("debugfs") is not None
)

DISKSIG = "0xb33dcafe"
ALIGN_SECTORS = 16384  # 8 MiB, matching genimage `align = 8M`
BOOT_SIZE_SECTORS = 1048576  # 512 MiB
ROOT_SIZE_SECTORS = 8388608  # 4 GiB
PERSIST_SIZE_SECTORS = 1048576  # 512 MiB

REQUIRED_DATA_DIRS = [
    "pibuddycam",
    "pibuddycam/config",
    "pibuddycam/releases",
    "pibuddycam/backups",
    "network",
    "network/system-connections",
    "sdcard",
    "sdcard/timelapse",
]

# Durable dirs the validator requires to be root:root 0:0 rather than owned by
# the service account (the root updater owns releases; NetworkManager the store).
PERSIST_ROOT_ONLY = {
    "pibuddycam/releases",
    "network",
    "network/system-connections",
}

# The synthetic tree is created by the (unprivileged) test runner, so every
# file is owned by the current uid/gid. Map the image's "root" account to that
# uid/gid and give pibuddycam a distinct sentinel so the validator's ownership
# assertions are exercised without needing real root (B3).
SYNTH_ROOT_UID = os.getuid()
SYNTH_ROOT_GID = os.getgid()
SYNTH_SERVICE_UID = os.getuid() + 1000
SYNTH_SERVICE_GID = os.getgid() + 1000


def run_validator(*args):
    return subprocess.run(
        [str(SCRIPT), *[str(a) for a in args]],
        capture_output=True,
        text=True,
    )


def valid_partitions():
    """(start, size, type, bootable) for BOOT/ROOT/PERSIST, 8M-aligned."""
    boot_start = ALIGN_SECTORS
    root_start = boot_start + BOOT_SIZE_SECTORS
    persist_start = root_start + ROOT_SIZE_SECTORS
    return [
        (boot_start, BOOT_SIZE_SECTORS, "c", True),
        (root_start, ROOT_SIZE_SECTORS, "83", False),
        (persist_start, PERSIST_SIZE_SECTORS, "83", False),
    ]


def write_mbr_image(path, entries, disksig=DISKSIG):
    """Create a sparse image file and lay down the given MBR entries."""
    path = Path(path)
    total_bytes = max(start + size for start, size, _, _ in entries) * 512
    truncate = shutil.which("truncate")
    if truncate:
        subprocess.run(
            [truncate, "-s", str(total_bytes), str(path)],
            check=True,
            capture_output=True,
        )
    else:  # pragma: no cover - coreutils truncate is normally present
        with open(path, "wb") as handle:
            handle.truncate(total_bytes)

    lines = ["label: dos", f"label-id: {disksig}", "unit: sectors"]
    for start, size, ptype, bootable in entries:
        line = f"start={start}, size={size}, type={ptype}"
        if bootable:
            line += ", bootable"
        lines.append(line)

    subprocess.run(
        ["sfdisk", "--no-reread", "-q", str(path)],
        input="\n".join(lines) + "\n",
        text=True,
        check=True,
        capture_output=True,
    )
    return path


def make_image(path):
    """A valid synthetic image when sfdisk exists, else a dummy file.

    On hosts without sfdisk the validator skips the partition group, so the
    rootfs tests still exercise everything else.
    """
    if HAS_SFDISK:
        return write_mbr_image(path, valid_partitions())
    path = Path(path)
    path.write_bytes(b"\x00" * 1024)
    return path


def make_rootfs(base):
    """Build a synthetic, defect-free rootfs tree representing the image."""
    root = Path(base)

    systemd = root / "etc" / "systemd" / "system"
    systemd.mkdir(parents=True)

    # Reused application units verbatim, plus the image-only units.
    for src in sorted(REPO_SYSTEMD.glob("*.service")) + sorted(
        REPO_SYSTEMD.glob("*.target")
    ) + sorted(REPO_SYSTEMD.glob("*.timer")):
        shutil.copy2(src, systemd / src.name)
    for name in (
        "pibuddycam-data-grow.service",
        "pibuddycam.target",
        "pibuddycam-boot-mode.service",
    ):
        shutil.copy2(ASSET_SYSTEMD / name, systemd / name)

    # Boot-mode gating (AC-12/AC-17): the selector is enabled at
    # multi-user.target; the camera target, the provisioning service and the
    # camera units are deliberately NOT enabled, and pibuddycam-admin.service is
    # enabled only under pibuddycam.target.wants (post-claim).
    wants = systemd / "multi-user.target.wants"
    wants.mkdir(parents=True)
    os.symlink("../pibuddycam-boot-mode.service", wants / "pibuddycam-boot-mode.service")
    # WP-R4b: the updater timer is enabled at multi-user.target; its oneshot
    # service is triggered by the timer and is not enabled directly.
    os.symlink("../pibuddycam-updater.timer", wants / "pibuddycam-updater.timer")
    # The network watchdog timer is enabled the same way; its service is not.
    os.symlink(
        "../pibuddycam-network-watchdog.timer", wants / "pibuddycam-network-watchdog.timer")
    camera_wants = systemd / "pibuddycam.target.wants"
    camera_wants.mkdir(parents=True)
    os.symlink("../pibuddycam-admin.service", camera_wants / "pibuddycam-admin.service")

    # Overlay root: config file and boot cmdline.
    (root / "etc" / "overlayroot.conf").write_text(
        'overlayroot="tmpfs:recurse=0"\n', encoding="utf-8"
    )
    # Read-only ROOT + volatile tmpfs mounts for the writable runtime state.
    (root / "etc" / "fstab").write_text(
        "# synthetic fstab\n"
        "PARTUUID=b33dcafe-02     /               ext4  ro,noatime 0 1\n"
        "PARTUUID=b33dcafe-01     /boot/firmware  vfat  defaults,rw 0 2\n"
        "PARTUUID=b33dcafe-03     /data           ext4  defaults 0 2\n"
        "tmpfs                    /var            tmpfs  mode=0755 0 0\n"
        "tmpfs                    /etc/pibuddycam  tmpfs  mode=0750,uid=1000,gid=1000 0 0\n",
        encoding="utf-8",
    )
    boot = root / "boot" / "firmware"
    boot.mkdir(parents=True)
    (boot / "cmdline.txt").write_text(
        "console=serial0,115200 root=PARTUUID=b33dcafe-02 rootfstype=ext4 "
        "rootwait overlayroot=tmpfs\n",
        encoding="utf-8",
    )

    # Volatile, size-limited journald.
    journald = root / "etc" / "systemd" / "journald.conf.d"
    journald.mkdir(parents=True)
    (journald / "10-volatile.conf").write_text(
        "[Journal]\nStorage=volatile\nRuntimeMaxUse=32M\n", encoding="utf-8"
    )

    # Camera device access: pibuddycam must be able to open /dev/dma_heap/*.
    udev = root / "etc" / "udev" / "rules.d"
    udev.mkdir(parents=True)
    (udev / "50-pibuddycam-camera.rules").write_text(
        'SUBSYSTEM=="dma_heap", GROUP="video", MODE="0660"\n', encoding="utf-8"
    )

    # GPIO access for the timelapse trigger: udev rule + gpio group membership.
    (udev / "60-pibuddycam-gpio.rules").write_text(
        'SUBSYSTEM=="gpio", KERNEL=="gpiochip*", GROUP="gpio", MODE="0660"\n',
        encoding="utf-8",
    )
    (root / "etc" / "group").write_text(
        "root:x:0:\nvideo:x:44:pibuddycam\ngpio:x:997:pibuddycam\n", encoding="utf-8"
    )

    # Time sync: timesyncd enabled, DHCP option 42 handed over by a dispatcher.
    sysinit_wants = systemd / "sysinit.target.wants"
    sysinit_wants.mkdir(parents=True)
    os.symlink(
        "/lib/systemd/system/systemd-timesyncd.service",
        sysinit_wants / "systemd-timesyncd.service",
    )

    # NetworkManager configuration.
    networkmanager = root / "etc" / "NetworkManager"
    networkmanager.mkdir(parents=True)
    (networkmanager / "NetworkManager.conf").write_text(
        "[main]\nplugins=keyfile\n", encoding="utf-8"
    )
    # Stable Wi-Fi identity: no scan-time MAC randomization.
    nm_conf = networkmanager / "conf.d"
    nm_conf.mkdir(parents=True, exist_ok=True)
    (nm_conf / "10-pibuddycam-mac.conf").write_text(
        "[device]\nwifi.scan-rand-mac-address=no\n", encoding="utf-8"
    )
    # Captive portal DNS for the setup hotspot.
    shared = networkmanager / "dnsmasq-shared.d"
    shared.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        REPO_ROOT / "image" / "assets" / "networkmanager" / "dnsmasq-shared.d"
        / "50-pibuddycam-captive.conf",
        shared / "50-pibuddycam-captive.conf",
    )

    dispatcher = networkmanager / "dispatcher.d"
    dispatcher.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        REPO_ROOT / "image" / "assets" / "networkmanager" / "dispatcher.d"
        / "50-pibuddycam-ntp",
        dispatcher / "50-pibuddycam-ntp",
    )
    os.chmod(dispatcher / "50-pibuddycam-ntp", 0o755)

    # console-setup.service masked (headless, ro ROOT).
    os.symlink("/dev/null", systemd / "console-setup.service")

    # Emulated SD mountpoint + Samba share config + volatile Samba tmpfiles.
    (root / "mnt" / "sdcard").mkdir(parents=True)
    samba = root / "etc" / "samba"
    samba.mkdir(parents=True, exist_ok=True)
    (samba / "smb-sdcard.conf").write_text(
        "[sdcard]\n"
        "   path = /mnt/sdcard\n"
        "   browseable = yes\n"
        "   read only = no\n"
        "   guest ok = yes\n"
        "   force user = pibuddycam\n"
        "   create mask = 0644\n"
        "   directory mask = 0755\n",
        encoding="utf-8",
    )
    (samba / "smb.conf").write_text(
        "[global]\n   workgroup = WORKGROUP\n"
        "\ninclude = /etc/samba/smb-sdcard.conf\n",
        encoding="utf-8",
    )
    tmpfiles = root / "etc" / "tmpfiles.d"
    tmpfiles.mkdir(parents=True, exist_ok=True)
    (tmpfiles / "pibuddycam-samba.conf").write_text(
        "d /var/lib/samba          0755 root root -\n"
        "d /var/lib/samba/private  0700 root root -\n"
        "d /var/log/samba          0755 root root -\n"
        "d /var/cache/samba        0755 root root -\n",
        encoding="utf-8",
    )

    # Factory application + launcher fallback. The fixture installs the real
    # image asset so the validator's WP-R4c launcher assertions (per-release
    # venv preference, factory fallback) exercise the shipped script.
    app = root / "opt" / "pibuddycam"
    app.mkdir(parents=True)
    # The image installs the factory app root-owned 0755 (B3); the fixture's
    # umask would otherwise make it group-writable.
    app.chmod(0o755)
    (app / "main.py").write_text("# synthetic factory app\n", encoding="utf-8")
    # WP-UI1/AC-2: the factory tree ships the local admin shell + assets.
    web = app / "web"
    web.mkdir()
    (web / "index.html").write_text(
        "<!doctype html><title>synthetic shell</title>\n", encoding="utf-8")
    (web / "app.css").write_text(":root{--bg:#fff}\n", encoding="utf-8")
    # The console script is split into ES modules: ship every one of them.
    for script in sorted((REPO_ROOT / "app" / "web").glob("*.js")):
        (web / script.name).write_text("export {};\n", encoding="utf-8")
    (web / "favicon.svg").write_text(
        "<svg xmlns='http://www.w3.org/2000/svg'></svg>\n", encoding="utf-8")
    launcher = app / "launcher.sh"
    shutil.copy2(REPO_ROOT / "image" / "assets" / "launcher.sh", launcher)
    launcher.chmod(0o755)
    # Admin TLS provisioning (appliance image/security defect): the boot-time
    # generator ships as admin_tls.py and admin_app.py must reference the
    # fail-closed resolver. Keep the synthetic content free of secret-looking
    # patterns so the image secret scan is not tripped.
    (app / "admin_tls.py").write_text(
        "TLS_DIR = '/data/pibuddycam/config/admin-tls'\n"
        "def ensure():\n    return True\n",
        encoding="utf-8",
    )
    (app / "admin_app.py").write_text(
        "def resolve_server_tls(mode):\n    raise TlsConfigurationError(mode)\n"
        "class TlsConfigurationError(RuntimeError):\n    pass\n",
        encoding="utf-8",
    )

    # Hash-locked runtime venv + dependency lock (WP-R3/AC-14). The installer
    # copies the lock mode 0644 and builds the venv with
    # ``--system-site-packages``; the fixture mirrors that layout. Everything
    # is owned by the synthetic "root" account (the test runner).
    lock = app / "requirements.lock"
    lock.write_text(
        "aiohttp==3.14.3 \\\n"
        f"    --hash=sha256:{'0' * 64}\n"
        "python-socketio==5.17.0 \\\n"
        f"    --hash=sha256:{'1' * 64}\n"
        "paho-mqtt==2.1.0 \\\n"
        f"    --hash=sha256:{'2' * 64}\n",
        encoding="utf-8",
    )
    lock.chmod(0o644)

    venv = app / "venv"
    venv_bin = venv / "bin"
    venv_bin.mkdir(parents=True)
    venv.chmod(0o755)
    venv_bin.chmod(0o755)
    venv_python = venv_bin / "python"
    venv_python.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    venv_python.chmod(0o755)
    (venv / "pyvenv.cfg").write_text(
        "home = /usr/bin\ninclude-system-site-packages = true\n",
        encoding="utf-8",
    )
    site_packages = venv / "lib" / "python3.13" / "site-packages"
    for module in ("aiohttp", "socketio", "paho"):
        (site_packages / module).mkdir(parents=True)

    # Privileged helper + narrow sudoers rule (B3). Both are owned by the
    # synthetic "root" account (the test runner) with the restricted modes the
    # image installs, so the validator's ownership/mode assertions are exercised.
    helper = root / "usr" / "libexec" / "pibuddycam" / "pibuddycam-priv"
    helper.parent.mkdir(parents=True)
    helper.write_text(
        "#!/bin/bash\n# synthetic fixed-verb helper\nexit 2\n", encoding="utf-8"
    )
    helper.chmod(0o755)
    # dnsmasq is required by NetworkManager's shared/hotspot mode; the validator
    # asserts /usr/sbin/dnsmasq exists (hardware-found missing package).
    dnsmasq = root / "usr" / "sbin" / "dnsmasq"
    dnsmasq.parent.mkdir(parents=True, exist_ok=True)
    dnsmasq.write_text("#!/bin/sh\n# synthetic dnsmasq\nexit 0\n", encoding="utf-8")
    dnsmasq.chmod(0o755)
    # openssl is required by the boot-time admin TLS provisioner; the validator
    # asserts the binary is present (added explicitly to the package manifest).
    openssl = root / "usr" / "bin" / "openssl"
    openssl.parent.mkdir(parents=True, exist_ok=True)
    openssl.write_text("#!/bin/sh\n# synthetic openssl\nexit 0\n", encoding="utf-8")
    openssl.chmod(0o755)
    # Shared-mode NAT backend (nftables/iptables); the validator requires one.
    nft = root / "usr" / "sbin" / "nft"
    nft.write_text("#!/bin/sh\n# synthetic nft\nexit 0\n", encoding="utf-8")
    nft.chmod(0o755)
    sudoers_dir = root / "etc" / "sudoers.d"
    sudoers_dir.mkdir(parents=True)
    sudoers_file = sudoers_dir / "pibuddycam"
    sudoers_file.write_text(
        "Defaults:pibuddycam env_reset\n"
        'Defaults:pibuddycam secure_path="/usr/local/sbin:/usr/local/bin:'
        '/usr/sbin:/usr/bin:/sbin:/bin"\n'
        "pibuddycam ALL=(root) NOPASSWD: /usr/libexec/pibuddycam/pibuddycam-priv\n",
        encoding="utf-8",
    )
    sudoers_file.chmod(0o440)

    # build-info.json.
    build_info_dir = root / "usr" / "share" / "pibuddycam"
    build_info_dir.mkdir(parents=True)
    (build_info_dir / "build-info.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_commit": "0" * 40,
                "builder_revision": "262d4df5a9f9d4133370465399a7958a7c22cdc7",
                "os_suite": "trixie",
                "kernel_package": "linux-image-rpi-v8",
                "package_manifest": "/usr/share/pibuddycam/packages.txt",
            }
        ),
        encoding="utf-8",
    )

    # Embedded release-signing public key (WP-R4b/AC-29): root:root 0644, no
    # private key material.
    (build_info_dir / "pibuddycam-release.pub").write_text(
        "untrusted comment: minisign public key SYNTHETIC\n"
        "RWTjuP4R4QtoSN543KA74kYYGJ7WkKAz2j5el+ZZC310+TJvrxVAia02\n",
        encoding="utf-8",
    )
    (build_info_dir / "pibuddycam-release.pub").chmod(0o644)

    # SSH disabled by default, root locked, no authorized_keys.
    ssh = root / "etc" / "ssh"
    ssh.mkdir(parents=True)
    (ssh / "sshd_config").write_text(
        "PasswordAuthentication no\nPermitRootLogin prohibit-password\n",
        encoding="utf-8",
    )
    (root / "etc" / "shadow").write_text(
        "root:!:19000:0:99999:7:::\n", encoding="utf-8"
    )
    (root / "etc" / "passwd").write_text(
        f"root:x:{SYNTH_ROOT_UID}:{SYNTH_ROOT_GID}:root:/root:/bin/bash\n"
        f"pibuddycam:x:{SYNTH_SERVICE_UID}:{SYNTH_SERVICE_GID}:PiBuddyCam:"
        "/opt/pibuddycam:/usr/sbin/nologin\n",
        encoding="utf-8",
    )

    # No persistent machine-id.
    (root / "etc" / "machine-id").write_text("", encoding="utf-8")

    # Initial PERSIST structure; ownership matches the passwd pibuddycam entry.
    data = root / "data"
    for relative in REQUIRED_DATA_DIRS:
        (data / relative).mkdir(parents=True, exist_ok=True)

    return root


def make_boot_image(
    path, cmdline, config="[pi02]\ndtoverlay=dwc2,dr_mode=host\n"
):
    """Build a FAT BOOT image with ``cmdline.txt`` and ``config.txt``.

    Requires mtools. ``config`` defaults to the Pi Zero 2 W USB host mode line
    the validator asserts; pass a different value to test its absence.
    """
    path = Path(path)
    with open(path, "wb") as handle:
        handle.truncate(16 * 1024 * 1024)
    subprocess.run(
        [MKFS_FAT, "-F", "32", str(path)], check=True, capture_output=True
    )
    source = path.with_name(path.name + ".cmdline.txt")
    source.write_text(cmdline, encoding="utf-8")
    subprocess.run(
        ["mcopy", "-i", str(path), str(source), "::/cmdline.txt"],
        check=True,
        capture_output=True,
    )
    cfg = path.with_name(path.name + ".config.txt")
    cfg.write_text(config, encoding="utf-8")
    subprocess.run(
        ["mcopy", "-i", str(path), str(cfg), "::/config.txt"],
        check=True,
        capture_output=True,
    )
    return path


def make_persist_image(path, tree, uid=None, gid=None):
    """Build an ext4 PERSIST image from ``tree`` (requires e2fsprogs).

    ``mke2fs -d`` preserves the source tree's (test-runner) ownership, so when
    ``uid``/``gid`` are given every seeded directory is re-owned in the image
    with ``debugfs sif`` to match the synthetic ``pibuddycam`` account. This lets
    the ownership assertion run without needing real root.
    """
    path = Path(path)
    with open(path, "wb") as handle:
        handle.truncate(64 * 1024 * 1024)
    subprocess.run(
        [MKE2FS, "-q", "-t", "ext4", "-d", str(tree), str(path)],
        check=True,
        capture_output=True,
    )
    if uid is not None and gid is not None:
        for relative in REQUIRED_DATA_DIRS:
            # Root-only dirs must be 0:0; the rest use the synthetic pibuddycam.
            owner_uid, owner_gid = (
                (0, 0) if relative in PERSIST_ROOT_ONLY else (uid, gid)
            )
            subprocess.run(
                ["debugfs", "-w", "-R", f"sif /{relative} uid {owner_uid}", str(path)],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["debugfs", "-w", "-R", f"sif /{relative} gid {owner_gid}", str(path)],
                check=True,
                capture_output=True,
            )
    return path


def make_persist_tree(base, dirs):
    """Create ``dirs`` under ``base``; mke2fs -d preserves their uid/gid."""
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    for relative in dirs:
        (base / relative).mkdir(parents=True, exist_ok=True)
    return base


class ValidateImageScriptSyntaxTests(unittest.TestCase):
    def test_script_exists_and_passes_bash_n(self):
        self.assertTrue(SCRIPT.is_file(), f"missing {SCRIPT}")
        result = subprocess.run(
            ["bash", "-n", str(SCRIPT)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_help_lists_documented_flags(self):
        result = run_validator("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        for flag in (
            "--image",
            "--mount-root",
            "--root-image",
            "--boot-image",
            "--persist-image",
            "--manifest",
            "--strict",
            "--disksig",
        ):
            self.assertIn(flag, result.stdout)


@unittest.skipUnless(HAS_SFDISK, "sfdisk not available")
class PartitionTableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _image(self, entries, name="image.img", disksig=DISKSIG):
        return write_mbr_image(Path(self.tmp) / name, entries, disksig)

    def test_correct_partition_table_passes(self):
        image = self._image(valid_partitions())
        result = run_validator("--image", image)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RESULT: PASS", result.stdout)
        self.assertIn("exactly 3 partitions", result.stdout)

    def test_wrong_partition_order_fails(self):
        boot, root, persist = valid_partitions()
        entries = [
            (boot[0], boot[1], "83", False),
            (root[0], root[1], "c", True),
            (persist[0], persist[1], "83", False),
        ]
        image = self._image(entries)
        result = run_validator("--image", image)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("[FAIL]", result.stdout)

    def test_missing_partition_fails(self):
        image = self._image(valid_partitions()[:2])
        result = run_validator("--image", image)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exactly 3 partitions", result.stdout)

    def test_wrong_partition_type_fails(self):
        boot, root, persist = valid_partitions()
        entries = [
            (boot[0], boot[1], "83", True),
            (root[0], root[1], "83", False),
            (persist[0], persist[1], "83", False),
        ]
        image = self._image(entries)
        result = run_validator("--image", image)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BOOT partition type", result.stdout)

    def test_wrong_partition_size_fails(self):
        # BOOT must be ~512 MiB (8 MiB alignment tolerance). 256 MiB and
        # 768 MiB are both far outside it; keep ROOT/PERSIST starts valid so the
        # only defect is the BOOT size.
        for label, boot_size in (("smaller", 524288), ("larger", 1572864)):
            with self.subTest(boot_size=label):
                boot_start = ALIGN_SECTORS
                root_start = boot_start + boot_size
                persist_start = root_start + ROOT_SIZE_SECTORS
                entries = [
                    (boot_start, boot_size, "c", True),
                    (root_start, ROOT_SIZE_SECTORS, "83", False),
                    (persist_start, PERSIST_SIZE_SECTORS, "83", False),
                ]
                image = self._image(entries, name=f"image-{label}.img")
                result = run_validator("--image", image)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("BOOT size", result.stdout)
                self.assertIn("RESULT: FAIL", result.stdout)

    def test_missing_bootable_flag_fails(self):
        boot, root, persist = valid_partitions()
        entries = [
            (boot[0], boot[1], boot[2], False),
            root,
            persist,
        ]
        image = self._image(entries)
        result = run_validator("--image", image)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BOOT bootable", result.stdout)

    def test_matching_disk_signature_passes(self):
        image = self._image(valid_partitions())
        result = run_validator("--image", image, "--disksig", DISKSIG)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("MBR disk signature", result.stdout)

    def test_wrong_disk_signature_fails(self):
        image = self._image(valid_partitions())
        result = run_validator("--image", image, "--disksig", "0xdeadbeef")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MBR disk signature", result.stdout)


class RootfsValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.image = make_image(Path(cls.tmp) / "image.img")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _root(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return make_rootfs(tmp)

    def test_good_rootfs_passes(self):
        root = self._root()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RESULT: PASS", result.stdout)
        self.assertIn("all required units installed", result.stdout)

    def test_missing_tmpfs_volatile_mounts_fails(self):
        # Read-only ROOT without the tmpfs mounts leaves NM/systemd unwritable.
        root = self._root()
        fstab = root / "etc" / "fstab"
        fstab.write_text(
            fstab.read_text(encoding="utf-8")
            .replace("tmpfs                    /var            tmpfs  mode=0755 0 0\n", "")
            .replace(
                "tmpfs                    /etc/pibuddycam  tmpfs  mode=0750,uid=1000,gid=1000 0 0\n",
                "",
            ),
            encoding="utf-8",
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must mount /var and /etc/pibuddycam on tmpfs", result.stdout)

    def test_missing_dnsmasq_fails(self):
        # NetworkManager shared/hotspot mode cannot activate without dnsmasq.
        root = self._root()
        (root / "usr" / "sbin" / "dnsmasq").unlink()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dnsmasq missing", result.stdout)

    def test_missing_camera_dma_heap_rule_fails(self):
        root = self._root()
        (root / "etc" / "udev" / "rules.d" / "50-pibuddycam-camera.rules").unlink()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing dma_heap udev rule", result.stdout)

    def test_missing_network_watchdog_timer_link_fails(self):
        root = self._root()
        (root / "etc" / "systemd" / "system" / "multi-user.target.wants"
         / "pibuddycam-network-watchdog.timer").unlink()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("network-watchdog.timer must be enabled", result.stdout)

    def test_enabled_network_watchdog_service_fails(self):
        root = self._root()
        os.symlink(
            "../pibuddycam-network-watchdog.service",
            root / "etc" / "systemd" / "system" / "multi-user.target.wants"
            / "pibuddycam-network-watchdog.service",
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("network-watchdog.service must not be enabled", result.stdout)

    def test_missing_network_watchdog_timer_fails(self):
        root = self._root()
        (root / "etc" / "systemd" / "system" / "pibuddycam-network-watchdog.timer").unlink()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("network watchdog units are missing", result.stdout)

    def test_camera_target_must_not_pull_the_network_watchdog(self):
        root = self._root()
        target = root / "etc" / "systemd" / "system" / "pibuddycam.target"
        target.write_text(
            target.read_text(encoding="utf-8")
            + "\nWants=pibuddycam-network-watchdog.timer\n", encoding="utf-8")
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not pull the network watchdog", result.stdout)

    def test_missing_web_module_fails(self):
        root = self._root()
        (root / "opt" / "pibuddycam" / "web" / "network.js").unlink()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("web assets missing", result.stdout)
        self.assertIn("network.js", result.stdout)

    def test_missing_gpio_udev_rule_fails(self):
        root = self._root()
        (root / "etc" / "udev" / "rules.d" / "60-pibuddycam-gpio.rules").unlink()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing gpiochip udev rule", result.stdout)

    def test_missing_gpio_group_fails(self):
        root = self._root()
        (root / "etc" / "group").write_text("root:x:0:\nvideo:x:44:pibuddycam\n", encoding="utf-8")
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gpio group missing", result.stdout)

    def test_service_account_outside_gpio_group_fails(self):
        root = self._root()
        (root / "etc" / "group").write_text("root:x:0:\ngpio:x:997:someone\n", encoding="utf-8")
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pibuddycam must be a member of the gpio group", result.stdout)

    def test_gpio_checks_pass_on_the_healthy_fixture(self):
        result = run_validator("--image", self.image, "--mount-root", self._root())
        self.assertIn("gpiochip udev rule grants the gpio group access", result.stdout)
        self.assertIn("pibuddycam is a member of the gpio group", result.stdout)

    def test_missing_ntp_dispatcher_fails(self):
        root = self._root()
        (root / "etc" / "NetworkManager" / "dispatcher.d" / "50-pibuddycam-ntp").unlink()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("NTP dispatcher script missing", result.stdout)

    def test_non_executable_ntp_dispatcher_fails(self):
        root = self._root()
        (root / "etc" / "NetworkManager" / "dispatcher.d" / "50-pibuddycam-ntp").chmod(0o644)
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dispatcher must be root:root mode 0755", result.stdout)

    def test_group_writable_ntp_dispatcher_fails(self):
        root = self._root()
        (root / "etc" / "NetworkManager" / "dispatcher.d" / "50-pibuddycam-ntp").chmod(0o775)
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)

    def test_disabled_timesyncd_fails(self):
        root = self._root()
        (root / "etc" / "systemd" / "system" / "sysinit.target.wants"
         / "systemd-timesyncd.service").unlink()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("systemd-timesyncd must be enabled", result.stdout)

    def test_timesyncd_ordering_dropin_that_would_cycle_fails(self):
        root = self._root()
        dropin = root / "etc" / "systemd" / "system" / "systemd-timesyncd.service.d"
        dropin.mkdir()
        (dropin / "10-data-ready.conf").write_text(
            "[Unit]\nAfter=pi-persist.service\n", encoding="utf-8")
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dependency cycle", result.stdout)

    def test_network_apply_unit_must_not_be_enabled(self):
        root = self._root()
        os.symlink(
            "../pibuddycam-network-apply.service",
            root / "etc" / "systemd" / "system" / "multi-user.target.wants"
            / "pibuddycam-network-apply.service",
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pibuddycam-network-apply.service must not be enabled", result.stdout)

    def test_network_apply_unit_with_install_section_fails(self):
        root = self._root()
        unit = root / "etc" / "systemd" / "system" / "pibuddycam-network-apply.service"
        unit.write_text(unit.read_text(encoding="utf-8") + "\n[Install]\nWantedBy=multi-user.target\n",
                        encoding="utf-8")
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not be enabled", result.stdout)

    def test_missing_network_apply_unit_fails(self):
        root = self._root()
        (root / "etc" / "systemd" / "system" / "pibuddycam-network-apply.service").unlink()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pibuddycam-network-apply.service is missing", result.stdout)

    def test_missing_captive_portal_dns_fails(self):
        root = self._root()
        (root / "etc" / "NetworkManager" / "dnsmasq-shared.d"
         / "50-pibuddycam-captive.conf").unlink()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing setup-hotspot captive DNS", result.stdout)

    def test_missing_stable_mac_conf_fails(self):
        root = self._root()
        (root / "etc" / "NetworkManager" / "conf.d" / "10-pibuddycam-mac.conf").unlink()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fingerprint flaps", result.stdout)

    def test_missing_firewall_backend_fails(self):
        root = self._root()
        for name in ("nft", "iptables"):
            candidate = root / "usr" / "sbin" / name
            if candidate.exists():
                candidate.unlink()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("firewall backend", result.stdout)

    def test_unit_referencing_personal_home_fails(self):
        root = self._root()
        unit = root / "etc" / "systemd" / "system" / "pibuddycam.service"
        unit.write_text(
            unit.read_text(encoding="utf-8")
            + "Environment=HOME=/home/alice\n",
            encoding="utf-8",
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("personal home", result.stdout)

    def _make_dev(self, root):
        """Turn the synthetic release rootfs into a developer-channel one."""
        info = root / "usr" / "share" / "pibuddycam" / "build-info.json"
        doc = json.loads(info.read_text(encoding="utf-8"))
        doc["channel"] = "dev"
        info.write_text(json.dumps(doc), encoding="utf-8")
        keys = root / "home" / "pibuddydev" / ".ssh"
        keys.mkdir(parents=True)
        (keys / "authorized_keys").write_text(
            "ssh-ed25519 AAAASYNTHETICKEY dev@example\n", encoding="utf-8"
        )
        dropin = root / "etc" / "ssh" / "sshd_config.d"
        dropin.mkdir()
        (dropin / "10-pibuddycam-dev.conf").write_text(
            "PasswordAuthentication no\nKbdInteractiveAuthentication no\n"
            "PermitRootLogin no\nAllowUsers pibuddydev\n"
            "HostKey /data/pibuddycam/ssh/ssh_host_ed25519_key\n",
            encoding="utf-8",
        )
        system = root / "etc" / "systemd" / "system"
        (system / "ssh.service.d").mkdir(parents=True)
        (system / "ssh.service.d" / "10-pibuddycam-dev.conf").write_text(
            "[Unit]\nWants=pibuddycam-dev-hostkey.service\n", encoding="utf-8"
        )
        (system / "pibuddycam-dev-hostkey.service").write_text(
            "[Service]\nType=oneshot\n", encoding="utf-8"
        )
        wants = root / "etc" / "systemd" / "system" / "multi-user.target.wants"
        wants.mkdir(parents=True, exist_ok=True)
        (wants / "ssh.service").symlink_to("/usr/lib/systemd/system/ssh.service")

    def test_dev_channel_with_key_only_ssh_passes_ssh_checks(self):
        root = self._root()
        self._make_dev(root)
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotIn("FAIL", " ".join(
            line for line in result.stdout.splitlines() if "dev image" in line
        ))
        self.assertIn("developer channel image", result.stdout)

    def test_dev_channel_without_data_hostkey_fails(self):
        # ROOT is read-only, so a dev sshd without a /data host key never starts.
        root = self._root()
        self._make_dev(root)
        (root / "etc" / "systemd" / "system" / "pibuddycam-dev-hostkey.service").unlink()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HostKey on /data", result.stdout)

    def test_dev_channel_without_key_only_dropin_fails(self):
        root = self._root()
        self._make_dev(root)
        (root / "etc" / "ssh" / "sshd_config.d" / "10-pibuddycam-dev.conf").unlink()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("10-pibuddycam-dev.conf", result.stdout)

    def test_release_channel_with_dev_ssh_material_fails(self):
        root = self._root()
        self._make_dev(root)
        info = root / "usr" / "share" / "pibuddycam" / "build-info.json"
        doc = json.loads(info.read_text(encoding="utf-8"))
        doc["channel"] = "release"
        info.write_text(json.dumps(doc), encoding="utf-8")
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("authorized_keys present", result.stdout)

    def test_release_with_service_account_authorized_keys_fails(self):
        root = self._root()
        ssh = root / "opt" / "pibuddycam" / ".ssh"
        ssh.mkdir(parents=True)
        (ssh / "authorized_keys").write_text("ssh-ed25519 AAAA x\n", encoding="utf-8")
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("authorized_keys present", result.stdout)

    def test_release_with_empty_service_account_authorized_keys_passes(self):
        # The openssh-server layer creates this empty file in every image.
        root = self._root()
        ssh = root / "opt" / "pibuddycam" / ".ssh"
        ssh.mkdir(parents=True)
        (ssh / "authorized_keys").write_text("", encoding="utf-8")
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotIn("authorized_keys present", result.stdout)
        self.assertIn("no authorized_keys installed", result.stdout)

    def test_ssh_host_key_present_fails(self):
        root = self._root()
        (root / "etc" / "ssh" / "ssh_host_ed25519_key").write_text(
            "synthetic-not-a-key\n", encoding="utf-8"
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SSH host key", result.stdout)

    def test_nonempty_machine_id_fails(self):
        root = self._root()
        (root / "etc" / "machine-id").write_text("a" * 32 + "\n", encoding="utf-8")
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("machine-id", result.stdout)

    def test_nmconnection_present_fails(self):
        root = self._root()
        profiles = root / "etc" / "NetworkManager" / "system-connections"
        profiles.mkdir(parents=True, exist_ok=True)
        (profiles / "synthetic.nmconnection").write_text(
            "[connection]\nid=synthetic\n", encoding="utf-8"
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nmconnection", result.stdout)

    def test_unit_missing_data_ready_ordering_fails(self):
        root = self._root()
        unit = root / "etc" / "systemd" / "system" / "rpicam-source.service"
        text = unit.read_text(encoding="utf-8")
        text = text.replace("After=network.target data-ready.target", "After=network.target")
        text = text.replace("Requires=data-ready.target\n", "")
        unit.write_text(text, encoding="utf-8")
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("data-ready.target", result.stdout)

    def test_missing_build_info_fails(self):
        root = self._root()
        (root / "usr" / "share" / "pibuddycam" / "build-info.json").unlink()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("build-info.json", result.stdout)

    def test_identity_json_in_data_fails(self):
        root = self._root()
        (root / "data" / "pibuddycam" / "identity.json").write_text(
            '{"device_id": "synthetic"}\n', encoding="utf-8"
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("device identity", result.stdout)

    def test_non_strict_without_mount_root_skips_and_passes(self):
        result = run_validator("--image", self.image)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SKIPPED", result.stdout)
        self.assertIn("not mounted", result.stdout)
        self.assertIn("RESULT: PASS", result.stdout)

    def test_strict_without_mount_root_fails(self):
        result = run_validator("--image", self.image, "--strict")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--strict", result.stdout)
        self.assertIn("RESULT: FAIL", result.stdout)

    @unittest.skipUnless(HAS_SFDISK, "sfdisk not available")
    def test_strict_ignores_absent_optional_inputs(self):
        # --strict must not fail just because --manifest/--boot-image/
        # --persist-image were not supplied. With a complete rootfs, a disk
        # signature, and no other runnable skip, strict passes.
        root = self._root()
        result = run_validator(
            "--image", self.image, "--mount-root", root,
            "--disksig", DISKSIG, "--strict",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RESULT: PASS", result.stdout)
        self.assertIn("optional-input check(s) skipped", result.stdout)


class BootModeGatingValidationTests(unittest.TestCase):
    """WP-R1 AC-12/AC-17: offline boot-mode gating assertions."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.image = make_image(Path(cls.tmp) / "image.img")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _root(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return make_rootfs(tmp)

    def _systemd(self, root):
        return Path(root) / "etc" / "systemd" / "system"

    def test_good_rootfs_reports_boot_mode_gating(self):
        root = self._root()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "pibuddycam-boot-mode.service is enabled at multi-user.target",
            result.stdout,
        )
        self.assertIn("pibuddycam-provisioning.service is not enabled", result.stdout)
        self.assertIn("pibuddycam.target is not enabled", result.stdout)
        self.assertIn(
            "pibuddycam-admin.service is enabled under pibuddycam.target.wants",
            result.stdout,
        )

    def test_boot_mode_not_enabled_fails(self):
        root = self._root()
        (
            self._systemd(root) / "multi-user.target.wants"
            / "pibuddycam-boot-mode.service"
        ).unlink()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be enabled at multi-user.target", result.stdout)

    def test_camera_target_enabled_fails(self):
        root = self._root()
        os.symlink(
            "../pibuddycam.target",
            self._systemd(root) / "multi-user.target.wants" / "pibuddycam.target",
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pibuddycam.target must NOT be enabled", result.stdout)

    def test_provisioning_enabled_fails(self):
        root = self._root()
        os.symlink(
            "../pibuddycam-provisioning.service",
            self._systemd(root) / "multi-user.target.wants"
            / "pibuddycam-provisioning.service",
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pibuddycam-provisioning.service must NOT be enabled", result.stdout)

    def test_camera_unit_enabled_fails(self):
        root = self._root()
        os.symlink(
            "../rpicam-source.service",
            self._systemd(root) / "multi-user.target.wants" / "rpicam-source.service",
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("camera units enabled at multi-user.target", result.stdout)

    def test_admin_not_bound_to_camera_target_fails(self):
        root = self._root()
        systemd = self._systemd(root)
        (systemd / "pibuddycam.target.wants" / "pibuddycam-admin.service").unlink()
        unit = systemd / "pibuddycam-admin.service"
        unit.write_text(
            unit.read_text(encoding="utf-8").replace(
                "WantedBy=pibuddycam.target", "WantedBy=multi-user.target"
            ),
            encoding="utf-8",
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "pibuddycam-admin.service must be enabled under pibuddycam.target.wants",
            result.stdout,
        )

    def test_admin_enabled_at_multi_user_fails(self):
        root = self._root()
        os.symlink(
            "../pibuddycam-admin.service",
            self._systemd(root) / "multi-user.target.wants" / "pibuddycam-admin.service",
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not be enabled at multi-user.target", result.stdout)

    def test_provisioning_without_conflicts_fails(self):
        root = self._root()
        unit = self._systemd(root) / "pibuddycam-provisioning.service"
        unit.write_text(
            unit.read_text(encoding="utf-8").replace(
                "Conflicts=pibuddycam.target\n", ""
            ),
            encoding="utf-8",
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must Conflicts= pibuddycam.target", result.stdout)

    def test_boot_mode_without_data_ready_after_fails(self):
        root = self._root()
        unit = self._systemd(root) / "pibuddycam-boot-mode.service"
        unit.write_text(
            unit.read_text(encoding="utf-8").replace(
                "After=data-ready.target", "After=network.target"
            ),
            encoding="utf-8",
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be After= data-ready.target", result.stdout)


class PrivilegedHelperValidationTests(unittest.TestCase):
    """B3/H3: the root helper must not be service-account-writable."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.image = make_image(Path(cls.tmp) / "image.img")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _root(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return make_rootfs(tmp)

    def _systemd(self, root):
        return Path(root) / "etc" / "systemd" / "system"

    def test_good_rootfs_reports_helper_checks(self):
        root = self._root()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "pibuddycam-priv is root:root and not group/world-writable", result.stdout
        )
        self.assertIn("/etc/sudoers.d/pibuddycam is root:root mode 0440", result.stdout)
        self.assertIn(
            "sudoers pins env_reset/secure_path and only the fixed-verb helper",
            result.stdout,
        )
        self.assertIn(
            "/opt/pibuddycam is root-owned and not group/world-writable",
            result.stdout,
        )

    def test_world_writable_helper_fails(self):
        root = self._root()
        (root / "usr" / "libexec" / "pibuddycam" / "pibuddycam-priv").chmod(0o777)
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("group/world-writable", result.stdout)

    def test_missing_helper_fails(self):
        root = self._root()
        (root / "usr" / "libexec" / "pibuddycam" / "pibuddycam-priv").unlink()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pibuddycam-priv helper is missing", result.stdout)

    def test_wrong_sudoers_mode_fails(self):
        root = self._root()
        (root / "etc" / "sudoers.d" / "pibuddycam").chmod(0o644)
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be root:root mode 0440", result.stdout)

    def test_opt_owned_by_prusa_cam_fails(self):
        root = self._root()
        # Map the synthetic pibuddycam account onto the test runner's uid so the
        # /opt tree (owned by the runner) looks service-account-owned.
        passwd = root / "etc" / "passwd"
        passwd.write_text(
            passwd.read_text(encoding="utf-8").replace(
                f":{SYNTH_SERVICE_UID}:{SYNTH_SERVICE_GID}:PiBuddyCam:",
                f":{SYNTH_ROOT_UID}:{SYNTH_ROOT_GID}:PiBuddyCam:",
            ),
            encoding="utf-8",
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("/opt/pibuddycam must not be owned by pibuddycam", result.stdout)

    def test_camera_target_without_admin_wants_fails(self):
        root = self._root()
        unit = self._systemd(root) / "pibuddycam.target"
        unit.write_text(
            unit.read_text(encoding="utf-8").replace(" pibuddycam-admin.service", ""),
            encoding="utf-8",
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "pibuddycam.target must Wants= pibuddycam-admin.service", result.stdout
        )


class PythonVenvValidationTests(unittest.TestCase):
    """WP-R3/AC-14: the runtime venv + hash-locked requirements.lock."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.image = make_image(Path(cls.tmp) / "image.img")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _root(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return make_rootfs(tmp)

    @staticmethod
    def _app(root):
        return Path(root) / "opt" / "pibuddycam"

    def test_good_rootfs_reports_venv_and_lock(self):
        root = self._root()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "/opt/pibuddycam/venv/bin/python is root-owned", result.stdout
        )
        self.assertIn(
            "/opt/pibuddycam/requirements.lock is root:root mode 0644",
            result.stdout,
        )
        self.assertIn("each with a sha256 hash", result.stdout)
        self.assertIn("venv site-packages contains aiohttp", result.stdout)
        self.assertIn("no pip wheel cache inside venv", result.stdout)
        self.assertIn("no unpinned requirements.txt", result.stdout)

    def test_missing_venv_fails(self):
        root = self._root()
        shutil.rmtree(self._app(root) / "venv")
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("venv/bin/python is missing", result.stdout)

    def test_missing_lock_fails(self):
        root = self._root()
        (self._app(root) / "requirements.lock").unlink()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requirements.lock is missing", result.stdout)

    def test_unhashed_lock_fails(self):
        root = self._root()
        (self._app(root) / "requirements.lock").write_text(
            "aiohttp==3.14.3\n"
            f"python-socketio==5.17.0 \\\n    --hash=sha256:{'0' * 64}\n"
            f"paho-mqtt==2.1.0 \\\n    --hash=sha256:{'1' * 64}\n",
            encoding="utf-8",
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("without a sha256 hash", result.stdout)
        self.assertIn("aiohttp", result.stdout)

    def test_lock_missing_direct_dependency_fails(self):
        root = self._root()
        (self._app(root) / "requirements.lock").write_text(
            f"aiohttp==3.14.3 \\\n    --hash=sha256:{'0' * 64}\n"
            f"python-socketio==5.17.0 \\\n    --hash=sha256:{'1' * 64}\n",
            encoding="utf-8",
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing direct dependencies", result.stdout)
        self.assertIn("paho-mqtt", result.stdout)

    def test_group_writable_venv_python_fails(self):
        root = self._root()
        (self._app(root) / "venv" / "bin" / "python").chmod(0o775)
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("venv python must not be group/world-writable", result.stdout)

    def test_wheel_cache_fails(self):
        root = self._root()
        wheel = (
            self._app(root) / "venv" / "lib" / "python3.13"
            / "site-packages" / "aiohttp" / "cached.whl"
        )
        wheel.write_text("synthetic wheel\n", encoding="utf-8")
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pip wheel cache", result.stdout)


@unittest.skipUnless(HAS_BOOT_TOOLS, "mkfs.fat/mcopy/mtype not available")
class BootPartitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.image = make_image(Path(self.tmp) / "image.img")

    def test_cmdline_with_overlayroot_passes(self):
        boot = make_boot_image(
            Path(self.tmp) / "boot.vfat",
            "console=serial0,115200 root=PARTUUID=b33dcafe-02 "
            "rootfstype=ext4 rootwait overlayroot=tmpfs\n",
        )
        result = run_validator("--image", self.image, "--boot-image", boot)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("BOOT cmdline sets overlayroot=", result.stdout)

    def test_cmdline_without_overlayroot_fails(self):
        boot = make_boot_image(
            Path(self.tmp) / "boot.vfat",
            "console=serial0,115200 root=PARTUUID=b33dcafe-02 "
            "rootfstype=ext4 rootwait\n",
        )
        result = run_validator("--image", self.image, "--boot-image", boot)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BOOT cmdline does not set overlayroot=", result.stdout)

    def test_cmdline_with_usb_host_mode_passes(self):
        boot = make_boot_image(
            Path(self.tmp) / "boot-usb.vfat",
            "console=serial0,115200 root=PARTUUID=b33dcafe-02 rootwait "
            "overlayroot=tmpfs\n",
        )
        result = run_validator("--image", self.image, "--boot-image", boot)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("enables Pi Zero 2 W USB host mode", result.stdout)

    def test_cmdline_without_usb_host_mode_fails(self):
        boot = make_boot_image(
            Path(self.tmp) / "boot-nousb.vfat",
            "console=serial0,115200 root=PARTUUID=b33dcafe-02 rootwait "
            "overlayroot=tmpfs\n",
            config="[all]\ncamera_auto_detect=1\n",
        )
        result = run_validator("--image", self.image, "--boot-image", boot)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("lacks [pi02] dtoverlay=dwc2,dr_mode=host", result.stdout)

    def test_missing_boot_image_skips_and_passes(self):
        result = run_validator("--image", self.image)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("no --boot-image", result.stdout)


@unittest.skipUnless(HAS_PERSIST_TOOLS, "mke2fs/debugfs not available")
class PersistPartitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.image = make_image(Path(cls.tmp) / "image.img")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _root(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return make_rootfs(tmp)

    def test_seeded_layout_passes(self):
        tree = make_persist_tree(
            Path(self.tmp) / "seeded", REQUIRED_DATA_DIRS
        )
        persist = make_persist_image(
            Path(self.tmp) / "seeded.ext4", tree,
            uid=SYNTH_SERVICE_UID, gid=SYNTH_SERVICE_GID,
        )
        root = self._root()
        result = run_validator(
            "--image", self.image, "--persist-image", persist,
            "--mount-root", root,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PERSIST has the seeded /data layout", result.stdout)
        self.assertIn("PERSIST directories owned correctly", result.stdout)

    def test_missing_seeded_layout_fails(self):
        tree = make_persist_tree(Path(self.tmp) / "empty", [])
        persist = make_persist_image(Path(self.tmp) / "empty.ext4", tree)
        result = run_validator(
            "--image", self.image, "--persist-image", persist
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PERSIST missing seeded directories", result.stdout)

    def test_missing_persist_image_skips_and_passes(self):
        result = run_validator("--image", self.image)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("no --persist-image", result.stdout)


class UpdaterImageValidationTests(unittest.TestCase):
    """WP-R4b: embedded public key + updater units/timer."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.image = make_image(Path(cls.tmp) / "image.img")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _root(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return make_rootfs(tmp)

    def _systemd(self, root):
        return Path(root) / "etc" / "systemd" / "system"

    def _key(self, root):
        return (
            Path(root) / "usr" / "share" / "pibuddycam"
            / "pibuddycam-release.pub"
        )

    def test_good_rootfs_reports_updater_checks(self):
        root = self._root()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("pibuddycam-release.pub is root:root mode 0644", result.stdout)
        self.assertIn("pibuddycam-release.pub contains no private key material",
                      result.stdout)
        self.assertIn("pibuddycam-updater.timer is enabled at multi-user.target",
                      result.stdout)
        self.assertIn("pibuddycam-updater.service is not separately enabled",
                      result.stdout)
        self.assertIn("pibuddycam-updater.service runs updater_install.py",
                      result.stdout)
        self.assertIn("pibuddycam.target Wants= pibuddycam-updater.timer",
                      result.stdout)

    def test_missing_key_fails(self):
        root = self._root()
        self._key(root).unlink()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pibuddycam-release.pub is missing", result.stdout)

    def test_wrong_key_mode_fails(self):
        root = self._root()
        self._key(root).chmod(0o600)
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pibuddycam-release.pub must be mode 0644", result.stdout)

    def test_prusa_cam_owned_key_fails(self):
        root = self._root()
        passwd = root / "etc" / "passwd"
        passwd.write_text(
            passwd.read_text(encoding="utf-8").replace(
                f":{SYNTH_SERVICE_UID}:{SYNTH_SERVICE_GID}:PiBuddyCam:",
                f":{SYNTH_ROOT_UID}:{SYNTH_ROOT_GID}:PiBuddyCam:",
            ),
            encoding="utf-8",
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pibuddycam-release.pub must not be owned by pibuddycam",
                      result.stdout)

    def test_missing_updater_service_fails(self):
        root = self._root()
        (self._systemd(root) / "pibuddycam-updater.service").unlink()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pibuddycam-updater.service is missing", result.stdout)

    def test_timer_not_enabled_fails(self):
        root = self._root()
        (
            self._systemd(root) / "multi-user.target.wants"
            / "pibuddycam-updater.timer"
        ).unlink()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pibuddycam-updater.timer must be enabled", result.stdout)

    def test_updater_service_enabled_fails(self):
        root = self._root()
        os.symlink(
            "../pibuddycam-updater.service",
            self._systemd(root) / "multi-user.target.wants"
            / "pibuddycam-updater.service",
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pibuddycam-updater.service must not be enabled", result.stdout)

    def test_good_rootfs_reports_install_unit_checks(self):
        root = self._root()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "pibuddycam-updater-install.service is installed", result.stdout)
        self.assertIn(
            "pibuddycam-updater-install.service runs updater_install.py install",
            result.stdout)
        self.assertIn(
            "pibuddycam-updater-install.service is After= and Requires= data-ready.target",
            result.stdout)
        self.assertIn(
            "pibuddycam-updater-install.service is not enabled", result.stdout)
        self.assertIn(
            "pibuddycam.target does not pull pibuddycam-updater-install.service",
            result.stdout)

    def test_missing_install_unit_fails(self):
        root = self._root()
        (self._systemd(root) / "pibuddycam-updater-install.service").unlink()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pibuddycam-updater-install.service is missing", result.stdout)

    def test_enabled_install_unit_fails(self):
        root = self._root()
        os.symlink(
            "../pibuddycam-updater-install.service",
            self._systemd(root) / "multi-user.target.wants"
            / "pibuddycam-updater-install.service",
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "pibuddycam-updater-install.service must not be enabled", result.stdout)

    def test_install_unit_in_camera_target_fails(self):
        root = self._root()
        target = self._systemd(root) / "pibuddycam.target.wants"
        if not target.exists():
            target.mkdir(parents=True)
        os.symlink(
            "../pibuddycam-updater-install.service",
            target / "pibuddycam-updater-install.service",
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "must not be enabled (triggered only via the helper)", result.stdout)


class LauncherWiringValidationTests(unittest.TestCase):
    """WP-R4c: runtime units exec the launcher; launcher prefers the release."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.image = make_image(Path(cls.tmp) / "image.img")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _root(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return make_rootfs(tmp)

    def _systemd(self, root):
        return Path(root) / "etc" / "systemd" / "system"

    def _launcher(self, root):
        return Path(root) / "opt" / "pibuddycam" / "launcher.sh"

    def test_good_rootfs_reports_launcher_wiring(self):
        root = self._root()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("pibuddycam.service execs /opt/pibuddycam/launcher.sh main.py",
                      result.stdout)
        self.assertIn(
            "pibuddycam-rtsp.service execs /opt/pibuddycam/launcher.sh rtsp_server.py",
            result.stdout,
        )
        self.assertIn(
            "pibuddycam-ha-rtsp.service execs /opt/pibuddycam/launcher.sh rtsp_server.py",
            result.stdout,
        )
        self.assertIn(
            "pibuddycam-admin.service execs /opt/pibuddycam/launcher.sh admin_app.py",
            result.stdout,
        )
        self.assertIn(
            "launcher prefers the per-release venv with a factory fallback",
            result.stdout,
        )

    def test_unit_execing_factory_venv_directly_fails(self):
        root = self._root()
        unit = self._systemd(root) / "pibuddycam.service"
        unit.write_text(
            unit.read_text(encoding="utf-8").replace(
                "ExecStart=/opt/pibuddycam/launcher.sh main.py",
                "ExecStart=/opt/pibuddycam/venv/bin/python main.py",
            ),
            encoding="utf-8",
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must ExecStart=/opt/pibuddycam/launcher.sh main.py",
                      result.stdout)

    def test_wrong_script_in_unit_fails(self):
        root = self._root()
        unit = self._systemd(root) / "pibuddycam-rtsp.service"
        unit.write_text(
            unit.read_text(encoding="utf-8").replace(
                "ExecStart=/opt/pibuddycam/launcher.sh rtsp_server.py",
                "ExecStart=/opt/pibuddycam/launcher.sh main.py",
            ),
            encoding="utf-8",
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must ExecStart=/opt/pibuddycam/launcher.sh rtsp_server.py",
                      result.stdout)

    def test_launcher_without_release_preference_fails(self):
        root = self._root()
        launcher = self._launcher(root)
        launcher.write_text(
            "#!/bin/bash\nexec /opt/pibuddycam/venv/bin/python \"$@\"\n",
            encoding="utf-8",
        )
        launcher.chmod(0o755)
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "launcher must prefer the per-release venv and fall back to the factory venv",
            result.stdout,
        )


class AdminWebAssetValidationTests(unittest.TestCase):
    """WP-UI1/AC-2: the factory image ships the local admin shell + assets."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.image = make_image(Path(cls.tmp) / "image.img")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _root(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return make_rootfs(tmp)

    def _web(self, root):
        return Path(root) / "opt" / "pibuddycam" / "web"

    def test_good_rootfs_reports_web_assets(self):
        root = self._root()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "local admin web assets present under /opt/pibuddycam/web",
            result.stdout,
        )

    def test_missing_web_assets_fail(self):
        root = self._root()
        shutil.rmtree(self._web(root))
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "local admin web assets missing under /opt/pibuddycam/web",
            result.stdout,
        )

    def test_missing_shell_fails(self):
        root = self._root()
        (self._web(root) / "index.html").unlink()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("local admin web assets missing", result.stdout)

    def test_missing_favicon_fails(self):
        # favicon.svg is in the served allowlist, so the image must ship it.
        root = self._root()
        (self._web(root) / "favicon.svg").unlink()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("local admin web assets missing", result.stdout)


class AdminTlsValidationTests(unittest.TestCase):
    """Appliance image/security defect: boot-provisioned admin TLS wiring."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.image = make_image(Path(cls.tmp) / "image.img")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _root(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return make_rootfs(tmp)

    def _app(self, root):
        return Path(root) / "opt" / "pibuddycam"

    def _systemd(self, root):
        return Path(root) / "etc" / "systemd" / "system"

    def test_good_rootfs_reports_admin_tls_wiring(self):
        root = self._root()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "admin TLS provisioning module present", result.stdout)
        self.assertIn(
            "openssl present for boot-time admin TLS provisioning", result.stdout)
        self.assertIn(
            "admin_app.py fails closed when admin-mode TLS is unavailable",
            result.stdout,
        )
        self.assertIn(
            "pibuddycam-admin.service reads the boot-provisioned", result.stdout)
        self.assertIn(
            "pibuddycam-provisioning.service keeps the setup portal plain HTTP",
            result.stdout,
        )

    def test_missing_admin_tls_module_fails(self):
        root = self._root()
        (self._app(root) / "admin_tls.py").unlink()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("admin TLS provisioning module missing", result.stdout)

    def test_missing_openssl_fails(self):
        root = self._root()
        (root / "usr" / "bin" / "openssl").unlink()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("openssl missing", result.stdout)

    def test_admin_app_without_fail_closed_wiring_fails(self):
        root = self._root()
        (self._app(root) / "admin_app.py").write_text(
            "# no TLS resolver here\n", encoding="utf-8")
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must fail closed without admin-mode TLS", result.stdout)

    def test_pi_persist_must_run_before_admin(self):
        root = self._root()
        unit = self._systemd(root) / "pi-persist.service"
        unit.write_text(
            unit.read_text(encoding="utf-8").replace(
                " pibuddycam-admin.service\n", "\n"),
            encoding="utf-8",
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pibuddycam-admin.service", result.stdout)

    def test_setup_portal_with_tls_env_fails(self):
        root = self._root()
        unit = self._systemd(root) / "pibuddycam-provisioning.service"
        unit.write_text(
            unit.read_text(encoding="utf-8") + "Environment=ADMIN_TLS_CERT=/x\n",
            encoding="utf-8",
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("setup portal", result.stdout)


class RuntimeDirectoryValidationTests(unittest.TestCase):
    """WP-UI2/AC-4: the factory unit creates the service-owned runtime dir."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.image = make_image(Path(cls.tmp) / "image.img")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _root(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return make_rootfs(tmp)

    def _systemd(self, root):
        return Path(root) / "etc" / "systemd" / "system"

    def test_good_rootfs_reports_the_runtime_directory(self):
        root = self._root()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "pibuddycam.service creates the service-owned runtime directory",
            result.stdout,
        )

    def test_missing_runtime_directory_fails(self):
        root = self._root()
        unit = self._systemd(root) / "pibuddycam.service"
        unit.write_text(
            unit.read_text(encoding="utf-8").replace(
                "RuntimeDirectory=pibuddycam\n", ""),
            encoding="utf-8",
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RuntimeDirectory=pibuddycam", result.stdout)


class PrivateKeyScanTests(unittest.TestCase):
    """The key scan must ignore library fixtures and public certs."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.image = make_image(Path(cls.tmp) / "image.img")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _root(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return make_rootfs(tmp)

    def test_selftest_fixture_is_not_flagged(self):
        root = self._root()
        fixture = (
            root / "usr" / "lib" / "python3" / "dist-packages" / "Cryptodome"
            / "SelfTest" / "Cipher" / "test_vectors.py"
        )
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text(
            "KEY = '-----BEGIN PRIVATE KEY-----\\nsynthetic\\n"
            "-----END PRIVATE KEY-----\\n'\n",
            encoding="utf-8",
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("no private key material", result.stdout)

    def test_public_cacert_is_not_flagged(self):
        root = self._root()
        cacert = (
            root / "usr" / "lib" / "python3" / "dist-packages" / "pip"
            / "_vendor" / "certifi" / "cacert.pem"
        )
        cacert.parent.mkdir(parents=True, exist_ok=True)
        cacert.write_text(
            "-----BEGIN CERTIFICATE-----\nsynthetic-public-ca\n"
            "-----END CERTIFICATE-----\n",
            encoding="utf-8",
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("no private key material", result.stdout)

    def test_openssh_private_key_content_is_flagged(self):
        root = self._root()
        (root / "opt" / "pibuddycam" / "server.key").write_text(
            "-----BEGIN OPENSSH PRIVATE KEY-----\nsynthetic\n"
            "-----END OPENSSH PRIVATE KEY-----\n",
            encoding="utf-8",
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("private key material", result.stdout)

    def test_id_rsa_filename_is_flagged(self):
        root = self._root()
        (root / "opt" / "pibuddycam" / "id_rsa").write_text(
            "-----BEGIN OPENSSH PRIVATE KEY-----\nsynthetic\n"
            "-----END OPENSSH PRIVATE KEY-----\n",
            encoding="utf-8",
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("private key", result.stdout)


if __name__ == "__main__":
    unittest.main()
