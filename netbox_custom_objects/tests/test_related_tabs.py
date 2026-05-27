"""
Tests for the related_tabs subpackage.

Focused on the surfaces most likely to regress:

* ``register_tabs()`` auto-discovery from CustomObjectTypeField rows (both
  non-polymorphic via ``related_object_type`` and polymorphic via the
  ``related_object_types`` M2M).
* ``_purge_tab_entries()`` / ``_purge_injected_urls()`` idempotency — multiple
  ``_do_refresh()`` cycles leave the registry in a deterministic state with
  no duplicates and no lost upstream entries.
* ``ViewTab.visible()`` defence-in-depth predicate — re-reads
  ``show_dedicated_tab`` live so a missed hot-reload can't show a stale tab.
* Signal handlers (``post_save`` / ``post_delete`` / ``m2m_changed``) defer
  via ``transaction.on_commit`` and ultimately bump the local registry
  version counter.

These unit tests catch regressions at the function level so a breaking
change shows up in CI before manual smoke.
"""

from django.contrib.contenttypes.models import ContentType
from django.db.models.signals import m2m_changed, post_save
from django.test import TestCase, TransactionTestCase
from extras.choices import CustomFieldTypeChoices

from dcim.models import Site

from netbox_custom_objects.models import CustomObjectType, CustomObjectTypeField
from netbox_custom_objects.related_tabs import (
    _REDIS_KEY,
    refresh_if_stale,
)
from netbox_custom_objects.related_tabs.registry import (
    _COMBINED_NAME,
    _TYPED_NAME_PREFIX,
    _discover_target_content_type_ids,
    _is_our_tab_name,
    _purge_tab_entries,
    _resolve_model_classes,
)
from netbox_custom_objects.tests.base import CustomObjectsTestCase, TransactionCleanupMixin


class DiscoveryTests(TransactionCleanupMixin, CustomObjectsTestCase, TransactionTestCase):
    """``_discover_target_content_type_ids()`` finds non-poly + poly targets."""

    def test_non_polymorphic_target_is_discovered(self):
        cot = CustomObjectType.objects.create(
            name='disc_test_a',
            slug='disc-test-a',
            verbose_name_plural='Disc Test As',
        )
        site_ct = ContentType.objects.get_for_model(Site)
        CustomObjectTypeField.objects.create(
            custom_object_type=cot,
            name='related_site',
            label='Related Site',
            type=CustomFieldTypeChoices.TYPE_OBJECT,
            is_polymorphic=False,
            related_object_type=site_ct,
        )

        ct_ids = _discover_target_content_type_ids()

        self.assertIn(site_ct.pk, ct_ids)

    def test_polymorphic_targets_are_discovered(self):
        cot = CustomObjectType.objects.create(
            name='disc_test_b',
            slug='disc-test-b',
            verbose_name_plural='Disc Test Bs',
        )
        site_ct = ContentType.objects.get_for_model(Site)
        # Use site as the only poly target; verifies the M2M code path
        # without requiring a second host model to exist in test fixtures.
        field = CustomObjectTypeField.objects.create(
            custom_object_type=cot,
            name='related_polymorphic',
            label='Related Poly',
            type=CustomFieldTypeChoices.TYPE_OBJECT,
            is_polymorphic=True,
        )
        field.related_object_types.set([site_ct])

        ct_ids = _discover_target_content_type_ids()

        self.assertIn(site_ct.pk, ct_ids)

    def test_no_fields_yields_no_targets(self):
        # Any pre-existing COT fields from other test runs are isolated by
        # TransactionCleanupMixin's setup, so without creating any new
        # OBJECT/MULTIOBJECT field the discovery should return an empty set.
        ct_ids = _discover_target_content_type_ids()
        self.assertEqual(ct_ids, set())


