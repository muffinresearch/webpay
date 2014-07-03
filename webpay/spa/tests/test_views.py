import os

from django import test
from django.conf import settings
from django.core.urlresolvers import reverse

import mock
from nose.tools import eq_, ok_
from pyquery import PyQuery as pq
from slumber.exceptions import HttpClientError

from webpay.base import dev_messages as msg
from webpay.base.tests import BasicSessionCase
from webpay.provider.tests.test_views import ProviderTestCase


class TestSpaViewsMeta(type):
    """Dynamically generate tests for Spartacus views."""

    def __new__(mcs, name, bases, dict):

        def gen_test(self):
            def chk_view(self):
                res = self.client.get(url)
                eq_(res.status_code, 200)
                # Ensure this is serving the Spartacus template.
                ok_('class="spartacus"' in res.content)
            return chk_view

        for url in settings.SPA_URLS + ['/mozpay/']:
            test_name = 'test_%s' % url.replace('/', '').replace('-', '_')
            test_method = gen_test(os.path.join(settings.SPA_BASE_URL, url))
            test_method.__name__ = test_name
            dict[test_name] = test_method

        return type.__new__(mcs, name, bases, dict)


@mock.patch.object(settings, 'SPA_ENABLE', True)
class TestSpaViews(test.TestCase):
    __metaclass__ = TestSpaViewsMeta


@mock.patch('webpay.base.utils.spartacus_build_id')
@test.utils.override_settings(SPA_ENABLE=True, SPA_ENABLE_URLS=True)
class TestSpartacusCacheBusting(test.TestCase):
    def test_build_id_is_set(self, spartacus_build_id):
        build_id = 'the-build-id-for-spartacus'
        spartacus_build_id.return_value = build_id
        url = reverse('pay.lobby')
        response = test.Client().get(url)
        doc = pq(response.content)
        build_id_from_dom = doc('body').attr('data-build-id')
        eq_(build_id_from_dom, build_id)


@test.utils.override_settings(SPA_ENABLE=True, SPA_ENABLE_URLS=True)
class TestWaitToFinish(ProviderTestCase):

    def setUp(self):
        super(TestWaitToFinish, self).setUp()
        self.wait_url = reverse('spa.complete_payment', args=['boku'])

    def wait(self, trans_uuid=None):
        if not trans_uuid:
            trans_uuid = self.trans_id
        return self.client.get('{u}?param={t}'.format(u=self.wait_url,
                                                      t=trans_uuid))

    def test_wait_for_boku_transaction(self):
        res = self.wait()
        url = reverse('provider.transaction_status', args=[self.trans_id])
        eq_(res.context['transaction_status_url'], url)
        eq_(res.status_code, 200)

    def test_missing_transaction(self):
        res = self.client.get('{u}?foo=bar'.format(u=self.wait_url))
        eq_(res.status_code, 404)

    @test.utils.override_settings(SPA_ENABLE=False)
    def test_missing_transaction_spa_disabled(self):
        res = self.client.get('{u}?param={t}'.format(u=self.wait_url,
                                                     t=self.trans_id))
        eq_(res.status_code, 403)


