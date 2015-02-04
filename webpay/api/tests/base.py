from curling.lib import HttpClientError
import mock

from lib.marketplace.api import client as marketplace
from lib.solitude.api import client as solitude
from webpay.base.tests import BasicSessionCase


class Response():

    def __init__(self, code, content=''):
        self.status_code = code
        self.content = content


class BaseAPICase(BasicSessionCase):

    def setUp(self, *args, **kw):
        super(BaseAPICase, self).setUp(*args, **kw)
        self.uuid = 'fake-uuid'
        self.email = 'fake@user.com'
        self.set_session(uuid=self.uuid)
        self.set_session(logged_in_user=self.email)

        p = mock.patch.object(solitude, 'slumber', name='patched:solitude')
        self.solitude = p.start()
        self.addCleanup(p.stop)

        m = mock.patch.object(marketplace, 'api', name='patched:market')
        prices = mock.Mock()
        prices.get_object.return_value = 1
        self.marketplace = m.start()
        self.marketplace.webpay.prices.return_value = prices
        self.addCleanup(m.stop)

    def error(self, status):
        error = HttpClientError
        error.response = Response(404)
        return error
