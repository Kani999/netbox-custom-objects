"""
Tests for the related_tabs subpackage (combined "Custom Objects" tab).

Focused on the surfaces most likely to regress:

* ``reference_q()`` builds the correct filter per field kind, and returns an
  EMPTY Q (which callers must treat as "skip", never as match-all) for an
  unsupported field type or an unresolvable polymorphic through model. A
  regression here would leak every custom object of a type onto every host page.
* ``_discover_target_content_type_ids()`` auto-discovery from CustomObjectTypeField
  rows (both non-polymorphic via ``related_object_type`` and polymorphic via the
  ``related_object_types`` M2M).
* ``_resolve_model_classes()`` skips unknown ContentTypes and dedups.
* ``custom_objects_tab_link`` renders the combined tab on a custom-object host
  page LIVE from the DB, so a CustomObjectType created after startup (a CO→CO
  reference) gets a tab with no NetBox restart and no startup registration.
"""

from core.models import ObjectType
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.test import TestCase, TransactionTestCase
from extras.choices import CustomFieldTypeChoices

from dcim.models import Site

from netbox_custom_objects.models import CustomObjectType, CustomObjectTypeField
from netbox_custom_objects.related_tabs.registry import (
    _discover_target_content_type_ids,
    _resolve_model_classes,
)
from netbox_custom_objects.related_tabs.views._co_common import reference_q
from netbox_custom_objects.tests.base import CustomObjectsTestCase, TransactionCleanupMixin


class ReferenceQTests(TestCase):
    """
    ``reference_q()`` builds the correct filter per field kind, and — critically —
    returns an EMPTY Q (which callers must treat as "skip", never as match-all) for
    an unsupported field type or an unresolvable polymorphic through model. A
    regression here would leak every custom object of a type onto every host page.
    """

    def test_object_non_polymorphic(self):
        self.assertEqual(
            reference_q(1, 42, 'site', CustomFieldTypeChoices.TYPE_OBJECT, False, None),
            Q(site_id=42),
        )

    def test_object_polymorphic(self):
        self.assertEqual(
            reference_q(7, 42, 'thing', CustomFieldTypeChoices.TYPE_OBJECT, True, None),
            Q(thing_content_type_id=7, thing_object_id=42),
        )

    def test_multiobject_non_polymorphic(self):
        self.assertEqual(
            reference_q(1, 42, 'sites', CustomFieldTypeChoices.TYPE_MULTIOBJECT, False, None),
            Q(sites=42),
        )

    def test_unsupported_field_type_returns_empty_q(self):
        q = reference_q(1, 42, 'x', CustomFieldTypeChoices.TYPE_TEXT, False, None)
        self.assertFalse(q.children)  # empty Q == "skip", NOT match-all

    def test_unresolvable_through_returns_empty_q(self):
        # Polymorphic MULTIOBJECT whose through model isn't in the app registry.
        with self.assertLogs('netbox_custom_objects.related_tabs', level='ERROR'):
            q = reference_q(1, 42, 'x', CustomFieldTypeChoices.TYPE_MULTIOBJECT, True, 'Through_does_not_exist')
        self.assertFalse(q.children)


class DiscoveryTests(TransactionCleanupMixin, CustomObjectsTestCase, TransactionTestCase):
    """``_discover_target_content_type_ids()`` finds non-poly + poly targets."""

    def test_non_polymorphic_target_is_discovered(self):
        cot = CustomObjectType.objects.create(
            name='disc_test_a',
            slug='disc-test-a',
            verbose_name_plural='Disc Test As',
        )
        # related_object_type is a FK to core.ObjectType (a strict subclass of
        # ContentType in NetBox); a plain ContentType instance is rejected.
        site_ot = ObjectType.objects.get_for_model(Site)
        CustomObjectTypeField.objects.create(
            custom_object_type=cot,
            name='related_site',
            label='Related Site',
            type=CustomFieldTypeChoices.TYPE_OBJECT,
            is_polymorphic=False,
            related_object_type=site_ot,
        )

        ct_ids = _discover_target_content_type_ids()

        self.assertIn(site_ot.pk, ct_ids)

    def test_polymorphic_targets_are_discovered(self):
        cot = CustomObjectType.objects.create(
            name='disc_test_b',
            slug='disc-test-b',
            verbose_name_plural='Disc Test Bs',
        )
        site_ot = ObjectType.objects.get_for_model(Site)
        # Use site as the only poly target; verifies the M2M code path
        # without requiring a second host model to exist in test fixtures.
        field = CustomObjectTypeField.objects.create(
            custom_object_type=cot,
            name='related_polymorphic',
            label='Related Poly',
            type=CustomFieldTypeChoices.TYPE_OBJECT,
            is_polymorphic=True,
        )
        field.related_object_types.set([site_ot])

        ct_ids = _discover_target_content_type_ids()

        self.assertIn(site_ot.pk, ct_ids)

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


