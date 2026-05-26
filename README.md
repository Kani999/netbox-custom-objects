# NetBox Custom Objects

This [NetBox](https://netboxlabs.com/products/netbox/) plugin introduces the ability to create new object types in NetBox so that users can add models to suit their own needs. NetBox users have been able to extend the NetBox data model for some time using both Tags & Custom Fields and Plugins. Tags and Custom Fields are easy to use, but they have limitations when used at scale, and Plugins are very powerful but require Python/Django knowledge, and ongoing maintenance. Custom Objects provides users with a no-code "sweet spot" for data model extensibility, providing a lot of the power of NetBox plugins, but with the ease of use of Tags and Custom Fields.

You can find further documentation [here](https://github.com/netboxlabs/netbox-custom-objects/blob/main/docs/index.md). See the [compatibility matrix](COMPATIBILITY.md) for supported NetBox versions.

## Installation

1. Install the NetBox Custom Objects package.

```
pip install netboxlabs-netbox-custom-objects
```

2. Add `netbox_custom_objects` to `PLUGINS` in `configuration.py`.

```python
PLUGINS = [
    # ...
    'netbox_custom_objects',
]
```

3. Run NetBox migrations:

```
$ ./manage.py migrate
```

4. Restart NetBox
```
sudo systemctl restart netbox netbox-rq
```

> [!NOTE]
> If you are using NetBox Custom Objects with NetBox Branching, you need to insert the following into your `configuration.py`. See the docs for a full description of how NetBox Custom Objects currently works with NetBox Branching.  

```
PLUGINS_CONFIG = {
    'netbox_branching': {
        'exempt_models': [
            'netbox_custom_objects.customobjecttype',
            'netbox_custom_objects.customobjecttypefield',
        ],
    },
}
```

## Related-Object Tabs

Whenever a Custom Object Type has an Object or Multi-object field that points
at another NetBox object (e.g. `dcim.device`, `ipam.prefix`, `tenancy.tenant`),
the plugin automatically renders a **Custom Objects** tab on that target
object's detail page. The tab lists every custom object linking back to the
parent — with search, filters, pagination, column configuration, and per-row
edit/delete actions — so you can see what's connected without leaving the
object you're already looking at.

### Combined tab (always on)

The combined "Custom Objects" tab appears on the detail page of any NetBox
object referenced by at least one custom-object field. It aggregates all
related custom objects across every Custom Object Type that points at this
host, with a Type column to disambiguate. No configuration required — the tab
discovery runs automatically from the Custom Object Type Field definitions.

### Dedicated tab per Custom Object Type (opt-in)

For Custom Object Types where the aggregated view isn't enough, tick **Show
dedicated tab** on the Custom Object Type's edit form. The plugin then
registers a per-type tab on every detail page the COT references, with the
COT's full native list-view experience: type-specific columns, sidebar
filters, bulk edit/delete, and a pre-filled "Add" button. The flag is also
available from bulk edit, CSV import, and the REST API
(`PATCH /api/plugins/custom-objects/custom-object-types/<id>/`
`{"show_dedicated_tab": true}`).

### Hot-reload (no restart required)

Toggling **Show dedicated tab**, creating or deleting a Custom Object Type,
or editing a polymorphic field's allowed target types takes effect on the
next page load without restarting NetBox or `gunicorn`. Cross-worker
propagation uses a Redis-shared monotonic counter
(`nbco:tab_registry_version`) plus a thin middleware
(`TabRegistryRefreshMiddleware`) that NetBox auto-installs via the plugin
config. Cost on the steady-state hot path: one Redis GET per request.

> [!NOTE]
> The middleware is registered automatically through the plugin's
> `PluginConfig.middleware` list — no manual addition to
> `MIDDLEWARE` in `configuration.py` is required.

### Behavioural notes

* A dedicated tab with zero linked custom objects is **hidden by default**
  (`hide_if_empty=True` on the underlying `ViewTab`). If you enable
  `show_dedicated_tab` on a COT before any instances reference the host
  model, the tab will materialise the moment the first instance is
  created.
* The combined-tab badge count reflects the total before per-COT view
  permissions are applied. A user without permission to view a particular
  COT will see the inflated count but no leaked rows once they open the
  tab. (Acknowledged limitation; tracked for a follow-up PR.)
* Renaming a Custom Object Type's slug propagates correctly to both the
  registry and host apps' URL configurations on the next request.

## Known Limitations

NetBox Custom Objects is now Generally Available which means you can use it in production and migrations to future versions will work. There are many upcoming features including GraphQL support - the best place to see what's on the way is the [issues](https://github.com/netboxlabs/netbox-custom-objects/issues) list on the GitHub repository.
