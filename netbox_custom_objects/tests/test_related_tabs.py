"""
Tests for the related_tabs subpackage — EXPERIMENT (local-only refresh).

This branch removes the Redis-shared version counter and middleware that
``feature/related-object-tabs-v2`` uses to propagate tab-registry changes
across WSGI worker processes.  Tests that exercised those code paths
(``refresh_if_stale``, ``_REDIS_KEY``, ``_tab_registry_version``) are
intentionally removed.

What is still covered:

* ``register_tabs()`` auto-discovery from CustomObjectTypeField rows.
* ``_purge_tab_entries()`` idempotency.
* ``ViewTab.visible()`` defence-in-depth predicate.
* Signal handlers connect/idempotency.
"""

from unittest.mock import patch

from django.contrib.contenttypes.models import ContentType
from django.db.models.signals import m2m_changed, post_save
from django.test import TestCase, TransactionTestCase
from extras.choices import CustomFieldTypeChoices

from dcim.models import Site

from netbox_custom_objects.models import CustomObjectType, CustomObjectTypeField
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

        cot_pk = cot.pk
        cot.delete()
        self.assertFalse(CustomObjectType.objects.filter(pk=cot_pk).exists())

        self.assertFalse(view_class.tab.visible(site))


class SignalRefreshTests(TransactionCleanupMixin, CustomObjectsTestCase, TransactionTestCase):
    """Signals on COT + COTField + m2m_changed call local_refresh after commit."""

    def test_cot_save_triggers_local_refresh(self):
        with patch('netbox_custom_objects.related_tabs.local_refresh') as mock_refresh:
            CustomObjectType.objects.create(
                name='signal_test_save',
                slug='signal-test-save',
                verbose_name_plural='Signal Test Saves',
            )
            self.assertTrue(mock_refresh.called)

    def test_m2m_changed_on_related_object_types_triggers_refresh(self):
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

        site_ct = ContentType.objects.get_for_model(Site)
        with patch('netbox_custom_objects.related_tabs.local_refresh') as mock_refresh:
            field.related_object_types.add(site_ct)
            self.assertTrue(mock_refresh.called)

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
                    if entry[0][0] in uids:
                        count += 1
            return count

        connect()
        before = _count_with_uids()
        self.assertEqual(before, len(uids))

        connect()
        after = _count_with_uids()
        self.assertEqual(before, after)
