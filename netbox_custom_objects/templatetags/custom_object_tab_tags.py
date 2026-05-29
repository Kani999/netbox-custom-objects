from django import template
from django.urls.exceptions import NoReverseMatch
from django.utils.module_loading import import_string
from netbox.registry import registry
from utilities.views import get_action_url

__all__ = ('plugin_extra_tabs', 'custom_objects_tab_link')

register = template.Library()

# NetBox's `extras` framework auto-registers ObjectJournalView/ObjectChangeLogView
# for every model that supports them (see netbox/models/features.py). On Custom
# Object detail pages we render those two tabs as hardcoded <li> blocks instead,
# because upstream's CustomObjectJournalView/CustomObjectChangeLogView put the
# string "journal"/"changelog" in the template context as the active-tab marker,
# which `model_view_tabs` cannot match against its ViewTab object. Filtering them
# out here prevents duplicate, never-active tabs from being rendered.
#
# 'custom_objects' is excluded too: the combined "Custom Objects" tab on a custom
# object detail page is rendered live by ``custom_objects_tab_link`` (a hardcoded
# <li>), independent of the startup view registry, so a brand-new CustomObjectType
# (a CO→CO reference) gets a tab without a restart.  Letting plugin_extra_tabs also
# render it from the registry would duplicate the tab for COTs referenced at startup.
_HARDCODED_TAB_NAMES = frozenset({'journal', 'changelog', 'custom_objects'})

# Weight matching registry._COMBINED_WEIGHT, for ordering against other tabs.
_COMBINED_WEIGHT = 2000


@register.inclusion_tag('tabs/model_view_tabs.html', takes_context=True)
def plugin_extra_tabs(context, instance):
    """
    Render registered model-view tabs for `instance`, excluding tabs that the
    Custom Object detail template already renders by hand (Journal, Changelog,
    and the combined Custom Objects tab — see _HARDCODED_TAB_NAMES).
    """
    app_label = instance._meta.app_label
    model_name = instance._meta.model_name
    user = context['request'].user
    tabs = []

    try:
        views = registry['views'][app_label][model_name]
    except KeyError:
        views = []

    for config in views:
        if config['name'] in _HARDCODED_TAB_NAMES:
            continue
        view = import_string(config['view']) if type(config['view']) is str else config['view']
        if tab := getattr(view, 'tab', None):
            if tab.permission and not user.has_perm(tab.permission):
                continue
            if attrs := tab.render(instance):
                try:
                    url = get_action_url(instance, action=config['name'], kwargs={'pk': instance.pk})
                except NoReverseMatch:
                    continue
                tabs.append(
                    {
                        'name': config['name'],
                        'url': url,
                        'label': attrs['label'],
                        'badge': attrs['badge'],
                        'weight': attrs['weight'],
                        'is_active': context.get('tab') == tab,
                    }
                )

    tabs = sorted(tabs, key=lambda x: x['weight'])
    return {'tabs': tabs}


@register.inclusion_tag('netbox_custom_objects/related_tabs/combined/tab_link.html', takes_context=True)
def custom_objects_tab_link(context, instance):
    """
    Render the combined "Custom Objects" tab nav-link on a custom object detail
    page, computed live from the DB (not the startup view registry).

    This is what makes references *between* custom object types live without a
    NetBox restart: the tab's URL is a single COT-agnostic route injected at
    startup (``registry._inject_co_urls``) that reverses for any slug, and the
    nav-link's visibility/badge are recomputed per render here.  Returns an empty
    context (no link) when the badge count is zero (hide_if_empty) or the URL
    can't be reversed (plugin URLs not loaded).
    """
    from netbox_custom_objects.related_tabs.views.combined import _count_linked_custom_objects

    badge = _count_linked_custom_objects(instance)
    if not badge:
        return {'tab': None}

    try:
        url = get_action_url(instance, action='custom_objects', kwargs={'pk': instance.pk})
    except NoReverseMatch:
        return {'tab': None}

    # The combined-tab view renders with context['tab'] set to its ViewTab object,
    # whereas the primary tab leaves it unset (None) and Journal/Changelog set it
    # to the strings 'journal'/'changelog'.  So "we are on the combined tab" iff
    # context['tab'] is a non-string (the ViewTab) — that's the active case.
    ctx_tab = context.get('tab')
    is_active = ctx_tab is not None and not isinstance(ctx_tab, str)

    return {
        'tab': {
            'url': url,
            'label': 'Custom Objects',
            'badge': badge,
            'is_active': is_active,
        }
    }