class ResolveModelClassesTests(TestCase):
    """``_resolve_model_classes()`` skips unknown ContentTypes and dedups."""

    def test_returns_unique_model_classes(self):
        site_ct = ContentType.objects.get_for_model(Site)
        result = _resolve_model_classes({site_ct.pk})

        labels = {(m._meta.app_label, m._meta.model_name) for m in result}
        self.assertIn(('dcim', 'site'), labels)

    def test_empty_input_returns_empty(self):
        self.assertEqual(_resolve_model_classes(set()), [])

    def test_skips_nonexistent_content_type(self):
        # Pass a deliberately-invalid CT id; should be skipped silently.
        result = _resolve_model_classes({-999999})
        self.assertEqual(result, [])


class IsOurTabNameTests(TestCase):
    """``_is_our_tab_name()`` correctly identifies the names we register."""

    def test_recognises_combined_name(self):
        self.assertTrue(_is_our_tab_name(_COMBINED_NAME))

    def test_recognises_typed_prefix(self):
        self.assertTrue(_is_our_tab_name(_TYPED_NAME_PREFIX + 'my-cot'))

    def test_rejects_unrelated_names(self):
        self.assertFalse(_is_our_tab_name('changelog'))
        self.assertFalse(_is_our_tab_name('journal'))
        self.assertFalse(_is_our_tab_name('custom_objects-not-ours'))
        self.assertFalse(_is_our_tab_name(''))


class PurgeRegistryTests(TestCase):
    """``_purge_tab_entries()`` removes only our entries; preserves siblings."""

    def test_removes_our_entries_and_leaves_others(self):
        from netbox.registry import registry

        marker_app = '__related_tabs_purge_test__'
        marker_model = 'fakemodel'
        # Fixture: three entries — combined (ours), typed (ours), changelog (not ours)
        registry['views'].setdefault(marker_app, {})[marker_model] = [
            {'name': _COMBINED_NAME, 'path': 'custom-objects', 'view': object(), 'detail': True, 'kwargs': {}},
            {
                'name': _TYPED_NAME_PREFIX + 'foo',
                'path': 'custom-objects-foo',
                'view': object(),
                'detail': True,
                'kwargs': {},
            },
            {'name': 'changelog', 'path': 'changelog', 'view': object(), 'detail': True, 'kwargs': {}},
        ]
        try:
            _purge_tab_entries()
            remaining = registry['views'][marker_app][marker_model]
            names = [e['name'] for e in remaining]
            self.assertEqual(names, ['changelog'])
        finally:
            del registry['views'][marker_app]


class ViewTabVisibleTests(TransactionCleanupMixin, CustomObjectsTestCase, TransactionTestCase):
    """ViewTab.visible() reads show_dedicated_tab live per render."""

    def _make_tab_view(self, cot):
        """Build a one-off typed-tab view for the given COT."""
        from netbox_custom_objects.related_tabs.views.typed import _make_typed_tab_view

        return _make_typed_tab_view(
            model_class=Site,
            custom_object_type=cot,
            field_infos=[('site_ref', CustomFieldTypeChoices.TYPE_OBJECT, 'Site Ref', False, None)],
            weight=2100,
            host_ct_id=ContentType.objects.get_for_model(Site).pk,
        )

    def test_visible_true_when_show_dedicated_is_true(self):
        cot = CustomObjectType.objects.create(
            name='visible_test_a',
            slug='visible-test-a',
            verbose_name_plural='Visible Test As',
            show_dedicated_tab=True,
        )
        view_class = self._make_tab_view(cot)
        site = Site.objects.create(name='Visible Site A', slug='visible-site-a')

        self.assertTrue(view_class.tab.visible(site))

    def test_visible_false_when_show_dedicated_is_false(self):
        cot = CustomObjectType.objects.create(
            name='visible_test_b',
            slug='visible-test-b',
            verbose_name_plural='Visible Test Bs',
            show_dedicated_tab=False,
        )
        view_class = self._make_tab_view(cot)
        site = Site.objects.create(name='Visible Site B', slug='visible-site-b')

        self.assertFalse(view_class.tab.visible(site))

    def test_visible_false_when_cot_deleted(self):
        cot = CustomObjectType.objects.create(
            name='visible_test_c',
            slug='visible-test-c',
            verbose_name_plural='Visible Test Cs',
            show_dedicated_tab=True,
        )
        view_class = self._make_tab_view(cot)
        site = Site.objects.create(name='Visible Site C', slug='visible-site-c')

        # Capture the view first, then delete the COT — the closure still
        # references its old pk, but the row is gone.
        cot_pk = cot.pk
        cot.delete()
        self.assertFalse(CustomObjectType.objects.filter(pk=cot_pk).exists())

        # visible() must fail closed, not raise.
        self.assertFalse(view_class.tab.visible(site))


