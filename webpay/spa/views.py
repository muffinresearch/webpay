from django import http
from django.conf import settings
from django.core.urlresolvers import reverse
from django.shortcuts import render

from django_paranoia.decorators import require_GET

from lib.solitude.api import ProviderHelper
from webpay.base.logger import getLogger
from webpay.base import dev_messages as msg
from webpay.bango.views import _record, RECORDED_OK
from webpay.pay import tasks


log = getLogger('w.spa')


@require_GET
def index(request):
    """Page that serves the static Single Page App (Spartacus)."""

    if not settings.SPA_ENABLE:
        return http.HttpResponseForbidden()

    return render(request, 'spa/index.html')


@require_GET
def payment_failure(request, provider_name, error_code):
    """Page that serves up a the SPA for a payment failure."""

    if not settings.SPA_ENABLE:
        return http.HttpResponseForbidden()

    if provider_name not in settings.PAYMENT_PROVIDERS:
        log.error('Unexpected provider {provider}, query string: {qs}'
                  .format(provider=provider_name, qs=request.GET))
        return http.HttpResponseNotFound()

    return render(request, 'spa/index.html', status=400)



@require_GET
def payment_error(request, provider_name):
    """This view is an error view called by the provider

    It should hand off to the relevant error_callback view
    which will then redirect to payment_failure on the SPA.

    Note: Boku shouldn't call this.

    """
    if not settings.SPA_ENABLE:
        return http.HttpResponseForbidden()

    if provider_name in settings.PAYMENT_PROVIDERS:
        if provider_name == 'bango':
            return _bango_error(request)
        elif provider_name == 'reference':
            return _provider_error(request, provider_name)

    log.error('Unexpected provider {provider}, query string: {qs}'
              .format(provider=provider_name, qs=request.GET))
    return http.HttpResponseNotFound()


@require_GET
def _provider_error(request, provider_name):
    """The standard provider error callback view."""
    helper = ProviderHelper(provider_name)
    if helper.name != 'reference':
        raise NotImplementedError(
            'only the reference provider is implemented so far')

    try:
        helper.prepare_notice(request)
    except msg.DevMessage as m:
        return http.HttpResponseRedirect(
            reverse('spa.payment_failure', args=[helper.name, m.code]))

    # TODO: handle user cancellation, bug 957774.
    log.error('Fatal payment error for {provider}: {code}; query string: {qs}'
              .format(provider=helper.name,
                      code=request.GET.get('ResponseCode'),
                      qs=request.GET))
    return http.HttpResponseRedirect(
        reverse('spa.payment_failure', args=[helper.name, msg.EXT_ERROR]))


@require_GET
def _bango_error(request):
    """The bango error callback view."""
    log.info('Bango error: %s' % request.GET)

    # We should NOT have OK's coming from Bango, presumably.
    if request.GET.get('ResponseCode') == 'OK':
        log.error('in error(): Invalid Bango response code: {code}'
                  .format(code=request.GET.get('ResponseCode')))
        return http.HttpResponseRedirect(
            reverse('spa.payment_failure', args=['bango', msg.BAD_BANGO_CODE]))

    result = _record(request)
    if result is not RECORDED_OK:
        return http.HttpResponseRedirect(
            reverse('spa.payment_failure', args=['bango', result]))

    if request.GET.get('ResponseCode') == 'CANCEL':
        return http.HttpResponseRedirect(
            reverse('spa.payment_failure', args=['bango', msg.USER_CANCELLED]))

    if request.GET.get('ResponseCode') == 'NOT_SUPPORTED':
        # This is a credit card or price point / region mismatch.
        # In theory users should never trigger this.
        return http.HttpResponseRedirect(
            reverse('spa.payment_failure', args=['bango', msg.UNSUPPORTED_PAY]))

    log.error('Fatal Bango error: {code}; query string: {qs}'
              .format(code=request.GET.get('ResponseCode'), qs=request.GET))
    return http.HttpResponseRedirect(
        reverse('spa.payment_failure', args=['bango', msg.BANGO_ERROR]))