@test.utils.override_settings(SPA_ENABLE=True, SPA_ENABLE_URLS=True)
@mock.patch('webpay.bango.views.client.slumber')
@mock.patch('webpay.bango.views.tasks.payment_notify')
class TestBangoReturn(BasicSessionCase):

    def setUp(self):
        super(TestBangoReturn, self).setUp()
        # Log in.
        self.session['uuid'] = 'verified-user'
        # Start a payment.
        self.trans_uuid = 'solitude-trans-uuid'
        self.session['trans_id'] = self.trans_uuid
        self.session['notes'] = {'pay_request': '<request>',
                                 'issuer_key': '<issuer>'}
        self.save_session()

    def call(self, overrides=None, expected_status=200,
             url='spa.complete_payment'):
        qs = {'MozSignature': 'xyz',
              'MerchantTransactionId': self.trans_uuid,
              'BillingConfigurationId': '123',
              'ResponseCode': 'OK',
              'Price': '0.99',
              'Currency': 'EUR',
              'BangoTransactionId': '456',
              'Token': '<bango-guid>'}
        if overrides:
            qs.update(overrides)
        res = self.client.get(reverse(url, args=['bango']), qs)
        eq_(res.status_code, expected_status)
        return res

    def test_good_return(self, payment_notify, slumber):
        self.call()
        payment_notify.delay.assert_called_with(self.trans_uuid)

    def test_invalid_return(self, payment_notify, slumber):
        err = HttpClientError
        err.content = ''
        slumber.bango.notification.post.side_effect = err
        res = self.call(expected_status=302)
        redir_url = reverse('spa.payment_failure',
                            args=['bango', 'NOTICE_ERROR'])
        self.assertRedirects(res, redir_url, status_code=302,
                             target_status_code=400)
        assert not payment_notify.delay.called

    def test_transaction_not_in_session(self, payment_notify, slumber):
        del self.session['trans_id']
        self.save_session()

        self.call(overrides={'MerchantTransactionId': 'invalid-trans'},
                  expected_status=200)
        assert slumber.bango.notification.post.called

    def test_transaction_in_session_differs(self, payment_notify, slumber):
        res = self.call(overrides={'MerchantTransactionId': 'invalid-trans'},
                        expected_status=302)
        redir_url = reverse('spa.payment_failure',
                            args=['bango', 'NO_ACTIVE_TRANS'])
        self.assertRedirects(res, redir_url, status_code=302,
                             target_status_code=400)
        assert not slumber.bango.notification.post.called

    def test_error(self, payment_notify, slumber):
        res = self.call(overrides={'ResponseCode': 'NOT OK'},
                        url='spa.payment_error', expected_status=302)
        assert slumber.bango.notification.post.called
        redir_url = reverse('spa.payment_failure',
                            args=['bango', 'BANGO_ERROR'])
        self.assertRedirects(res, redir_url, status_code=302,
                             target_status_code=400)

    def test_cancel(self, payment_notify, slumber):
        res = self.call(overrides={'ResponseCode': 'CANCEL'},
                        url='spa.payment_error',
                        expected_status=302)
        redir_url = reverse('spa.payment_failure',
                            args=['bango', 'USER_CANCELLED'])
        self.assertRedirects(res, redir_url, status_code=302,
                             target_status_code=400)

    def test_not_error(self, payment_notify, slumber):
        res = self.call(overrides={'ResponseCode': 'OK'},
                        url='spa.payment_error', expected_status=302)
        redir_url = reverse('spa.payment_failure',
                            args=['bango', 'BAD_BANGO_CODE'])
        self.assertRedirects(res, redir_url, status_code=302,
                             target_status_code=400)

    def test_bad_tier(self, payment_notify, slumber):
        res = self.call(overrides={'ResponseCode': 'NOT_SUPPORTED'},
                  url='spa.payment_error', expected_status=302)
        redir_url = reverse('spa.payment_failure',
                            args=['bango', 'UNSUPPORTED_PAY'])
        self.assertRedirects(res, redir_url, status_code=302,
                             target_status_code=400)

    def test_not_ok(self, payment_notify, slumber):
        res = self.call(overrides={'ResponseCode': 'NOT_OK'},
                        url='spa.complete_payment', expected_status=302)
        redir_url = reverse('spa.payment_failure',
                            args=['bango', 'BAD_BANGO_CODE'])
        self.assertRedirects(res, redir_url, status_code=302,
                             target_status_code=400)


