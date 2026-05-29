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

## Related Objects Tab

When a Custom Object Type has an Object or Multi-object field that points at another model (a NetBox model such as Device or Site, or another Custom Object Type), a **Custom Objects** tab is added to the detail page of every referenced object. The tab lists all custom objects that link to the object being viewed, across every referencing field and type, with:

- a badge showing the linked-object count (the tab hides itself when there are none),
- search, plus type and tag filters, and sortable columns,
- HTMX-driven pagination and per-user column configuration,
- per-row edit/delete actions.

Discovery is automatic and requires no configuration — both non-polymorphic and polymorphic Object/Multi-object fields are supported, on built-in NetBox models and on Custom Object Type detail pages (custom-object-to-custom-object references).

The tab complements the existing **Custom Objects linking to this object** panel on the object's main page; both surface the same relationships, the tab as a dedicated, filterable list.

### Caveats

- **References between custom object types are always live.** A brand-new Custom Object Type that points at another shows the tab on the referenced type's pages on the next page load, with no restart — custom object detail pages render the tab live from the database. Creating and editing custom objects is likewise always live.
- **One case needs a NetBox restart:** the *first time* any Custom Object Type field references a **built-in NetBox model** that nothing referenced before (e.g. the first-ever reference to `dcim.rack`), that model's tab only appears after a restart. This is because NetBox builds each built-in model's URL routes once at startup; subsequent references to an already-referenced built-in model are live.
- **Badge count vs. visible rows:** the count in the tab badge is computed before per-Custom-Object-Type view permissions are applied, so a user without permission on a given type may see a count higher than the number of rows they can actually open.

## Known Limitations

NetBox Custom Objects is now Generally Available which means you can use it in production and migrations to future versions will work. There are many upcoming features including GraphQL support - the best place to see what's on the way is the [issues](https://github.com/netboxlabs/netbox-custom-objects/issues) list on the GitHub repository.
