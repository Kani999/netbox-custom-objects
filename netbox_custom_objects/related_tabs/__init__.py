"""
Related-object tabs for netbox-custom-objects.

Adds a "Custom Objects" combined tab and per-CustomObjectType typed tabs to
NetBox object detail pages. Vendored from the standalone netbox-custom-objects-tab
plugin and integrated as a subpackage so the feature ships with the core plugin
itself.

Three public entry points:

* ``registry.register_tabs()`` — initial registration, called once from
  ``CustomObjectsPluginConfig.ready()``.
* ``seed_local_state()`` — captures the DB invalidation token after the initial
  ``register_tabs()`` so the first request doesn't trigger an extra refresh.
* ``refresh_if_stale()`` / ``force_local_refresh()`` — hot-reload, called by
  ``TabRegistryRefreshMiddleware`` (every request) and the signal handlers in
  ``signals`` (after COT save or delete).

Cross-worker hot-reload propagation rides on the same ``cache_timestamp``
invalidation token that ``CustomObjectType._model_cache`` already uses — see
``models.py``.  Each worker process records a snapshot
``(MAX(cache_timestamp), COUNT(*))`` over ``CustomObjectType``; on the next
request the middleware compares that to a freshly-aggregated value from the DB
and re-runs ``register_tabs()`` if they differ.

Why both ``MAX`` and ``COUNT``: ``MAX(cache_timestamp)`` alone cannot detect
deletion of the highest-timestamp row (MAX may drop or stay flat); ``COUNT``
catches that case.  Together they form an invalidation token that covers
create, update, and delete — including the create→delete→create cycle where
COUNT returns to the prior value while MAX moves forward.

No Redis, no separate cache backend: the same DB column that gates the model
cache is the gate for the tab registry, so the invariant is documented and
tested in one place.
"""

import logging
import threading

logger = logging.getLogger(__name__)

# Per-worker snapshot of (MAX(cache_timestamp), COUNT(*)) over CustomObjectType
# at the time of the most recent successful refresh.  ``None`` is a sentinel for
# "never refreshed in this process"; the first comparison forces a refresh.
_last_seen_state: tuple | None = None

# Serialises both reads/writes of ``_last_seen_state`` and the purge +
# re-register sequence in ``_do_refresh()``.  RLock so a signal handler that
# fires while we're holding the lock can re-enter without deadlock.
_global_lock = threading.RLock()


def _compute_state():
    """
    Return ``(MAX(cache_timestamp), COUNT(*))`` aggregated over CustomObjectType.

    Single indexed aggregation.  Used by ``refresh_if_stale()`` as the global
    invalidation token: every COT save (including transitive bumps from field
    save/delete at ``models.py`` and the m2m_changed receiver) advances
    cache_timestamp; every COT delete decreases COUNT.  Together they cover
    every event the related-tabs registry cares about, without a separate
    counter.
    """
    # Lazy imports — this module is referenced by middleware before the app
    # registry is fully ready, so module-level imports of model classes would
    # create a startup cycle.
    from django.db.models import Count, Max
    from netbox_custom_objects.models import CustomObjectType

    agg = CustomObjectType.objects.aggregate(
        max_ts=Max('cache_timestamp'),
        n=Count('id'),
    )
    return (agg['max_ts'], agg['n'])


def seed_local_state() -> None:
    """
    Initialise ``_last_seen_state`` after the initial ``register_tabs()``.

    Without this, the first request handled by a fresh worker would see
    ``_last_seen_state is None`` and unconditionally run ``_do_refresh()`` —
    duplicating work that ``register_tabs()`` already performed during
    ``ready()``.  Idempotent and safe to call multiple times.

    Database errors are swallowed (logged at WARNING): the worst case is one
    unnecessary refresh on the next request, which is harmless.
    """
    global _last_seen_state
    try:
        _last_seen_state = _compute_state()
    except Exception:
        logger.warning(
            'seed_local_state() failed; first request will trigger an extra refresh',
            exc_info=True,
        )


def _refresh_and_snapshot() -> bool:
    """
    Run ``_do_refresh()`` then re-snapshot ``_last_seen_state`` from a fresh DB read.

    Caller MUST hold ``_global_lock``.  Returns True if the refresh ran, False if
    ``_do_refresh()`` raised (``_last_seen_state`` left unchanged so a later call
    retries).  The snapshot is a fresh DB read rather than a value captured before
    the lock: another thread may have advanced ``_last_seen_state`` in the interim,
    and writing a stale value would regress it and force an unnecessary refresh on
    the next request.  If the post-refresh snapshot itself fails, reset to ``None``
    so the next ``refresh_if_stale()`` is forced to re-run (any tuple != None).
    """
    global _last_seen_state
    try:
        from netbox_custom_objects.related_tabs.registry import _do_refresh

        _do_refresh()
    except Exception:
        logger.exception('related_tabs hot-reload refresh failed')
        return False
    try:
        _last_seen_state = _compute_state()
    except Exception:
        logger.exception('failed to snapshot state after refresh; next request will re-refresh')
        _last_seen_state = None
    return True


def refresh_if_stale() -> bool:
    """
    Re-register tabs if the DB invalidation token differs from our snapshot.

    Called from ``TabRegistryRefreshMiddleware`` on every request and from the
    signal handlers in the process that mutated the COT.  Fast-path cost when
    in sync: one indexed aggregation over ``CustomObjectType`` (typical NetBox
    installs have <50 COTs; comparable to a Redis GET).

    Returns True if a refresh actually ran, False otherwise.
    """
    try:
        current = _compute_state()
    except Exception:
        # DB temporarily unavailable (e.g. mid-migration).  Don't crash the
        # request; we'll catch up when the DB recovers and the snapshot drifts
        # again.
        logger.exception('failed to read cache_timestamp snapshot')
        return False

    if current == _last_seen_state:
        return False

    with _global_lock:
        # Re-check after acquiring the lock: another thread in this process may
        # have just refreshed and advanced _last_seen_state.
        if current == _last_seen_state:
            return False
        return _refresh_and_snapshot()


def force_local_refresh() -> None:
    """
    Refresh local registry unconditionally and snapshot the resulting DB token.

    Called from signal handlers in the process that just performed a COT
    mutation, so the worker that handled the request sees the change on its
    own next view dispatch without waiting for the middleware path.  Other
    workers pick up the change via ``refresh_if_stale()`` when they observe
    the cache_timestamp / count change in the DB.
    """
    with _global_lock:
        _refresh_and_snapshot()
