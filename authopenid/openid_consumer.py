from __future__ import absolute_import

from base64 import b64decode, b64encode
from contextlib import contextmanager
from urlparse import urlparse, urlunparse
try:
    import cPickle as pickle
except ImportError:                     # pragma: no cover
    import pickle

from trac.config import BoolOption
from trac.core import Component, ExtensionPoint, implements
from trac.db.api import DatabaseManager
from trac.env import IEnvironmentSetupParticipant

import openid.consumer.consumer
from openid.consumer.consumer import (
    DiscoveryFailure,
    SUCCESS, FAILURE, CANCEL, SETUP_NEEDED,
    )
from openid import oidutil
import openid.store.memstore
import openid.store.sqlstore

from authopenid.exceptions import (
    LoginError,
    AuthenticationFailed,
    AuthenticationCancelled,
    SetupNeeded,
    )
from authopenid.interfaces import IOpenIDConsumer, IOpenIDExtensionProvider

# XXX: It looks like python-openid is going to switch to using the
# stock logging module.  We'll need to detect when that happens.
@contextmanager
def openid_logging_to(log):
    """ Capture logging from python-openid to the trac log.
    """
    def log_to_trac_log(message, level=0):
        # XXX: What level to log at?
        # The level argument is unused python-openid.  Log messages
        # generated by python-openid seem to range from INFO to ERROR
        # severity, but there is no good way to distinguish which is which.
        log.warning("%s", message)

    save_log, oidutil.log = oidutil.log, log_to_trac_log
    try:
        yield
    finally:
        oidutil.log = save_log

def _session_mutator(method):
    def wrapped(self, *args):
        rv = method(self, *args)
        self.save()
        return rv
    try:
        wrapped.__name__ = method.__name__
    except:                             # pragma: no cover
        pass
    return wrapped

class PickleSession(dict):
    """ A session dict that can store any kind of object.

    (The trac req.session can only store ``unicode`` values.)
    """

    def __init__(self, req, skey):
        self.req = req
        self.skey = skey
        try:
            data = b64decode(req.session[self.skey])
            self.update(pickle.loads(data))
        except (KeyError, TypeError, pickle.UnpicklingError):
            pass

    def save(self):
        session = self.req.session
        if len(self) > 0:
            data = pickle.dumps(dict(self), pickle.HIGHEST_PROTOCOL)
            session[self.skey] = b64encode(data)
        elif self.skey in session:
            del session[self.skey]

    __setitem__ = _session_mutator(dict.__setitem__)
    __delitem__ = _session_mutator(dict.__delitem__)
    clear = _session_mutator(dict.clear)
    pop = _session_mutator(dict.pop)
    popitem = _session_mutator(dict.popitem)
    setdefault = _session_mutator(dict.setdefault)
    update = _session_mutator(dict.update)


STORE_CLASSES = {
    'sqlite': openid.store.sqlstore.SQLiteStore,
    'mysql': openid.store.sqlstore.MySQLStore,
    'postgres': openid.store.sqlstore.PostgreSQLStore,
    }

@contextmanager
def openid_store(env, db=None):
    """ Get a suitable openid store
    """
    dburi = DatabaseManager(env).connection_uri
    scheme = dburi.split(':', 1)[0]
    try:
        store_class = STORE_CLASSES[scheme]
    except KeyError:
        # no store class for database type, punt...
        yield openid.store.memstore.MemoryStore()
    else:
        if db:
            yield store_class(db.cnx.cnx)
        else:
            with env.db_transaction as db_:
                yield store_class(db_.cnx.cnx)

# FIXME: needed?
# class NonCommittingConnectionWrapper(ConnectionWrapper):
#     """ A connection proxy which intercepts ``commit` and ``rollback`` messages
#     """
#     def commit(self):
#         pass

#     def rollback(self):
#         pass