class SignalRefreshTests(TransactionCleanupMixin, CustomObjectsTestCase, TransactionTestCase):
    """Signals on COT + COTField + m2m_changed schedule force_local_refresh."""

    def test_cot_save_triggers_local_version_bump(self):
        import netbox_custom_objects.related_tabs as rt

        before = rt._tab_registry_version
        cot = CustomObjectType.objects.create(
            name='signal_test_save',
            slug='signal-test-save',
            verbose_name_plural='Signal Test Saves',
        )

        # TransactionTestCase commits after each statement, so the
        # on_commit callback should have fired.
        self.assertGreater(rt._tab_registry_version, before)
        self.assertTrue(CustomObjectType.objects.filter(pk=cot.pk).exists())

    def test_m2m_changed_on_related_object_types_triggers_refresh(self):
        import netbox_custom_objects.related_tabs as rt

        cot = CustomObjectType.objects.create(
            name='signal_test_m2m',
            slug='signal-test-m2m',
            verbose_name_plural='Signal Test M2Ms',
        )
        field = CustomObjectTypeField.objects.create(
            custom_object_type=cot,
            name='related_poly',
            label='Related Poly',
            type=CustomFieldTypeChoices.TYPE_OBJECT,
            is_polymorphic=True,
        )

        before = rt._tab_registry_version
        site_ct = ContentType.objects.get_for_model(Site)
        field.related_object_types.add(site_ct)

        # on_commit fires after the M2M add commits.
        self.assertGreater(rt._tab_registry_version, before)

    def test_dispatch_uids_idempotent(self):
        """Connect signals twice — should not duplicate registrations."""
        from django.db.models.signals import post_delete
        from netbox_custom_objects.related_tabs.signals import connect

        uids = {
            'related_tabs_refresh_on_save_cot',
            'related_tabs_refresh_on_save_cotfield',
            'related_tabs_refresh_on_delete_cot',
            'related_tabs_refresh_on_delete_cotfield',
            'related_tabs_refresh_on_cotfield_related_object_types_changed',
        }

        def _count_with_uids():
            count = 0
            for signal in (post_save, post_delete, m2m_changed):
                for entry in signal.receivers:
                    # When dispatch_uid is provided, Django uses it directly
                    # as lookup_key[0] — a plain string, not a hash.
                    if entry[0][0] in uids:
                        count += 1
            return count

        # Ensure a non-zero baseline so the test would actually fail if
        # dispatch_uid de-dup broke (otherwise before == after == 0 passes
        # vacuously regardless of dedup behaviour).
        connect()
        before = _count_with_uids()
        self.assertEqual(before, len(uids))

        connect()
        after = _count_with_uids()
        self.assertEqual(before, after)


class RefreshIfStaleTests(TransactionCleanupMixin, CustomObjectsTestCase, TransactionTestCase):
    """``refresh_if_stale()`` is a no-op when local == remote and refreshes when behind."""

    def setUp(self):
        super().setUp()
        from django.core.cache import cache

        import netbox_custom_objects.related_tabs as rt

        cache.set(_REDIS_KEY, 1, timeout=None)
        rt._tab_registry_version = 1

    def test_no_op_when_versions_match(self):
        # Should return False (didn't refresh).
        self.assertFalse(refresh_if_stale())

    def test_refresh_runs_when_local_behind(self):
        from django.core.cache import cache

        import netbox_custom_objects.related_tabs as rt

        cache.set(_REDIS_KEY, rt._tab_registry_version + 5, timeout=None)
        self.assertTrue(refresh_if_stale())
        # After refresh, local catches up to remote.
        self.assertEqual(rt._tab_registry_version, rt._get_remote_version())
