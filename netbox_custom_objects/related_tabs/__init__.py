"""
Related-object tabs for netbox-custom-objects.

Adds a "Custom Objects" combined tab and per-CustomObjectType typed tabs to
NetBox object detail pages. Vendored from the standalone netbox-custom-objects-tab
plugin and integrated as a subpackage so the feature ships with the core plugin
itself.

The public entry point is ``register_tabs()`` in ``registry``. It is *not* wired
into ``CustomObjectsPluginConfig.ready()`` yet — that happens in the next
integration step, alongside the per-COT ``show_dedicated_tab`` opt-in field and
the multi-worker hot-reload machinery (signals, middleware, Redis-versioned
registry).
"""
