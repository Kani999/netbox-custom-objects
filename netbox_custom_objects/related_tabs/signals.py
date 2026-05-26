"""
Django signals that drive multi-worker tab hot-reload.

When a CustomObjectType or CustomObjectTypeField is created, modified, or
deleted, the worker that handled the request bumps a shared Redis counter
and refreshes its own tab registry.  Other gunicorn workers pick up the
change via ``TabRegistryRefreshMiddleware`` on their next request.

The handlers are intentionally minimal: a single ``force_local_refresh()``
call covers both add and remove cases because ``_do_refresh()`` tears down
all our registry entries before re-running ``register_tabs()``.
"""

import logging

from django.db.models.signals import post_delete, post_save

logger = logging.getLogger('netbox_custom_objects.related_tabs')


def _on_cot_change(sender, instance, **kwargs):
    """Trigger a refresh after a CustomObjectType or CustomObjectTypeField mutation."""
    from netbox_custom_objects.related_tabs import force_local_refresh

    try:
        force_local_refresh()
    except Exception:
        logger.exception('hot-reload refresh failed after %s save/delete', sender.__name__)


def connect():
    """
    Wire post_save / post_delete handlers for the two driving models.

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
