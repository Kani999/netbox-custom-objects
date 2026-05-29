"""
Related-object tabs for netbox-custom-objects.

Adds a single combined "Custom Objects" tab to the detail page of any NetBox
object referenced by a Custom Object Type field. Vendored from the standalone
netbox-custom-objects-tab plugin and integrated as a subpackage so the feature
ships with the core plugin itself.

The tab is registered once, at startup, by ``registry.register_tabs()`` (called
from ``CustomObjectsPluginConfig.ready()``). It discovers every host model
referenced by a CustomObjectType OBJECT/MULTIOBJECT field and registers one
combined-tab view per host model into NetBox's view registry
(``registry['views']``), which NetBox reads live on every request.

Why registration is startup-only — and what that costs:

NetBox renders detail-page tabs from ``registry['views']``, read live per
request, but each tab's URL is resolved by ``reverse()`` against a per-entry URL
name (``<model>_custom_objects``). Those URL patterns are emitted by
``get_model_urls()`` when each host app's ``urls.py`` is imported — once, when
Django loads the root URLconf on the first request, i.e. after ``ready()``.
Registering in ``ready()`` lands our patterns in the frozen urlpatterns
naturally; anything registered later has no reversible URL and the tab would
silently 404.

Consequence (deliberate trade-off): a COT field that references a brand-new
*target model type* — a NetBox model nothing referenced at startup — needs a
NetBox restart before its combined tab appears. Everything else is live: the
tab's badge count and table contents are computed from the DB on each render, so
creating/editing/deleting custom objects shows up immediately with no restart.

This keeps the feature free of any runtime URL-resolver mutation or cross-worker
coordination machinery (no middleware, no signals, no shared cache backend).
Per-CustomObjectType "typed" tabs are intentionally out of scope here; they are
proposed as a follow-up built as sub-navigation inside this one combined tab.
"""
