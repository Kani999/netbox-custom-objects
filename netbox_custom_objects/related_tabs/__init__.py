"""
Related-object tabs for netbox-custom-objects.

Adds a "Custom Objects" combined tab and per-CustomObjectType typed tabs to
NetBox object detail pages. Vendored from the standalone netbox-custom-objects-tab
plugin and integrated as a subpackage so the feature ships with the core plugin
itself.

Two public entry points:

* ``registry.register_tabs()`` — initial registration, called once from
  ``CustomObjectsPluginConfig.ready()``.
* ``refresh_if_stale()`` / ``force_local_refresh()`` — hot-reload, called by
  ``TabRegistryRefreshMiddleware`` (every request) and the signal handlers in
  ``signals`` (after COT/COTField save or delete).

Hot-reload propagation across WSGI worker processes uses a Redis-shared
monotonic counter (key ``nbco:tab_registry_version``).  Each process tracks
its own ``_tab_registry_version``; whenever the Redis counter advances past
the local value, the process re-runs ``register_tabs()`` and clears the URL
caches before the next view dispatches.
"""

import logging
import threading

logger = logging.getLogger(__name__)

# Process-local monotonic counter.  Compared against the Redis value to
# decide whether this process needs to re-run register_tabs().
_tab_registry_version: int = 0

# Serialises (a) reads/writes of _tab_registry_version and (b) the
# purge + re-register sequence in _do_refresh().  RLock so a signal handler
# that fires while we're holding the lock can re-enter without deadlock.
_global_lock = threading.RLock()

# Redis key shared across processes.  Bumped by the post_save/post_delete
# signal handlers in the process that handled the mutation; observed by other
# processes via the middleware.
_REDIS_KEY = 'nbco:tab_registry_version'


def _get_remote_version() -> int:
    """Return the Redis-shared registry version (0 if absent or Redis is down)."""
    from django.core.cache import cache

    try:
        value = cache.get(_REDIS_KEY)
    except Exception:
        logger.exception('failed to read remote tab-registry version')
        return 0
    return int(value or 0)


def _bump_remote_version() -> int:
    """
    Atomically increment the Redis counter and return the new value.

    Uses ``cache.add`` to seed the key when absent (returning False if the key
    already exists), then ``cache.incr``.  Two concurrent first-bumpers may
    both ``add(0)``; the second's ``add`` is a no-op and both ``incr`` calls
    serialise in Redis, so they end up with consecutive values rather than
    racing past each other.
    """
    from django.core.cache import cache

    try:
        cache.add(_REDIS_KEY, 0, timeout=None)
        return int(cache.incr(_REDIS_KEY))
    except Exception:
        logger.exception('failed to bump remote tab-registry version')
        # Best-effort fallback: return local+1 so refresh_if_stale still fires
        # in-process even if Redis is unavailable.
        return _tab_registry_version + 1


def refresh_if_stale() -> bool:
    """
    Re-register tabs if our local version is behind the Redis counter.

    Called from ``TabRegistryRefreshMiddleware`` on every request and from
    the signal handlers in the process that mutated the COT.  The fast path
    (when versions match) is a single Redis GET and no lock contention.

    Returns True if a refresh actually ran, False otherwise.
    """
    global _tab_registry_version
    remote = _get_remote_version()
    if remote <= _tab_registry_version:
        return False

    with _global_lock:
        # Re-check after acquiring the lock: another thread in this process
        # may have just refreshed and advanced the local version.
        if remote <= _tab_registry_version:
            return False
        try:
            from netbox_custom_objects.related_tabs.registry import _do_refresh

            _do_refresh()
        except Exception:
            logger.exception('related_tabs hot-reload refresh failed')
            return False
        _tab_registry_version = remote
        return True


def force_local_refresh() -> int:
    """
    Refresh local registry unconditionally and bump Redis so peers refresh.

    Called from signal handlers in the process that just performed a COT or
    COTField mutation.  Always refreshes in-process (the process that handled
    the mutation will see the change on its own next view dispatch) AND bumps
    the Redis counter so other processes' middleware notices on next request.

    Returns the new local/Redis version (which are equal after this call).
    """
    global _tab_registry_version
    with _global_lock:
        try:
            from netbox_custom_objects.related_tabs.registry import _do_refresh

            _do_refresh()
        except Exception:
            logger.exception('related_tabs hot-reload refresh failed')
            return _tab_registry_version
        _tab_registry_version = _bump_remote_version()
        return _tab_registry_version
