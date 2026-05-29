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
