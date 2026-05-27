"""
Django signals that drive multi-process tab hot-reload.

When a CustomObjectType or CustomObjectTypeField is created, modified, or
deleted, the process that handled the request bumps a shared Redis counter
and refreshes its own tab registry.  Other WSGI worker processes pick up
the change via ``TabRegistryRefreshMiddleware`` on their next request.

The handlers are intentionally minimal: a single ``force_local_refresh()``
call covers both add and remove cases because ``_do_refresh()`` tears down
all our registry entries before re-running ``register_tabs()``.

Three triggers feed the refresh:

* ``post_save`` / ``post_delete`` on the two driving models — catches the
  vast majority of mutations.
* ``m2m_changed`` on ``CustomObjectTypeField.related_object_types`` — catches
  polymorphic field target updates.  Without this, the API serializer's
  pattern of writing the M2M *after* the field's own ``save()`` would leave
  our initial refresh seeing an empty target list, so newly-targeted host
  ContentTypes wouldn't register until something else nudges another
  refresh (e.g., a later show_dedicated_tab toggle).

All refreshes are deferred via ``transaction.on_commit`` so a rolled-back
transaction can't leak a Redis bump or in-process registry mutation that
doesn't reflect persisted state.
"""

import logging

from django.db import transaction
from django.db.models.signals import m2m_changed, post_delete, post_save

logger = logging.getLogger('netbox_custom_objects.related_tabs')


def _schedule_refresh(reason):
    """
    Defer a ``force_local_refresh()`` until the current transaction commits.

    Outside a transaction (e.g. autocommit fallback) ``transaction.on_commit``
    runs the callable immediately, which is fine — the caller is already
    after the row mutation.  Inside a transaction that rolls back, the
    callable never fires, so we don't bump Redis or invalidate the registry
    based on changes that didn't persist.
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
        # Defensive — on_commit shouldn't raise outside special cases (atomic
        # block savepoint failures).  Fall back to immediate refresh so a
        # transient transaction-state oddity doesn't permanently desync.
        logger.exception('transaction.on_commit failed; running refresh inline for %s', reason)
        _do()


def _on_cot_change(sender, instance, **kwargs):
    """post_save / post_delete handler for CustomObjectType / CustomObjectTypeField."""
    _schedule_refresh(f'{sender.__name__} save/delete')


def _on_cot_field_targets_changed(sender, instance, action, **kwargs):
    """
    m2m_changed handler for CustomObjectTypeField.related_object_types.

    Fires once per add/remove/clear/set action on the through table.  We
    only care about the post_* actions (state has actually changed in the
    DB); the pre_* variants would observe the registry mid-transaction.
    """
    if action not in {'post_add', 'post_remove', 'post_clear'}:
        return
    _schedule_refresh(f'{instance.__class__.__name__}.related_object_types {action}')


def connect():
    """
    Wire post_save / post_delete / m2m_changed handlers for the driving models.

    Idempotent: each handler uses a unique ``dispatch_uid``, so calling
    ``connect()`` more than once (e.g. under Django's autoreloader) doesn't
    duplicate registrations.
    """
    from netbox_custom_objects.models import CustomObjectType, CustomObjectTypeField

    for model, suffix in [
        (CustomObjectType, 'cot'),
        (CustomObjectTypeField, 'cotfield'),
    ]:
        post_save.connect(
            _on_cot_change,
            sender=model,
            dispatch_uid=f'related_tabs_refresh_on_save_{suffix}',
        )
        post_delete.connect(
            _on_cot_change,
            sender=model,
            dispatch_uid=f'related_tabs_refresh_on_delete_{suffix}',
        )

    # m2m_changed sender for the field's polymorphic-target M2M.  The
    # `through` model is auto-created by Django for the M2M; passing it as
    # the sender narrows the signal to only this M2M (other plugins' M2Ms
    # don't trigger us).
    m2m_changed.connect(
        _on_cot_field_targets_changed,
        sender=CustomObjectTypeField.related_object_types.through,
        dispatch_uid='related_tabs_refresh_on_cotfield_related_object_types_changed',
    )
