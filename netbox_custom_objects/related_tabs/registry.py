import logging

from .views._co_common import _CUSTOM_OBJECTS_APP
from .views.combined import register_combined_tabs
from .views.typed import register_typed_tabs

logger = logging.getLogger('netbox_custom_objects.related_tabs')


def _inject_co_urls():
    """
    Inject URL patterns for our tab views into netbox_custom_objects.urls.

    The netbox_custom_objects plugin serves all custom object detail pages through a
    single generic view at ``<str:custom_object_type>/<int:pk>/``.  It never calls
    ``get_model_urls()`` for dynamic models, so our registered views have no
    corresponding URL patterns.  We add them here at ready() time — before Django
    loads the URL conf on the first request.

    The URL names follow CustomObject._get_viewname():
      ``plugins:netbox_custom_objects:customobject_{action}``
    which means we need names like ``customobject_custom_objects`` and
    ``customobject_custom_objects_{slug}`` inside netbox_custom_objects.urls.
    """
    try:
        import netbox_custom_objects.urls as co_urls
        from django.urls import path as url_path
        from netbox.registry import registry
    except ImportError:
        return

    co_app = _CUSTOM_OBJECTS_APP
    # Collect all tab view classes our plugin registered for CO dynamic models
    # from the global registry, keyed by their action name.
    co_views_by_name = {}  # action_name -> view_class
    for model_name, view_entries in registry['views'].get(co_app, {}).items():
        if not model_name.startswith('table'):
            continue
        for entry in view_entries:
            name = entry['name']
            view_cls = entry['view']
            # Only inject views we registered (combined / typed tab views)
            if name.startswith('custom_objects') and name not in co_views_by_name:
                co_views_by_name[name] = (entry['path'], view_cls)

    existing_names = {p.name for p in co_urls.urlpatterns if hasattr(p, 'name') and p.name}
    for action_name, (url_path_str, view_cls) in co_views_by_name.items():
        url_name = f'customobject_{action_name}'
        if url_name in existing_names:
            continue
        full_path = f'<str:custom_object_type>/<int:pk>/{url_path_str}/'
        co_urls.urlpatterns.append(url_path(full_path, view_cls.as_view(), name=url_name))
        logger.debug("injected URL pattern '%s'", url_name)


def _deduplicate_registry():
    """
    Remove duplicate view registrations from registry['views'].

    netbox_custom_objects calls get_model() multiple times during startup; each call
    that generates a new model instance re-registers journal/changelog views, producing
    duplicate tabs.  Since we run after netbox_custom_objects in INSTALLED_APPS, we can
    clean up the registry here by keeping only the first occurrence of each view name
    per model.
    """
    from netbox.registry import registry

    for app_label, model_map in registry['views'].items():
        for model_name, entries in model_map.items():
            seen = set()
            deduped = []
            for entry in entries:
                key = entry['name']
                if key not in seen:
                    seen.add(key)
                    deduped.append(entry)
            if len(deduped) < len(entries):
                logger.debug(
                    'removed %d duplicate registry entries for %s.%s',
                    len(entries) - len(deduped),
                    app_label,
                    model_name,
                )
                model_map[model_name] = deduped


# Hardcoded label/weight defaults.  The source plugin exposed these as
# PLUGINS_CONFIG knobs; the integrated version drops the knobs because target
# discovery is now automatic — the only per-COT control is show_dedicated_tab.
_COMBINED_LABEL = 'Custom Objects'
_COMBINED_WEIGHT = 2000
_TYPED_WEIGHT = 2100


def _discover_target_content_type_ids():
    """
    Return the set of ContentType IDs that should host a Custom Objects tab.

    A ContentType is a target iff at least one CustomObjectTypeField of type
    OBJECT or MULTIOBJECT references it — via either ``related_object_type``
    (non-polymorphic FK) or ``related_object_types`` (polymorphic M2M).

    Returns None if the database is not yet usable (fresh install before
    migrations).  Callers should treat that as "register nothing this round".
    """
    from django.db.utils import OperationalError, ProgrammingError
    from extras.choices import CustomFieldTypeChoices
    from netbox_custom_objects.models import CustomObjectTypeField

    type_choices = [
        CustomFieldTypeChoices.TYPE_OBJECT,
        CustomFieldTypeChoices.TYPE_MULTIOBJECT,
    ]

    try:
        non_poly = set(
            CustomObjectTypeField.objects.filter(
                is_polymorphic=False,
                type__in=type_choices,
            )
            .exclude(related_object_type__isnull=True)
            .values_list('related_object_type_id', flat=True)
        )
        poly = set(
            CustomObjectTypeField.objects.filter(
                is_polymorphic=True,
                type__in=type_choices,
            ).values_list('related_object_types__id', flat=True)
        )
    except (OperationalError, ProgrammingError):
        logger.warning('database unavailable - tabs not registered until next start')
        return None

    return {ct for ct in non_poly | poly if ct is not None}


def _resolve_model_classes(ct_ids):
    """
    Return a deduplicated list of model classes for the given ContentType IDs.

    Skips ContentTypes whose model class can't be resolved (e.g. an
    uninstalled plugin or a stale CT row pointing at a deleted dynamic model).
    """
    from django.contrib.contenttypes.models import ContentType
    from django.db.utils import OperationalError, ProgrammingError

    seen_keys = set()
    result = []
    try:
        cts = list(ContentType.objects.filter(pk__in=ct_ids))
    except (OperationalError, ProgrammingError):
        logger.warning('database unavailable during ContentType resolution')
        return result

    for ct in cts:
        try:
            model = ct.model_class()
        except Exception:
            logger.exception('could not resolve model for ContentType %s', ct)
            continue
        if model is None:
            logger.warning('ContentType %s has no model class - skipping', ct)
            continue
        key = (model._meta.app_label, model._meta.model_name)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        result.append(model)

    return result


def register_tabs():
    """
    Auto-discover target NetBox models from CustomObjectTypeField references
    and register combined + typed tabs for each.

    Called from ``CustomObjectsPluginConfig.ready()`` as a third pass, after
    the existing two-pass model + serializer registration.

    All registration must happen synchronously here: NetBox builds each
    model's URLconf (via ``get_model_urls()``) on the first ``resolve()``
    call, snapshotting ``registry['views']`` at that moment.  Anything added
    to the registry after the URLconf is built has no URL pattern.  Likewise,
    ``_inject_co_urls()`` mutates ``netbox_custom_objects.urls.urlpatterns``
    and must run before the URL resolver populates its lookup cache against
    that list.

    Earlier source-plugin versions deferred typed-tab registration to the
    first HTTP request (commit 5bf09c3, PR #4) to silence DB-access warnings
    from Django and netbox_branching.  That broke typed-tab URL routing
    entirely — the Add-button feature in 2.3.0 was never reachable on a
    deployment.  See standalone-plugin 2.3.0 release notes.

    Database errors during target discovery are swallowed so that
    ``manage.py migrate`` on a fresh DB doesn't blow up; tabs come up on the
    next process start once migrations have applied.
    """
    ct_ids = _discover_target_content_type_ids()
    if not ct_ids:
        # Either the DB is not ready or no COT fields reference anything yet.
        return

    model_classes = _resolve_model_classes(ct_ids)
    if not model_classes:
        return

    register_combined_tabs(model_classes, _COMBINED_LABEL, _COMBINED_WEIGHT)
    register_typed_tabs(model_classes, _TYPED_WEIGHT)

    if any(m._meta.app_label == _CUSTOM_OBJECTS_APP for m in model_classes):
        _inject_co_urls()

    _deduplicate_registry()
