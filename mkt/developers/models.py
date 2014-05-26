import posixpath
import uuid

from django.conf import settings
from django.core.urlresolvers import reverse
from django.db import models

import commonware.log
from tower import ugettext as _

import amo
from constants.payments import PROVIDER_BANGO, PROVIDER_CHOICES
from lib.crypto import generate_key
from lib.pay_server import client
from mkt.constants.payments import ACCESS_SIMULATE
from users.models import UserForeignKey


log = commonware.log.getLogger('z.devhub')


class CantCancel(Exception):
    pass


class SolitudeSeller(amo.models.ModelBase):
    # TODO: When Solitude allows for it, this should be updated to be 1:1 with
    # users.
    user = UserForeignKey()
    uuid = models.CharField(max_length=255, unique=True)
    resource_uri = models.CharField(max_length=255)

    class Meta:
        db_table = 'payments_seller'

    @classmethod
    def create(cls, user):
        uuid_ = str(uuid.uuid4())
        res = client.api.generic.seller.post(data={'uuid': uuid_})
        uri = res['resource_uri']
        obj = cls.objects.create(user=user, uuid=uuid_, resource_uri=uri)

        log.info('[User:%s] Created Solitude seller (uuid:%s)' %
                 (user, uuid_))
        return obj


class PaymentAccount(amo.models.ModelBase):
    user = UserForeignKey()
    name = models.CharField(max_length=64)
    agreed_tos = models.BooleanField(default=False)
    solitude_seller = models.ForeignKey(SolitudeSeller)

    # These two fields can go away when we're not 1:1 with SolitudeSellers.
    seller_uri = models.CharField(max_length=255, unique=True)
    uri = models.CharField(max_length=255, unique=True)
    # A soft-delete so we can talk to Solitude asynchronously.
    inactive = models.BooleanField(default=False)
    # The id for this account from the provider.
    account_id = models.CharField(max_length=255)
    # Each account will be for a particular provider.
    provider = models.IntegerField(choices=PROVIDER_CHOICES,
                                   default=PROVIDER_BANGO)
    shared = models.BooleanField(default=False)

    class Meta:
        db_table = 'payment_accounts'
        unique_together = ('user', 'uri')

    def cancel(self, disable_refs=False):
        """Cancels the payment account.

        If `disable_refs` is set, existing apps that use this payment account
        will be set to STATUS_NULL.

        """
        account_refs = AddonPaymentAccount.objects.filter(account_uri=self.uri)
        if self.shared and account_refs:
            # With sharing a payment account comes great responsibility. It
            # would be really mean to create a payment account, share it
            # and have lots of apps use it. Then one day you remove it and
            # make a whole pile of apps in the marketplace get removed from
            # the store, or have in-app payments fail.
            #
            # For the moment I'm just stopping this completely, if this ever
            # happens, we'll have to go through a deprecation phase.
            # - let all the apps that use it know
            # - when they have all stopped sharing it
            # - re-run this
            log.error('Cannot cancel a shared payment account that has '
                      'apps using it.')
            raise CantCancel('You cannot cancel a shared payment account.')

        self.update(inactive=True)
        log.info('Soft-deleted payment account (uri: %s)' % self.uri)

        for acc_ref in account_refs:
            # Unlink the account in Solitude from the app
            self.get_provider().delete_seller_product(acc_ref)

            if (disable_refs and
                    not acc_ref.addon.has_multiple_payment_accounts()):
                log.info('Changing app status to NULL for app: {0}'
                         'because of payment account deletion'.format(
                             acc_ref.addon_id))

                acc_ref.addon.update(status=amo.STATUS_NULL)
            log.info('Deleting AddonPaymentAccount for app: {0} because of '
                     'payment account deletion'.format(acc_ref.addon_id))
            acc_ref.delete()

    def get_provider(self):
        """Returns an instance of the payment provider for this account."""
        # TODO: fix circular import. Providers imports models which imports
        # forms which imports models.
        from mkt.developers.providers import get_provider
        return get_provider(id=self.provider)

    def __unicode__(self):
        date = self.created.strftime('%m/%y')
        if not self.shared:
            return u'%s - %s' % (date, self.name)
        # L10n: {0} is the name of the account.
        return _(u'Shared Account: {0}'.format(self.name))

    def get_agreement_url(self):
        return reverse('mkt.developers.provider.agreement', args=[self.pk])


class AddonPaymentAccount(amo.models.ModelBase):
    addon = models.ForeignKey(
        'addons.Addon', related_name='app_payment_accounts')
    payment_account = models.ForeignKey(PaymentAccount)
    account_uri = models.CharField(max_length=255)
    product_uri = models.CharField(max_length=255, unique=True)

    class Meta:
        db_table = 'addon_payment_account'

    @property
    def user(self):
        return self.payment_account.user


class UserInappKey(amo.models.ModelBase):
    solitude_seller = models.ForeignKey(SolitudeSeller)
    seller_product_pk = models.IntegerField(unique=True)

    def secret(self):
        return self._product().get()['secret']

    def public_id(self):
        return self._product().get()['public_id']

    def reset(self):
        self._product().patch(data={'secret': generate_key(48)})

    @classmethod
    def create(cls, user):
        sel = SolitudeSeller.create(user)
        # Create a product key that can only be used for simulated purchases.
        prod = client.api.generic.product.post(data={
            'seller': sel.resource_uri, 'secret': generate_key(48),
            'external_id': str(uuid.uuid4()), 'public_id': str(uuid.uuid4()),
            'access': ACCESS_SIMULATE,
        })
        log.info(u'User %s created an in-app payments dev key product=%s '
                 u'with %s' % (unicode(user), prod['resource_pk'], sel))
        return cls.objects.create(solitude_seller=sel,
                                  seller_product_pk=prod['resource_pk'])

    def _product(self):
        return client.api.generic.product(self.seller_product_pk)

    class Meta:
        db_table = 'user_inapp_keys'


class PreloadTestPlan(amo.models.ModelBase):
    addon = models.ForeignKey('addons.Addon')
    last_submission = models.DateTimeField(auto_now_add=True)
    filename = models.CharField(max_length=60)
    status = models.PositiveSmallIntegerField(default=amo.STATUS_PUBLIC)

    class Meta:
        db_table = 'preload_test_plans'
        ordering = ['-last_submission']

    @property
    def preload_test_plan_url(self):
        host = (settings.PRIVATE_MIRROR_URL if self.addon.is_disabled
                else settings.LOCAL_MIRROR_URL)
        return posixpath.join(host, str(self.addon.id), self.filename)
