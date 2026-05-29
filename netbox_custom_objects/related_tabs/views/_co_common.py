import logging

from django.apps import apps
from django.db.models import Q
from extras.choices import CustomFieldTypeChoices
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


def reference_q(host_ct_id, host_pk, field_name, field_type, is_polymorphic, through_model_name=None):
    """
    Build a Q selecting custom-object rows whose ``field_name`` references the host
    object identified by (``host_ct_id``, ``host_pk``).  Single source of truth for
    the four reference shapes shared by the combined and typed tab views:

      * OBJECT, non-polymorphic      -> ``{name}_id``
      * OBJECT, polymorphic          -> ``{name}_content_type_id`` + ``{name}_object_id``
      * MULTIOBJECT, non-polymorphic -> ``{name}`` (reverse M2M)
      * MULTIOBJECT, polymorphic     -> ``pk__in`` subquery over the field's through table

    Returns an EMPTY ``Q()`` for an unsupported field type or an unresolvable
    polymorphic through model.  Callers MUST treat an empty Q as "matches nothing /
    skip" and never pass it to ``.filter()`` directly — ``filter(Q())`` matches
    every row (an empty Q is the identity element for ``|``).
    """
    if field_type == CustomFieldTypeChoices.TYPE_OBJECT:
        if is_polymorphic:
            return Q(**{f'{field_name}_content_type_id': host_ct_id, f'{field_name}_object_id': host_pk})
        return Q(**{f'{field_name}_id': host_pk})

    if field_type == CustomFieldTypeChoices.TYPE_MULTIOBJECT:
        if is_polymorphic:
            try:
                through = apps.get_model(_CUSTOM_OBJECTS_APP, through_model_name)
            except LookupError:
                logger.exception(
                    'Could not resolve through model %r for polymorphic field %s', through_model_name, field_name
                )
                return Q()
            return Q(pk__in=through.objects.filter(content_type_id=host_ct_id, object_id=host_pk).values('source_id'))
        return Q(**{field_name: host_pk})

    return Q()


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
