from django.db.models import ProtectedError

from amo.tests import TestCase
from mkt.inapp.models import InAppProduct
from mkt.prices.models import Price
from mkt.site.fixtures import fixture


class TestInappProduct(TestCase):
    fixtures = fixture('prices')

    def test_guid_is_auto_populated(self):
        inapp = InAppProduct.objects.create(
            name='kiwii',
            price=Price.objects.all()[0]
        )
        self.assertTrue(inapp.guid is not None)

    def test_can_not_delete_inapp_product(self):
        inapp = InAppProduct.objects.create(
            name='kiwii',
            price=Price.objects.all()[0]
        )

        with self.assertRaises(ProtectedError):
            inapp.delete()
