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

Two host kinds, two behaviours:

* **Built-in NetBox models** (Device, Site, …) — the plugin does NOT own their
  templates, so the tab is rendered by NetBox's registry-driven tab machinery and
  its URL is a per-model route baked by ``get_model_urls()`` at URLconf-freeze.
  Consequence (deliberate trade-off): the *first* COT field to reference a
  built-in model nothing referenced at startup needs a NetBox restart before its
  tab appears. Subsequent references to an already-referenced built-in model, and
  all object create/edit/delete, are live.

* **Custom-object host pages** (a COT field that targets another COT — CO→CO) —
  the plugin owns ``customobject.html``, so the tab nav-link is rendered live by
  the ``custom_objects_tab_link`` template tag (computed from the DB per render),
  and its URL is a single COT-agnostic route injected once at startup
  (``_inject_co_urls``) that reverses for *any* slug, including COTs created
  later. So CO→CO references are **always live, with no restart** — independent of
  the startup view registry.

This keeps the feature free of any runtime URL-resolver mutation or cross-worker
coordination machinery (no middleware, no signals, no shared cache backend).
Per-CustomObjectType "typed" tabs (each opted-in COT as its own separate tab) are
intentionally out of scope here and proposed as a follow-up.
"""