class OpenIDConsumer(Component):

    implements(IOpenIDConsumer, IEnvironmentSetupParticipant)


    absolute_trust_root = BoolOption(
        'openid', 'absolute_trust_root', 'true',
        doc="""Does OpenID realm include the whole site, or just the project

        If true (the default) then a url to the root of the whole site
        will be sent for the OpenID realm.  Thus when a user approves
        authentication, he will be approving it for all trac projects
        on the site.

        Set to false to send a realm which only includes the current trac
        project.
        """)

    openid_extension_providers = ExtensionPoint(IOpenIDExtensionProvider)

    consumer_class = openid.consumer.consumer.Consumer # testing

    consumer_skey = 'openid_session_data'

    # IOpenIDConsumer methods

    def begin(self, req, identifier, return_to,
              trust_root=None, immediate=False):

        log = self.env.log

        if not identifier:
            raise LoginError("Enter an OpenID Identifier")

        if trust_root is None:
            trust_root = self._get_trust_root(req)

        session = PickleSession(req, self.consumer_skey)
        with openid_store(self.env) as store:
            with openid_logging_to(log):
                consumer = self.consumer_class(session, store)
                try:
                    # FIXME: raises ProtocolError?
                    auth_request = consumer.begin(identifier)
                except DiscoveryFailure, exc:
                    raise LoginError("OpenID discovery failed: %s" % exc)

                for provider in self.openid_extension_providers:
                    provider.add_to_auth_request(req, auth_request)

                if auth_request.shouldSendRedirect():
                    redirect_url = auth_request.redirectURL(
                        trust_root, return_to, immediate=immediate)
                    log.debug('Redirecting to: %s' % redirect_url)
                    req.redirect(redirect_url)  # noreturn (raises RequestDone)
                else:
                    # return an auto-submit form
                    form_html = auth_request.htmlMarkup(
                        trust_root, return_to, immediate=immediate)
                    req.send(form_html, 'text/html')

    def complete(self, req, current_url=None):
        if current_url is None:
            current_url = req.abs_href(req.path_info)

        session = PickleSession(req, self.consumer_skey)
        with openid_store(self.env) as store:
            with openid_logging_to(self.env.log):
                consumer = self.consumer_class(session, store)
                response = consumer.complete(req.args, current_url)

                if response.status != SETUP_NEEDED:
                    session.clear()

                if response.status == FAILURE:
                    raise AuthenticationFailed(
                        response.message, response.identity_url)
                elif response.status == CANCEL:
                    raise AuthenticationCancelled()
                elif response.status == SETUP_NEEDED:
                    raise SetupNeeded(response.setup_url)
                assert response.status == SUCCESS

                if response.endpoint.canonicalID:
                    # You should authorize i-name users by their
                    # canonicalID, rather than their more
                    # human-friendly identifiers.  That way their
                    # account with you is not compromised if their
                    # i-name registration expires and is bought by
                    # someone else.

                    # FIXME: is this right?
                    identifier = response.endpoint.canonicalID
                else:
                    identifier = response.identity_url

                # FIXME: strip_protocol, strip_trailing_slash?
                # These used to be applied before checking white/blacklists

                extension_data = {}
                for provider in self.openid_extension_providers:
                    ext_data = provider.parse_response(response)
                    if ext_data:
                        extension_data.update(ext_data)

                return identifier, extension_data

    def _get_trust_root(self, req):
        root = urlparse(req.abs_href() + '/')
        assert root.scheme and root.netloc
        path = root.path
        if self.absolute_trust_root:
            path = '/'
        else:
            path = root.path
        if not path.endswith('/'):
            path += '/'
        return urlunparse((root.scheme, root.netloc, path) + (None,) * 3)

    # IEnvironmentSetupParticipant methods

    # FIXME: save versioning info in system table?

    def environment_created(self):
        with openid_store(self.env) as store:
            if hasattr(store, 'createTables'):
                store.createTables()

    def environment_needs_upgrade(self, db):
        with openid_store(self.env, db) as store:
            if hasattr(store, 'createTables'):
                c = db.cursor()
                try:
                    c.execute("SELECT count(*) FROM oid_associations")
                except Exception, e:
                    if hasattr(db, 'rollback'):
                        db.rollback()
                    return True
        return False

    def upgrade_environment(self, db):
        with openid_store(self.env, db) as store:
            if hasattr(store, 'createTables'):
                store.createTables()
