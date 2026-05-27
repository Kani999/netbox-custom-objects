"""
Middleware that checks for cross-process tab-registry updates on each request.

When another WSGI worker process mutated a CustomObjectType or
CustomObjectTypeField, it bumped the Redis counter in
``netbox_custom_objects.related_tabs`` via the signal handlers.  This
middleware compares local vs remote on every request and, if stale, calls
``refresh_if_stale()`` before URL resolution runs — so the request that
triggered the refresh sees the fresh registry, not the stale one.

Fast-path cost (in-sync): one Redis GET, ~1 ms.

Installed automatically via ``CustomObjectsPluginConfig.middleware`` — NetBox
appends each plugin's middleware list to the global ``MIDDLEWARE`` setting at
startup (see ``netbox/settings.py`` around the ``plugin_config.middleware``
line in the plugin-loading block).
"""


class TabRegistryRefreshMiddleware:
    """Refresh ``related_tabs`` registry from Redis on every request if stale."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Imported inside __call__ to avoid an import-at-startup cycle with
        # the plugin's ready() (this module is referenced by name in
        # CustomObjectsPluginConfig.middleware, so Django imports it before
        # apps are fully ready).
        from netbox_custom_objects.related_tabs import refresh_if_stale

        try:
            refresh_if_stale()
        except Exception:
            # Never let a registry-refresh failure prevent a request from
            # being served; just log and continue with whatever tabs we
            # currently have.
            import logging  # noqa: PLC0415

            logging.getLogger('netbox_custom_objects.related_tabs').exception(
                'refresh_if_stale() raised inside middleware'
            )
        return self.get_response(request)
