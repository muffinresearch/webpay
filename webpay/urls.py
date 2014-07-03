from django.conf import settings
from django.conf.urls.defaults import patterns, include, url


# Uncomment the next two lines to enable the admin:
# from django.contrib import admin
# admin.autodiscover()

# Run jingo monkey patch - see https://github.com/jbalogh/jingo#forms
import jingo.monkey
jingo.monkey.patch()

urlpatterns = patterns('',
    (r'^mozpay/auth/', include('webpay.auth.urls')),
    (r'^mozpay/bango/', include('webpay.bango.urls')),
    (r'^mozpay/provider/', include('webpay.provider.urls')),
    (r'^mozpay/services/', include('webpay.services.urls')),
    (r'^mozpay/pin/', include('webpay.pin.urls')),
    (r'^mozpay/', include('webpay.pay.urls'))
)

if settings.SPA_ENABLE_URLS:

    from webpay.spa.views import index as spa_index
    from webpay.spa.views import complete_payment
    from webpay.spa.views import payment_error
    from webpay.spa.views import payment_failure

    urlpatterns += patterns('',
        # The callback url for payment completion/success. Called by providers.
        url(r'^mozpay/spa/provider/(?P<provider_name>[a-z][^/]+)/complete-payment$',
            complete_payment, name='spa.complete_payment'),
        # The callback url for payment errors. Called by providers.
        url(r'^mozpay/spa/provider/(?P<provider_name>[a-z][^/]+)/payment-error$',
            payment_error, name='spa.payment_error'),
        # A generic payment failure view.
        url(r'^mozpay/spa/provider/(?P<provider_name>[a-z][^/]+)/payment-failure/(?P<error_code>[A-Z_]+)$',
            payment_failure, name='spa.payment_failure'),
        # All the other SPA urls.
        url(r'^mozpay/spa/(?:' + '|'.join(settings.SPA_URLS) + ')$',
            spa_index, name='spa.index'),
        url(r'^mozpay/v1/api/', include('webpay.api.urls', namespace='api'))
    )

# Test/Development only urls.
if settings.TEMPLATE_DEBUG:
    urlpatterns += patterns('',
        url(r'^', include('webpay.testing.urls')),
    )

# Ensure that 403 is routed through a view.
handler403 = 'webpay.auth.views.denied'
