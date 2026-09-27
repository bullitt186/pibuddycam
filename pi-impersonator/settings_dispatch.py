"""Single-owner settings mutation dispatch (WP-UI3; AC-4/AC-5).

The live ``SettingsCoordinator`` is owned by the camera runtime's asyncio event
loop: Prusa signaling, MQTT, and startup restore all mutate it from loop
callbacks. The admin Web UI reaches the same coordinator over the runtime IPC
socket, whose handler runs on a *separate* worker thread. Calling the
coordinator directly from that thread would race the loop's mutations.

This module is the small, host-testable boundary that fixes the ownership
problem without an ad-hoc lock:

* every admin mutation is marshalled onto the owning event loop with
  :func:`asyncio.run_coroutine_threadsafe`, so the coordinator is only ever
  mutated by one thread (the loop);
* the worker thread waits with a bounded deadline, so a wedged loop can never
  pin an IPC worker forever; and
* the outcome is an authoritative envelope -- on success or coordinator
  rejection it carries the coordinator's snapshot; on a timeout/closed loop it
  reports a bounded, truthful failure instead of a fabricated success.

A lock that merely guarded the coordinator would not be sound here: the
event-loop mutation paths would not take it, and a lock held across a blocking
service restart would stall the loop. Marshalling to the owner is the single
mechanism.

Stdlib only, and no file/network/thread side effects on import.
"""
import concurrent.futures
import logging

log = logging.getLogger('pibuddycam.settings_dispatch')

#: Default bounded wait for a mutation to run on the owning loop. Long enough
#: for a service restart triggered by a settings change, short enough that an
#: IPC worker is never pinned indefinitely.
DEFAULT_MUTATION_TIMEOUT = 15.0


class EventLoopMutationDispatcher:
    """Run coordinator mutations on the owning event loop, from any thread.

    ``loop`` must be the running loop that owns ``coordinator``. The dispatcher
    itself holds no mutable state, so it is safe to share across the runtime IPC
    worker pool.
    """

    def __init__(self, coordinator, loop, timeout=DEFAULT_MUTATION_TIMEOUT):
        if loop is None or not hasattr(loop, 'call_soon_threadsafe'):
            raise ValueError('a running asyncio event loop is required')
        self._coordinator = coordinator
        self._loop = loop
        self._timeout = max(0.1, float(timeout))

    @property
    def timeout(self):
        return self._timeout

    def apply(self, field, value):
        """Apply one mutation on the owning loop; return a bounded envelope.

        Safe to call from the runtime IPC worker thread. Never raises: a closed
        loop, a coordinator failure, or a deadline expiry becomes an
        ``ok: False`` envelope. On a timeout the mutation may still complete on
        the loop later, so the reason says so rather than claiming no change.
        """
        future = concurrent.futures.Future()

        def _run_on_loop():
            try:
                result = self._coordinator.apply_mutation(field, value)
            except Exception as e:  # noqa: BLE001 - report, never crash the loop
                future.set_exception(e)
            else:
                future.set_result(result)

        try:
            self._loop.call_soon_threadsafe(_run_on_loop)
        except RuntimeError:
            log.warning('settings_dispatch: event loop is not running')
            return self._failure('settings runtime is not accepting changes')

        # Retrieve any late exception so a post-timeout failure is not reported
        # as "never retrieved"; the caller already has its bounded failure.
        future.add_done_callback(lambda done: done.exception())

        try:
            result = future.result(timeout=self._timeout)
        except concurrent.futures.TimeoutError:
            log.warning('settings_dispatch: mutation deadline exceeded')
            return self._failure('settings update timed out; refresh to confirm')
        except Exception as e:  # noqa: BLE001 - the caller must get an envelope
            log.warning('settings_dispatch: mutation failed: %s', type(e).__name__)
            return self._failure('settings update failed')

        return {
            'ok': bool(getattr(result, 'ok', False)),
            'reason': getattr(result, 'reason', None) or '',
            'changed': list(getattr(result, 'changed', []) or []),
            'settings': getattr(result, 'state', {}) or {},
            'timed_out': False,
        }

    @staticmethod
    def _failure(reason):
        return {
            'ok': False,
            'reason': reason,
            'changed': [],
            'settings': {},
            'timed_out': False,
        }


__all__ = [
    'DEFAULT_MUTATION_TIMEOUT',
    'EventLoopMutationDispatcher',
]