class CoToCoLiveTabLinkTests(TransactionCleanupMixin, CustomObjectsTestCase, TransactionTestCase):
    """
    ``custom_objects_tab_link`` renders the combined tab on a custom-object host
    page live from the DB — the CO→CO case the startup view registry can't cover.

    Regression for the smoke-test S2 finding: a CustomObjectType (B) created after
    startup that references another CustomObjectType (A) must surface a 'Custom
    Objects' tab on A's detail page WITHOUT a restart and WITHOUT A's dynamic model
    being in the startup view registry.
    """

    def setUp(self):
        super().setUp()
        from django.contrib.auth import get_user_model
        from netbox_custom_objects.related_tabs.registry import CO_COMBINED_URL_NAME, _inject_co_urls

        # Inject the generic CO combined-tab URL exactly as ready() does at startup,
        # so get_action_url() inside the tag can reverse it.  Idempotent.
        _inject_co_urls()
        self.url_name = CO_COMBINED_URL_NAME
        self.user = get_user_model().objects.create_user(username='cotabtest', password='x')

    def _build_a_referenced_by_b(self):
        """Create COT A (+1 instance) and COT B with an Object field -> A linking to it."""
        cot_a = self.create_custom_object_type(name='live_a', slug='live-a')
        self.create_custom_object_type_field(cot_a, name='name', label='Name', type='text', primary=True)
        model_a = cot_a.get_model()
        a1 = model_a.objects.create(name='a-1')

        cot_b = self.create_custom_object_type(name='live_b', slug='live-b')
        self.create_custom_object_type_field(cot_b, name='name', label='Name', type='text', primary=True)
        self.create_custom_object_type_field(
            cot_b,
            name='ref_a',
            label='Ref A',
            type=CustomFieldTypeChoices.TYPE_OBJECT,
            is_polymorphic=False,
            related_object_type=cot_a.object_type,
        )
        model_b = cot_b.get_model()
        model_b.objects.create(name='b-1', ref_a=a1)
        return a1

    def _render_link(self, instance):
        from netbox_custom_objects.templatetags.custom_object_tab_tags import custom_objects_tab_link

        request = self.client.request().wsgi_request
        request.user = self.user
        # The inclusion tag reads context['request'] and context.get('tab').
        return custom_objects_tab_link({'request': request, 'tab': None}, instance)

    def test_link_rendered_for_co_referenced_by_another_cot(self):
        a1 = self._build_a_referenced_by_b()
        result = self._render_link(a1)
        self.assertIsNotNone(result['tab'], 'combined tab link should render for a referenced custom object')
        self.assertEqual(result['tab']['badge'], 1)
        self.assertEqual(result['tab']['label'], 'Custom Objects')
        # URL must reverse to the generic CO combined route for THIS slug.
        self.assertIn('/custom-objects/', result['tab']['url'])

    def test_no_link_when_unreferenced(self):
        # A custom object that nothing references → badge 0 → no tab (hide_if_empty).
        cot = self.create_custom_object_type(name='live_lonely', slug='live-lonely')
        self.create_custom_object_type_field(cot, name='name', label='Name', type='text', primary=True)
        lonely = cot.get_model().objects.create(name='lonely-1')
        result = self._render_link(lonely)
        self.assertIsNone(result['tab'], 'no tab should render when nothing links to the object')
