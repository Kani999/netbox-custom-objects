import logging

from netbox.registry import registry
from utilities.views import register_model_view

_CUSTOM_OBJECTS_APP = 'netbox_custom_objects'
# Dynamic CO models use a single shared detail template; per-model templates don't exist.
_CO_BASE_TEMPLATE = 'netbox_custom_objects/customobject.html'

logger = logging.getLogger('netbox_custom_objects.related_tabs')


def _get_base_template(instance):
    """Return the correct base_template for an object's detail page."""
    if instance._meta.app_label == _CUSTOM_OBJECTS_APP:
        return _CO_BASE_TEMPLATE
    return f'{instance._meta.app_label}/{instance._meta.model_name}.html'


def _restrict_or_warn(qs, user, *, label):
    """
    Apply NetBox's per-row ``.restrict(user, 'view')`` to ``qs``.

    If the queryset's manager doesn't implement ``.restrict()`` (rare — only
    models whose manager isn't a RestrictedQuerySet), log a warning and return
    ``qs`` unrestricted, so a silent permission bypass is observable in logs
    rather than invisible.
    """
    try:
        return qs.restrict(user, 'view')
    except AttributeError:
        logger.warning('%s lacks restrict(user, view); per-row permission filter skipped', label)
        return qs


def _register_tab_view(model_class, name, path, view_factory):
    """
    Register a model-view tab on ``model_class``, building it via ``view_factory``.

    Idempotent: if a tab with this ``name`` is already registered for the model,
    log and skip without building the view — this guards against the Django
    autoreloader re-running registration and against hot-reload re-registration.
    ``view_factory`` is a zero-arg callable so the (cheap but pointless) view-class
    construction is skipped on the already-registered path.

    Returns True if the view was registered, False if it was skipped.
    """
    app_label = model_class._meta.app_label
    model_name = model_class._meta.model_name
    existing = registry['views'].get(app_label, {}).get(model_name, [])
    if any(entry['name'] == name for entry in existing):
        logger.debug('tab %r already registered for %s.%s — skipping', name, app_label, model_name)
        return False
    register_model_view(model_class, name=name, path=path)(view_factory())
    logger.debug('registered tab %r for %s.%s', name, app_label, model_name)
    return True
