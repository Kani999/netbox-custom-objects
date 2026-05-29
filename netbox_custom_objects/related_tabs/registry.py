import logging

from .views.combined import make_co_combined_view, register_combined_tabs

logger = logging.getLogger('netbox_custom_objects.related_tabs')

# Action name / path / URL name for the combined tab on custom-object host pages.
# Kept in sync with the hardcoded <li> in customobject.html and the
# custom_objects_tab_link template tag.
_CO_COMBINED_ACTION = 'custom_objects'
_CO_COMBINED_PATH = 'custom-objects'
# CustomObject._get_viewname('custom_objects') ->
# 'plugins:netbox_custom_objects:customobject_custom_objects'
CO_COMBINED_URL_NAME = f'customobject_{_CO_COMBINED_ACTION}'


def _inject_co_urls():
    """
    Inject the generic combined-tab URL for custom-object host pages into
    ``netbox_custom_objects.urls``.

    The netbox_custom_objects plugin serves all custom object detail pages through a
    single generic view at ``<str:custom_object_type>/<int:pk>/``.  It never calls
    ``get_model_urls()`` for dynamic models, so our tab view has no corresponding
    URL pattern.  We add ONE generic, slug-parameterised pattern here at ready()
    time — before Django loads the URL conf on the first request.

    Crucially this is a single COT-agnostic route (the slug is a path parameter),
    not one route per CustomObjectType.  It therefore reverses for *any* slug,
    including CustomObjectTypes created after startup — which is what lets the
    combined tab appear on a brand-new CO→CO reference with no restart.  The
    nav-link is rendered live by the ``custom_objects_tab_link`` template tag;
    this function only guarantees the link target resolves.

    The URL name follows CustomObject._get_viewname():
      ``plugins:netbox_custom_objects:customobject_custom_objects``
    """
    try:
        import netbox_custom_objects.urls as co_urls
        from django.urls import path as url_path
    except ImportError:
        return

    existing_names = {p.name for p in co_urls.urlpatterns if hasattr(p, 'name') and p.name}
    if CO_COMBINED_URL_NAME in existing_names:
        return

    full_path = f'<str:custom_object_type>/<int:pk>/{_CO_COMBINED_PATH}/'
    co_urls.urlpatterns.append(
        url_path(full_path, make_co_combined_view().as_view(), name=CO_COMBINED_URL_NAME)
    )
    logger.debug("injected URL pattern '%s'", CO_COMBINED_URL_NAME)


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
# discovery is now automatic.
_COMBINED_LABEL = 'Custom Objects'
_COMBINED_WEIGHT = 2000


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
    and register a combined "Custom Objects" tab for each.

    Called from ``CustomObjectsPluginConfig.ready()`` as a third pass, after
    the existing two-pass model + serializer registration.

    All registration must happen synchronously here: NetBox builds each
    model's URLconf (via ``get_model_urls()``) on the first ``resolve()``
    call, snapshotting ``registry['views']`` at that moment.  Anything added
    to the registry after the URLconf is built has no URL pattern.  Likewise,
    ``_inject_co_urls()`` mutates ``netbox_custom_objects.urls.urlpatterns``
    and must run before the URL resolver populates its lookup cache against
    that list.

    Because registration is startup-only, a brand-new target *model type* (a
    COT field referencing a NetBox model that nothing referenced before)
    requires a NetBox restart before its tab appears.  Everyday changes —
    creating custom objects, editing them — are reflected live, because the
    combined tab's badge and contents are computed from the DB on every
    render.  See the package docstring for the full trade-off.

    Database errors during target discovery are swallowed so that
    ``manage.py migrate`` on a fresh DB doesn't blow up; tabs come up on the
    next process start once migrations have applied.
    """
    # Inject the generic custom-object combined-tab URL unconditionally and
    # first.  It is a single COT-agnostic route, so it must exist at startup
    # (the URLconf freezes after ready()) to serve combined tabs on custom-object
    # host pages — including CustomObjectTypes created later (CO→CO references).
    # This does not depend on any CO being a *referenced* host at startup.
    _inject_co_urls()

    ct_ids = _discover_target_content_type_ids()
    if not ct_ids:
        # Either the DB is not ready or no COT fields reference anything yet.
        return

    model_classes = _resolve_model_classes(ct_ids)
    if not model_classes:
        return

    # Built-in (non custom-object) host models get a per-model registry entry +
    # per-model URL (via NetBox's get_model_urls at URLconf-build time), so a
    # brand-new built-in target still needs a restart.  Custom-object hosts are
    # served by the generic URL above + the live template-tag link, so they do
    # not.  Registering the combined view here for CO models too is harmless
    # (it shares the generic URL) and keeps the type-filter discovery uniform.
    register_combined_tabs(model_classes, _COMBINED_LABEL, _COMBINED_WEIGHT)

    _deduplicate_registry()
