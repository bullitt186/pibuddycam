# `app/`: the PiBuddyCam runtime

Everything that runs on the device. It ships in the image as the factory copy
(`/opt/pibuddycam`) and as signed OTA releases (`/data/pibuddycam/releases/<version>`). How the
processes fit together is in [docs/architecture.md](../docs/architecture.md); tests and local
development are in [docs/development.md](../docs/development.md).

## Entry points

| Script | Started by |
|---|---|
| `main.py` | `pibuddycam.service`: the camera runtime |
| `admin_app.py` | `pibuddycam-admin.service` (console) and `pibuddycam-provisioning.service` (setup wizard) |
| `camera_source.py` → `stream_mux.py` | `rpicam-source.service` |
| `rtsp_server.py` | `pibuddycam-rtsp.service`, `pibuddycam-ha-rtsp.service` |
| `persist_restore.py` | `pi-persist.service` (root, at boot) |
| `updater_install.py` | `pibuddycam-updater.service`, `pibuddycam-updater-install.service` (always the factory copy) |
| `boot_mode.py`, `data_ready.py`, `hotspot_ctl.py`, `wifi_station.py`, `bootlog.sh` | image units and the privileged helper |
| `network_apply.py` | `pibuddycam-network-apply.service` (root, started only by the helper's `network-apply` verb) |
| `network_watchdog.py` | `pibuddycam-network-watchdog.service`, started every minute by its timer (root) |
| `ntp_apply.py` | the helper's `ntp-apply` verb, the NetworkManager dispatcher script and `persist_restore.py` (root) |

Unit files are in `systemd/`. The web console's static files are in `web/`.

## Modules

| Area | Modules |
|---|---|
| Prusa Connect protocol | `signaling.py` (Socket.IO), `auth.py`, `proto.py` (nanopb wire format), `status.py`, `info_body.py`, `info_service.py`, `upload.py`, `http_result.py`, `trigger.py`, `features.py`, `identity.py` (fingerprint), `network.py`, `timezone.py`, `ota.py` |
| Video | `camera_source.py`, `stream_mux.py`, `camera.py` (JPEG from the mux), `quality.py`, `quality_control.py`, `rotation.py`, `rtsp_server.py`, `rtsp_config.py`, `rtsp_control.py` |
| WebRTC | `webrtc.py` (Connect), `webrtc_control.py`, `webrtc_lifecycle.py`, `local_webrtc.py` + `local_webrtc_signaling.py` (console live view) |
| State and settings | `state.py`, `settings_coordinator.py`, `settings_dispatch.py`, `settings_store.py`, `config_schema.py`, `expert_config.py`, `scheduling.py` |
| Local integrations | `local_http.py`, `onvif_facade.py`, `onvif_discovery.py`, `mqtt_service.py`, `mqtt_state.py`, `mqtt_topics.py`, `mqtt_probe.py` |
| Timelapse | `timelapse.py` (frames, AVI, per-print sessions), `media_library.py`, `media_build.py` (with a retry queue for session builds), `gpio_trigger.py` + `gpio_pins.py` (Prusa GPIO Hackerboard trigger), `gpio_selftest.py` (hand-run hardware check) |
| Network and time | `network_settings.py` (validation, live status, apply control), `network_apply.py`, `wifi_station.py`, `network_watchdog.py`, `ntp_apply.py` |
| Console and setup | `admin_app.py`, `admin_http.py` (routes, security policy, shared helpers) with its handler mixins `admin_http_media.py`, `admin_http_system.py`, `admin_http_network.py`, `admin_http_settings.py`, `admin_auth.py`, `admin_tls.py`, `dashboard.py`, `live_monitor.py`, `diagnostics.py`, `runtime_ipc.py`, `setup_wizard.py`, `provisioning.py`, `camera_probe.py`, `hotspot.py`, `ssh_control.py`, `factory_reset.py`, `recovery.py` |
| Appliance lifecycle | `boot_mode.py`, `data_ready.py`, `persist_restore.py`, `migrations.py`, `privileged.py`, `device_control.py`, `updater.py`, `updater_install.py`, `update_control.py`, `app_version.py`, `app_metrics.py`, `image_guard.py` |

## Conventions

- **Standard library first.** Third-party packages come only from the hash-locked
  `image/requirements.lock`. GStreamer and libcamera are system packages from the image.
- **Testable cores.** Protocol and policy logic lives in pure, host-testable modules, and
  GStreamer, aiohttp and systemd stay at the edges. The tests in `tests/app/` run without any Pi
  packages.
- **No privilege in the app.** Anything that needs root goes through `privileged.py` to the image's
  fixed-verb helper.