@require_GET
def complete_payment(request, provider_name):
    """After the payment provider flow is finished the provider redirects here
    for completion.

    This view hands off to the correct completion view if successful serves
    a completion view from the SPA.

    If it fails it serves an error page in the SPA which allows the user to
    run `paymentFailed` after displaying an error message.

    This view loads up the spa on a specific URL.

    """
    if not settings.SPA_ENABLE:
        return http.HttpResponseForbidden()

    if provider_name in settings.PAYMENT_PROVIDERS:
        if provider_name == 'boku':
            return _poll_for_completion(request, provider_name)
        elif provider_name == 'bango':
            return _complete_bango(request)
        elif provider_name == 'reference':
            return _complete_provider(request, provider_name)

    log.error('Unexpected provider {provider}, query string: {qs}'
              .format(provider=provider_name, qs=request.GET))
    return http.HttpResponseNotFound()


@require_GET
def _complete_bango(request):
    """
    Process a redirect request after the Bango payment has completed.
    This URL endpoint is pre-arranged with Bango via the Billing Config API.

    Example request:

    ?ResponseCode=OK&ResponseMessage=Success&BangoUserId=1473894939
    &MerchantTransactionId=webpay%3a14d6a53c-fc4c-4bd1-8dc0-9f24646064b8
    &BangoTransactionId=1078692145
    &TransactionMethods=USA_TMOBILE%2cT-Mobile+USA%2cTESTPAY%2cTest+Pay
    &BillingConfigurationId=218240
    &MozSignature=
    c2cf7b937720c6e41f8b6401696cf7aef56975ebe54f8cee51eff4eb317841af
    &Currency=USD&Network=USA_TMOBILE&Price=0.99&P=
    """
    log.info('Bango success: %s' % request.GET)

    # We should only have OK's coming from Bango, presumably.
    if request.GET.get('ResponseCode') != 'OK':
        log.error('in success(): Invalid Bango response code: {code}'
                  .format(code=request.GET.get('ResponseCode')))
        return http.HttpResponseRedirect(reverse('spa.payment_failure',
                                         args=['bango', msg.BAD_BANGO_CODE]))

    result = _record(request)
    if result is not RECORDED_OK:
        return http.HttpResponseRedirect(reverse('spa.payment_failure',
                                         args=['bango', result]))

    # Signature verification was successful; fulfill the payment.
    tasks.payment_notify.delay(request.GET.get('MerchantTransactionId'))
    return render(request, 'spa/index.html')


@require_GET
def _poll_for_completion(request, provider_name):
    """This view polls for a transaction."""

    helper = ProviderHelper(provider_name)
    trans_uuid = helper.provider.transaction_from_notice(request.GET)
    if not trans_uuid:
        # This could happen if someone is tampering with the URL or if
        # the payment provider changed their URL parameters.
        log.info('no transaction found for provider {p}; url: {u}'
                 .format(p=helper.provider.name, u=request.get_full_path()))
        return http.HttpResponseNotFound()

    trans_url = reverse('provider.transaction_status', args=[trans_uuid])

    # For the SPA we are serving up the app on the complete-payment url.
    return render(request, 'spa/index.html',
                  {'transaction_status_url': trans_url})


@require_GET
def _complete_provider(request, provider_name):
    """The standard provider success callback view."""

    helper = ProviderHelper(provider_name)
    if helper.name != 'reference':
        raise NotImplementedError(
            'only the reference provider is implemented so far')

    try:
        transaction_id = helper.prepare_notice(request)
    except msg.DevMessage as m:
        return http.HttpResponseRedirect(reverse('spa.payment_failure',
                                         args=[helper.name, m.code]))

    tasks.payment_notify.delay(transaction_id)
    return render(request, 'spa/index.html')
