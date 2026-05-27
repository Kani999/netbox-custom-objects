"""
Related-object tabs for netbox-custom-objects — EXPERIMENT: local-only refresh.

This branch removes the Redis-shared version counter and middleware that the
``feature/related-object-tabs-v2`` branch uses to propagate tab-registry
changes between WSGI worker processes.  The goal is to empirically observe
the Stage-1 failure mode from the design discussion: tab mutations made in
one worker do not reach other workers, so users see inconsistent tab
visibility depending on which worker handled their request.

What still works here:

* ``registry.register_tabs()`` — initial registration in
  ``CustomObjectsPluginConfig.ready()``.
* ``local_refresh()`` — called by signal handlers in the process that
  performed a COT/COTField mutation.  Tears down the local tab registry
  and re-runs ``register_tabs()``.  No cross-process signal.

What is intentionally missing compared to ``feature/related-object-tabs-v2``:

* ``_tab_registry_version`` process-local counter.
* ``_REDIS_KEY`` shared monotonic counter in Redis.
* ``refresh_if_stale()`` middleware entry point.
* ``TabRegistryRefreshMiddleware``.
* Redis seeding on startup in ``ready()``.

Run NetBox under ``gunicorn --workers 2+`` (not the dev server) to observe
the inconsistency: PATCH ``show_dedicated_tab=true`` on a CustomObjectType
and reload a related detail page repeatedly — only the worker that handled
the PATCH will show the new tab.
"""

import logging
import threading

logger = logging.getLogger(__name__)

# Serialises the purge + re-register sequence in _do_refresh() against
# concurrent threads within the same process.  RLock so a signal handler
# that fires while we're holding the lock can re-enter without deadlock.
_global_lock = threading.RLock()


def local_refresh() -> None:
    """
    Refresh the in-process tab registry unconditionally.

    Called from signal handlers in the process that just performed a COT or
    COTField mutation.  Only the calling process is updated — other WSGI
    workers will continue to serve the old registry until their next restart.
    """
    with _global_lock:
        try:
            from netbox_custom_objects.related_tabs.registry import _do_refresh

            _do_refresh()
        except Exception:
            logger.exception('related_tabs local refresh failed')
