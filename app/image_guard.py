"""Refuse to run this application on a pre-rename (``prusa-cam``) image.

The project was renamed to PiBuddyCam: the service account, ``/data`` and
``/etc`` directories, units, privileged helper and updater config all changed
names, and only a reflashed image carries them. An application bundle is
installed over the air onto whatever image is present, so a new bundle could
land on an old image whose layout it cannot use.

The old image has ``/usr/share/prusa-buddy3d-camera`` and lacks
``/usr/share/pibuddycam``. The runtime entry points call
:func:`exit_if_legacy_image` first and exit non-zero there. The old image's own
updater then sees its post-install health probe fail and rolls back to the
previous release instead of leaving the device broken. A host checkout (tests,
development) has neither directory and is never affected.

Stdlib only; no side effects on import.
"""
import logging
import os
import sys

log = logging.getLogger('pibuddycam.guard')

IMAGE_SHARE_DIR = '/usr/share/pibuddycam'
LEGACY_IMAGE_SHARE_DIR = '/usr/share/prusa-buddy3d-camera'
#: Distinct exit status (EX_CONFIG) so the journal shows why the unit failed.
LEGACY_IMAGE_EXIT_CODE = 78


def legacy_image_detected(share_dir=IMAGE_SHARE_DIR, legacy_dir=LEGACY_IMAGE_SHARE_DIR):
    """True only on a pre-rename image: legacy marker present, new one absent."""
    return os.path.isdir(legacy_dir) and not os.path.isdir(share_dir)


def exit_if_legacy_image(share_dir=IMAGE_SHARE_DIR, legacy_dir=LEGACY_IMAGE_SHARE_DIR,
                         exit=sys.exit):
    """Exit with :data:`LEGACY_IMAGE_EXIT_CODE` on a pre-rename image."""
    if legacy_image_detected(share_dir, legacy_dir):
        message = ('this PiBuddyCam release needs a PiBuddyCam image; this device runs a '
                   'pre-rename image. Reflash the SD card with a current image.')
        log.error(message)
        print(f'pibuddycam: {message}', file=sys.stderr, flush=True)
        exit(LEGACY_IMAGE_EXIT_CODE)