@test.utils.override_settings(SPA_ENABLE=True, SPA_ENABLE_URLS=True)
@mock.patch.object(settings, 'PAYMENT_PROVIDER', 'reference')
class TestProviderSuccess(ProviderTestCase):

    def trust_notice(self):
        post = self.slumber.provider.reference.notices.post
        post.return_value = {'result': 'OK'}

    def callback(self, data=None, clear_qs=False, view=None):
        assert view, 'not sure which view to use'
        params = {'ext_transaction_id': self.trans_id}
        if clear_qs:
            params.clear()
        if data:
            params.update(data)
        return self.client.get(view, params)

    def error(self, **kw):
        kw['view'] = reverse('spa.payment_error', args=['reference'])
        return self.callback(**kw)

    def success(self, **kw):
        kw['view'] = reverse('spa.complete_payment', args=['reference'])
        return self.callback(**kw)

    def test_missing_ext_trans(self):
        self.trust_notice()
        res = self.success(clear_qs=True)
        redir_url = reverse('spa.payment_failure',
                            args=['reference', 'TRANS_MISSING'])
        self.assertRedirects(res, redir_url, status_code=302,
                             target_status_code=400)

    def test_missing_session_trans(self):
        self.trust_notice()
        del self.session['trans_id']
        self.save_session()
        res = self.success()
        redir_url = reverse('spa.payment_failure',
                            args=['reference', 'NO_ACTIVE_TRANS'])
        self.assertRedirects(res, redir_url, status_code=302,
                             target_status_code=400)

    def test_success(self):
        self.trust_notice()
        res = self.success()
        eq_(res.status_code, 200)
        self.payment_notify.delay.assert_called_with(self.trans_id)
        self.assertTemplateUsed(res, 'spa/index.html')

    def test_error(self):
        self.trust_notice()
        res = self.error()
        assert not self.payment_notify.delay.called, (
            'did not expect a notification on error')
        redir_url = reverse('spa.payment_failure',
                            args=['reference', 'EXT_ERROR'])
        self.assertRedirects(res, redir_url, status_code=302,
                             target_status_code=400)

    def test_invalid_notice_on_success(self):
        post = self.slumber.provider.reference.notices.post
        post.return_value = {'result': 'FAIL'}

        res = self.success()
        redir_url = reverse('spa.payment_failure',
                            args=['reference', 'NOTICE_ERROR'])
        self.assertRedirects(res, redir_url, status_code=302,
                             target_status_code=400)

    def test_notice_failure(self):
        post = self.slumber.provider.reference.notices.post
        post.side_effect = HttpClientError('bad stuff')

        res = self.success()
        redir_url = reverse('spa.payment_failure',
                            args=['reference', 'NOTICE_EXCEPTION'])
        self.assertRedirects(res, redir_url, status_code=302,
                             target_status_code=400)

    def test_invalid_notice_on_error(self):
        post = self.slumber.provider.reference.notices.post
        post.return_value = {'result': 'FAIL'}
        res = self.error()
        redir_url = reverse('spa.payment_failure',
                            args=['reference', 'NOTICE_ERROR'])
        self.assertRedirects(res, redir_url, status_code=302,
                             target_status_code=400)


@test.utils.override_settings(SPA_ENABLE=True, SPA_ENABLE_URLS=True)
class TestSpaDataAttrs(test.TestCase):

    def test_has_bango_logout_url(self):
        res = self.client.get('/mozpay/')
        eq_(res.status_code, 200)
        doc = pq(res.content)
        eq_(doc('body').attr('data-bango-logout-url'),
            settings.PAY_URLS['bango']['base'] +
            settings.PAY_URLS['bango']['logout'])


@test.utils.override_settings(SPA_ENABLE=True, SPA_ENABLE_URLS=True)
class TestView404(test.TestCase):

    def test_404_if_unknown_provider_payment_failure(self):
        res = self.client.get(reverse('spa.payment_failure',
                                      args=['whatever', 'NOTICE_EXCEPTION']))
        eq_(res.status_code, 404)

    def test_404_if_unknown_provider_payment_success(self):
        res = self.client.get(reverse('spa.complete_payment',
                                      args=['whatever']))
        eq_(res.status_code, 404)

    def test_404_if_unknown_provider_payment_error(self):
        res = self.client.get(reverse('spa.payment_error',
                                      args=['whatever']))
        eq_(res.status_code, 404)
