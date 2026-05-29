"""
Django signals that drive multi-process tab hot-reload.

When a ``CustomObjectType`` is created, modified, or deleted, the process that
handled the request schedules a local registry refresh (deferred to commit).
Cross-worker propagation rides on the ``cache_timestamp`` column on
``CustomObjectType``: peer workers observe the change via the
``(MAX(cache_timestamp), COUNT(*))`` snapshot read by
``TabRegistryRefreshMiddleware`` on their next request.

This module only needs to wire up the local refresh; the cross-worker side is
entirely a property of the DB token.

Two triggers feed the refresh:

* ``post_save`` on ``CustomObjectType`` — catches direct COT saves AND
  transitively every ``CustomObjectTypeField`` mutation, because both
  ``CustomObjectTypeField.save()`` and ``.delete()`` call
  ``cot.save(update_fields=['cache_timestamp'])`` which fires this signal.  The
  ``models.py`` ``m2m_changed`` receiver
  (``bump_cot_cache_timestamp_on_m2m_change``) similarly bumps the parent COT,
  so polymorphic target M2M changes also flow through here.
* ``post_delete`` on ``CustomObjectType`` — catches deletion.  The DB snapshot
  picks this up via ``COUNT(*)`` decreasing; this handler ensures the worker
  that performed the delete also refreshes locally.

All refreshes are deferred via ``transaction.on_commit`` so a rolled-back
transaction can't leak a registry mutation that doesn't reflect persisted
state.
"""

import logging

from django.db import transaction
from django.db.models.signals import post_delete, post_save

logger = logging.getLogger('netbox_custom_objects.related_tabs')


def _schedule_refresh(reason):
    """
    Defer a ``force_local_refresh()`` until the current transaction commits.

    Outside a transaction (e.g. autocommit fallback) ``transaction.on_commit``
    runs the callable immediately, which is fine — the caller is already after
    the row mutation.  Inside a transaction that rolls back, the callable never
    fires, so we don't invalidate the registry based on changes that didn't
    persist.
    """

    def _do():
        from netbox_custom_objects.related_tabs import force_local_refresh

        try:
            force_local_refresh()
        except Exception:
            logger.exception('hot-reload refresh failed after %s', reason)

    try:
        transaction.on_commit(_do)
    except Exception:
        # ``transaction.on_commit`` raises only inside an aborted atomic block
        # (savepoint already failed).  Running ``_do()`` inline here would
        # invalidate the registry from a state that may never persist.  Log
        # and skip; the next request's middleware will catch up via the DB
        # snapshot once the outer transaction either commits or rolls back.
        logger.exception(
            'transaction.on_commit failed; skipping refresh for %s (will be picked up on next request via middleware)',
            reason,
        )


def _on_cot_change(sender, instance, **kwargs):
    """post_save / post_delete handler for CustomObjectType."""
    _schedule_refresh(f'{sender.__name__} save/delete')


def connect():
    """
    Wire post_save / post_delete handlers for ``CustomObjectType``.

    Idempotent: each handler uses a unique ``dispatch_uid``, so calling
    ``connect()`` more than once (e.g. under Django's autoreloader) doesn't
    duplicate registrations.

    Note: ``CustomObjectTypeField`` saves/deletes don't need their own
    handlers because ``field.save()`` / ``field.delete()`` already call
    ``cot.save(update_fields=['cache_timestamp'])`` on the parent COT (see
    ``models.py``), which fires the ``CustomObjectType.post_save`` handler
    below transitively.  Likewise, ``m2m_changed`` on
    ``related_object_types.through`` is handled by the
    ``bump_cot_cache_timestamp_on_m2m_change`` receiver in ``models.py``,
    which bumps the parent COT and triggers this handler in turn.
    """
    from netbox_custom_objects.models import CustomObjectType

    post_save.connect(
        _on_cot_change,
        sender=CustomObjectType,
        dispatch_uid='related_tabs_refresh_on_save_cot',
    )
    post_delete.connect(
        _on_cot_change,
        sender=CustomObjectType,
        dispatch_uid='related_tabs_refresh_on_delete_cot',
    )
