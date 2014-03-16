# Copyright (c) 2011 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from time import time
from traceback import format_exc
from urllib import unquote
from uuid import uuid4
from hashlib import sha1
import hmac
import base64

from eventlet import Timeout
from swift.common.swob import Response, Request
from swift.common.swob import HTTPBadRequest, HTTPForbidden, HTTPNotFound, \
    HTTPUnauthorized

from swift.common.request_helpers import get_sys_meta_prefix
from swift.common.middleware.acl import (
    clean_acl, parse_acl, referrer_allowed, acls_from_account_info)
from swift.common.utils import cache_from_env, get_logger, \
    split_path, config_true_value, register_swift_info
from swift.proxy.controllers.base import get_account_info


class PersistentAuth(object):
    """
    Test authentication and authorization system.

    Add to your pipeline in proxy-server.conf, such as::

        [pipeline:main]
        pipeline = catch_errors cache tempauth proxy-server

    Set account auto creation to true in proxy-server.conf::

        [app:proxy-server]
        account_autocreate = true

    And add a tempauth filter section, such as::

        [filter:tempauth]
        use = egg:swift#tempauth
        user_admin_admin = admin .admin .reseller_admin
        user_test_tester = testing .admin
        user_test2_tester2 = testing2 .admin
        user_test_tester3 = testing3
        # To allow accounts/users with underscores you can base64 encode them.
        # Here is the account "under_score" and username "a_b" (note the lack
        # of padding equal signs):
        user64_dW5kZXJfc2NvcmU_YV9i = testing4


    See the proxy-server.conf-sample for more information.

    Account ACLs:
        If a swift_owner issues a POST or PUT to the account, with the
        X-Account-Access-Control header set in the request, then this may
        allow certain types of access for additional users.

        * Read-Only: Users with read-only access can list containers in the
          account, list objects in any container, retrieve objects, and view
          unprivileged account/container/object metadata.
        * Read-Write: Users with read-write access can (in addition to the
          read-only privileges) create objects, overwrite existing objects,
          create new containers, and set unprivileged container/object
          metadata.
        * Admin: Users with admin access are swift_owners and can perform
          any action, including viewing/setting privileged metadata (e.g.
          changing account ACLs).

    To generate headers for setting an account ACL::

        from swift.common.middleware.acl import format_acl
        acl_data = { 'admin': ['alice'], 'read-write': ['bob', 'carol'] }
        header_value = format_acl(version=2, acl_dict=acl_data)

    To generate a curl command line from the above::

        token=...
        storage_url=...
        python -c '
          from swift.common.middleware.acl import format_acl
          acl_data = { 'admin': ['alice'], 'read-write': ['bob', 'carol'] }
          headers = {'X-Account-Access-Control':
                     format_acl(version=2, acl_dict=acl_data)}
          header_str = ' '.join(["-H '%s: %s'" % (k, v)
                                 for k, v in headers.items()])
          print ('curl -D- -X POST -H "x-auth-token: $token" %s '
                 '$storage_url' % header_str)
        '

    :param app: The next WSGI app in the pipeline
    :param conf: The dict of configuration values from the Paste config file
    """

    def __init__(self, app, conf):
        self.app = app
        self.conf = conf
        self.logger = get_logger(conf, log_route='tempauth')
        self.log_headers = config_true_value(conf.get('log_headers', 'f'))
        self.reseller_prefix = conf.get('reseller_prefix', 'AUTH').strip()
        if self.reseller_prefix and self.reseller_prefix[-1] != '_':
            self.reseller_prefix += '_'
        self.logger.set_statsd_prefix('tempauth.%s' % (
            self.reseller_prefix if self.reseller_prefix else 'NONE',))
        self.auth_prefix = conf.get('auth_prefix', '/auth/')
        if not self.auth_prefix or not self.auth_prefix.strip('/'):
            self.logger.warning('Rewriting invalid auth prefix "%s" to '
                                '"/auth/" (Non-empty auth prefix path '
                                'is required)' % self.auth_prefix)
            self.auth_prefix = '/auth/'
        if self.auth_prefix[0] != '/':
            self.auth_prefix = '/' + self.auth_prefix
        if self.auth_prefix[-1] != '/':
            self.auth_prefix += '/'
        self.token_life = int(conf.get('token_life', 86400))
        self.allow_overrides = config_true_value(
            conf.get('allow_overrides', 't'))
        self.storage_url_scheme = conf.get('storage_url_scheme', 'default')
        self.users = {}
        for conf_key in conf:
            if conf_key.startswith('user_') or conf_key.startswith('user64_'):
                account, username = conf_key.split('_', 1)[1].split('_')
                if conf_key.startswith('user64_'):
                    # Because trailing equal signs would screw up config file
                    # parsing, we auto-pad with '=' chars.
                    account += '=' * (len(account) % 4)
                    account = base64.b64decode(account)
                    username += '=' * (len(username) % 4)
                    username = base64.b64decode(username)
                values = conf[conf_key].split()
                if not values:
                    raise ValueError('%s has no key set' % conf_key)
                key = values.pop(0)
                if values and ('://' in values[-1] or '$HOST' in values[-1]):
                    url = values.pop()
                else:
                    url = '$HOST/v1/%s%s' % (self.reseller_prefix, account)
                self.users[account + ':' + username] = {
                    'key': key, 'url': url, 'groups': values}

    def __call__(self, env, start_response):
        """
        Accepts a standard WSGI application call, authenticating the request
        and installing callback hooks for authorization and ACL header
        validation. For an authenticated request, REMOTE_USER will be set to a
        comma separated list of the user's groups.

        With a non-empty reseller prefix, acts as the definitive auth service
        for just tokens and accounts that begin with that prefix, but will deny
        requests outside this prefix if no other auth middleware overrides it.

        With an empty reseller prefix, acts as the definitive auth service only
        for tokens that validate to a non-empty set of groups. For all other
        requests, acts as the fallback auth service when no other auth
        middleware overrides it.

        Alternatively, if the request matches the self.auth_prefix, the request
        will be routed through the internal auth request handler (self.handle).
        This is to handle granting tokens, etc.
        """
        if self.allow_overrides and env.get('swift.authorize_override', False):
            return self.app(env, start_response)
        if env.get('PATH_INFO', '').startswith(self.auth_prefix):
            return self.handle(env, start_response)
        s3 = env.get('HTTP_AUTHORIZATION')
        token = env.get('HTTP_X_AUTH_TOKEN', env.get('HTTP_X_STORAGE_TOKEN'))
        if s3 or (token and token.startswith(self.reseller_prefix)):
            # Note: Empty reseller_prefix will match all tokens.
            groups = self.get_groups(env, token)
            if groups:
                user = groups and groups.split(',', 1)[0] or ''
                trans_id = env.get('swift.trans_id')
                self.logger.debug('User: %s uses token %s (trans_id %s)' %
                                  (user, 's3' if s3 else token, trans_id))
                env['REMOTE_USER'] = groups
                env['swift.authorize'] = self.authorize
                env['swift.clean_acl'] = clean_acl
                if '.reseller_admin' in groups:
                    env['reseller_request'] = True
            else:
                # Unauthorized token
                if self.reseller_prefix and not s3:
                    # Because I know I'm the definitive auth for this token, I
                    # can deny it outright.
                    self.logger.increment('unauthorized')
                    try:
                        vrs, realm, rest = split_path(env['PATH_INFO'],
                                                      2, 3, True)
                    except ValueError:
                        realm = 'unknown'
                    return HTTPUnauthorized(headers={
                        'Www-Authenticate': 'Swift realm="%s"' % realm})(
                            env, start_response)
                # Because I'm not certain if I'm the definitive auth for empty
                # reseller_prefixed tokens, I won't overwrite swift.authorize.
                elif 'swift.authorize' not in env:
                    env['swift.authorize'] = self.denied_response
        else:
            if self.reseller_prefix:
                # With a non-empty reseller_prefix, I would like to be called
                # back for anonymous access to accounts I know I'm the
                # definitive auth for.
                try:
                    version, rest = split_path(env.get('PATH_INFO', ''),
                                               1, 2, True)
                except ValueError:
                    version, rest = None, None
                    self.logger.increment('errors')
                if rest and rest.startswith(self.reseller_prefix):
                    # Handle anonymous access to accounts I'm the definitive
                    # auth for.
                    env['swift.authorize'] = self.authorize
                    env['swift.clean_acl'] = clean_acl
                # Not my token, not my account, I can't authorize this request,
                # deny all is a good idea if not already set...
                elif 'swift.authorize' not in env:
                    env['swift.authorize'] = self.denied_response
            # Because I'm not certain if I'm the definitive auth for empty
            # reseller_prefixed accounts, I won't overwrite swift.authorize.
            elif 'swift.authorize' not in env:
                env['swift.authorize'] = self.authorize
                env['swift.clean_acl'] = clean_acl
        return self.app(env, start_response)

    def _get_user_groups(self, account, account_user, account_id):
        """
        :param account: example: test
        :param account_user: example: test:tester
        """
        groups = [account, account_user]
        groups.extend(self.users[account_user]['groups'])
        if '.admin' in groups:
            groups.remove('.admin')
            groups.append(account_id)
        groups = ','.join(groups)
        return groups

    def get_groups(self, env, token):
        """
        Get groups for the given token.

        :param env: The current WSGI environment dictionary.
        :param token: Token to validate and return a group string for.

        :returns: None if the token is invalid or a string containing a comma
                  separated list of groups the authenticated user is a member
                  of. The first group in the list is also considered a unique
                  identifier for that user.
        """
        groups = None
        memcache_client = cache_from_env(env)
        if not memcache_client:
            raise Exception('Memcache required')
        memcache_token_key = '%s/token/%s' % (self.reseller_prefix, token)
        cached_auth_data = memcache_client.get(memcache_token_key)
        if cached_auth_data:
            expires, groups = cached_auth_data
            if expires < time():
                groups = None

        if env.get('HTTP_AUTHORIZATION'):
            account_user, sign = \
                env['HTTP_AUTHORIZATION'].split(' ')[1].rsplit(':', 1)
            if account_user not in self.users:
                return None
            account, user = account_user.split(':', 1)
            account_id = self.users[account_user]['url'].rsplit('/', 1)[-1]
            path = env['PATH_INFO']
            env['PATH_INFO'] = path.replace(account_user, account_id, 1)
            msg = base64.urlsafe_b64decode(unquote(token))
            key = self.users[account_user]['key']
            s = base64.encodestring(hmac.new(key, msg, sha1).digest()).strip()
            if s != sign:
                return None
            groups = self._get_user_groups(account, account_user, account_id)

        return groups

    def account_acls(self, req):
        """
        Return a dict of ACL data from the account server via get_account_info.

        Auth systems may define their own format, serialization, structure,
        and capabilities implemented in the ACL headers and persisted in the
        sysmeta data.  However, auth systems are strongly encouraged to be
        interoperable with Tempauth.

        Account ACLs are set and retrieved via the header
           X-Account-Access-Control

        For header format and syntax, see:
         * :func:`swift.common.middleware.acl.parse_acl()`
         * :func:`swift.common.middleware.acl.format_acl()`
        """
        info = get_account_info(req.environ, self.app, swift_source='TA')
        try:
            acls = acls_from_account_info(info)
        except ValueError as e1:
            self.logger.warn("Invalid ACL stored in metadata: %r" % e1)
            return None
        except NotImplementedError as e2:
            self.logger.warn("ACL version exceeds middleware version: %r" % e2)
            return None
        return acls

    def extract_acl_and_report_errors(self, req):
        """
        Return a user-readable string indicating the errors in the input ACL,
        or None if there are no errors.
        """
        acl_header = 'x-account-access-control'
        acl_data = req.headers.get(acl_header)
        result = parse_acl(version=2, data=acl_data)
        if result is None:
            return 'Syntax error in input (%r)' % acl_data

        tempauth_acl_keys = 'admin read-write read-only'.split()
        for key in result:
            # While it is possible to construct auth systems that collaborate
            # on ACLs, TempAuth is not such an auth system.  At this point,
            # it thinks it is authoritative.
            if key not in tempauth_acl_keys:
                return 'Key %r not recognized' % key

        for key in tempauth_acl_keys:
            if key not in result:
                continue
            if not isinstance(result[key], list):
                return 'Value for key %r must be a list' % key
            for grantee in result[key]:
                if not isinstance(grantee, str):
                    return 'Elements of %r list must be strings' % key

        # Everything looks fine, no errors found
        internal_hdr = get_sys_meta_prefix('account') + 'core-access-control'
        req.headers[internal_hdr] = req.headers.pop(acl_header)
        return None

    def authorize(self, req):
        """
        Returns None if the request is authorized to continue or a standard
        WSGI response callable if not.
        """

        try:
            _junk, account, container, obj = req.split_path(1, 4, True)
        except ValueError:
            self.logger.increment('errors')
            return HTTPNotFound(request=req)

        if not account or not account.startswith(self.reseller_prefix):
            self.logger.debug("Account name: %s doesn't start with "
                              "reseller_prefix: %s."
                              % (account, self.reseller_prefix))
            return self.denied_response(req)

        # At this point, TempAuth is convinced that it is authoritative.
        # If you are sending an ACL header, it must be syntactically valid
        # according to TempAuth's rules for ACL syntax.
        acl_data = req.headers.get('x-account-access-control')
        if acl_data is not None:
            error = self.extract_acl_and_report_errors(req)
            if error:
                msg = 'X-Account-Access-Control invalid: %s\n\nInput: %s\n' % (
                    error, acl_data)
                headers = [('Content-Type', 'text/plain; charset=UTF-8')]
                return HTTPBadRequest(request=req, headers=headers, body=msg)

        user_groups = (req.remote_user or '').split(',')
        account_user = user_groups[1] if len(user_groups) > 1 else None

        if '.reseller_admin' in user_groups and \
                account != self.reseller_prefix and \
                account[len(self.reseller_prefix)] != '.':
            req.environ['swift_owner'] = True
            self.logger.debug("User %s has reseller admin authorizing."
                              % account_user)
            return None

        if account in user_groups and \
                (req.method not in ('DELETE', 'PUT') or container):
            # If the user is admin for the account and is not trying to do an
            # account DELETE or PUT...
            req.environ['swift_owner'] = True
            self.logger.debug("User %s has admin authorizing."
                              % account_user)
            return None

        if (req.environ.get('swift_sync_key')
                and (req.environ['swift_sync_key'] ==
                     req.headers.get('x-container-sync-key', None))
                and 'x-timestamp' in req.headers):
            self.logger.debug("Allow request with container sync-key: %s."
                              % req.environ['swift_sync_key'])
            return None

        if req.method == 'OPTIONS':
            #allow OPTIONS requests to proceed as normal
            self.logger.debug("Allow OPTIONS request.")
            return None

        referrers, groups = parse_acl(getattr(req, 'acl', None))

        if referrer_allowed(req.referer, referrers):
            if obj or '.rlistings' in groups:
                self.logger.debug("Allow authorizing %s via referer ACL."
                                  % req.referer)
                return None

        for user_group in user_groups:
            if user_group in groups:
                self.logger.debug("User %s allowed in ACL: %s authorizing."
                                  % (account_user, user_group))
                return None

        # Check for access via X-Account-Access-Control
        acct_acls = self.account_acls(req)
        if acct_acls:
            # At least one account ACL is set in this account's sysmeta data,
            # so we should see whether this user is authorized by the ACLs.
            user_group_set = set(user_groups)
            if user_group_set.intersection(acct_acls['admin']):
                req.environ['swift_owner'] = True
                self.logger.debug('User %s allowed by X-Account-Access-Control'
                                  ' (admin)' % account_user)
                return None
            if (user_group_set.intersection(acct_acls['read-write']) and
                    (container or req.method in ('GET', 'HEAD'))):
                # The RW ACL allows all operations to containers/objects, but
                # only GET/HEAD to accounts (and OPTIONS, above)
                self.logger.debug('User %s allowed by X-Account-Access-Control'
                                  ' (read-write)' % account_user)
                return None
            if (user_group_set.intersection(acct_acls['read-only']) and
                    req.method in ('GET', 'HEAD')):
                self.logger.debug('User %s allowed by X-Account-Access-Control'
                                  ' (read-only)' % account_user)
                return None

        return self.denied_response(req)

    def denied_response(self, req):
        """
        Returns a standard WSGI response callable with the status of 403 or 401
        depending on whether the REMOTE_USER is set or not.
        """
        if req.remote_user:
            self.logger.increment('forbidden')
            return HTTPForbidden(request=req)
        else:
            self.logger.increment('unauthorized')
            return HTTPUnauthorized(request=req)

    def handle(self, env, start_response):
        """
        WSGI entry point for auth requests (ones that match the
        self.auth_prefix).
        Wraps env in swob.Request object and passes it down.

        :param env: WSGI environment dictionary
        :param start_response: WSGI callable
        """
        try:
            req = Request(env)
            if self.auth_prefix:
                req.path_info_pop()
            req.bytes_transferred = '-'
            req.client_disconnect = False
            if 'x-storage-token' in req.headers and \
                    'x-auth-token' not in req.headers:
                req.headers['x-auth-token'] = req.headers['x-storage-token']
            return self.handle_request(req)(env, start_response)
        except (Exception, Timeout):
            print "EXCEPTION IN handle: %s: %s" % (format_exc(), env)
            self.logger.increment('errors')
            start_response('500 Server Error',
                           [('Content-Type', 'text/plain')])
            return ['Internal server error.\n']

    def handle_request(self, req):
        """
        Entry point for auth requests (ones that match the self.auth_prefix).
        Should return a WSGI-style callable (such as swob.Response).

        :param req: swob.Request object
        """
        req.start_time = time()
        handler = None
        try:
            version, account, user, _junk = req.split_path(1, 4, True)
        except ValueError:
            self.logger.increment('errors')
            return HTTPNotFound(request=req)
        if version in ('v1', 'v1.0', 'auth'):
            if req.method == 'GET':
                handler = self.handle_get_token
        if not handler:
            self.logger.increment('errors')
            req.response = HTTPBadRequest(request=req)
        else:
            req.response = handler(req)
        return req.response

    def handle_get_token(self, req):
        """
        Handles the various `request for token and service end point(s)` calls.
        There are various formats to support the various auth servers in the
        past. Examples::

            GET <auth-prefix>/v1/<act>/auth
                X-Auth-User: <act>:<usr>  or  X-Storage-User: <usr>
                X-Auth-Key: <key>         or  X-Storage-Pass: <key>
            GET <auth-prefix>/auth
                X-Auth-User: <act>:<usr>  or  X-Storage-User: <act>:<usr>
                X-Auth-Key: <key>         or  X-Storage-Pass: <key>
            GET <auth-prefix>/v1.0
                X-Auth-User: <act>:<usr>  or  X-Storage-User: <act>:<usr>
                X-Auth-Key: <key>         or  X-Storage-Pass: <key>

        On successful authentication, the response will have X-Auth-Token and
        X-Storage-Token set to the token to use with Swift and X-Storage-URL
        set to the URL to the default Swift cluster to use.

        :param req: The swob.Request to process.
        :returns: swob.Response, 2xx on success with data set as explained
                  above.
        """
        # Validate the request info
        try:
            pathsegs = split_path(req.path_info, 1, 3, True)
        except ValueError:
            self.logger.increment('errors')
            return HTTPNotFound(request=req)
        if pathsegs[0] == 'v1' and pathsegs[2] == 'auth':
            account = pathsegs[1]
            user = req.headers.get('x-storage-user')
            if not user:
                user = req.headers.get('x-auth-user')
                if not user or ':' not in user:
                    self.logger.increment('token_denied')
                    return HTTPUnauthorized(request=req, headers=
                                            {'Www-Authenticate':
                                             'Swift realm="%s"' % account})
                account2, user = user.split(':', 1)
                if account != account2:
                    self.logger.increment('token_denied')
                    return HTTPUnauthorized(request=req, headers=
                                            {'Www-Authenticate':
                                             'Swift realm="%s"' % account})
            key = req.headers.get('x-storage-pass')
            if not key:
                key = req.headers.get('x-auth-key')
        elif pathsegs[0] in ('auth', 'v1.0'):
            user = req.headers.get('x-auth-user')
            if not user:
                user = req.headers.get('x-storage-user')
            if not user or ':' not in user:
                self.logger.increment('token_denied')
                return HTTPUnauthorized(request=req, headers=
                                        {'Www-Authenticate':
                                         'Swift realm="unknown"'})
            account, user = user.split(':', 1)
            key = req.headers.get('x-auth-key')
            if not key:
                key = req.headers.get('x-storage-pass')
        else:
            return HTTPBadRequest(request=req)
        if not all((account, user, key)):
            self.logger.increment('token_denied')
            realm = account or 'unknown'
            return HTTPUnauthorized(request=req, headers={'Www-Authenticate':
                                                          'Swift realm="%s"' %
                                                          realm})
        # Authenticate user
        account_user = account + ':' + user
        if account_user not in self.users:
            self.logger.increment('token_denied')
            return HTTPUnauthorized(request=req, headers=
                                    {'Www-Authenticate':
                                     'Swift realm="%s"' % account})
        if self.users[account_user]['key'] != key:
            self.logger.increment('token_denied')
            return HTTPUnauthorized(request=req, headers=
                                    {'Www-Authenticate':
                                     'Swift realm="unknown"'})
        account_id = self.users[account_user]['url'].rsplit('/', 1)[-1]
        # Get memcache client
        memcache_client = cache_from_env(req.environ)
        if not memcache_client:
            raise Exception('Memcache required')
        # See if a token already exists and hasn't expired
        token = None
        memcache_user_key = '%s/user/%s' % (self.reseller_prefix, account_user)
        candidate_token = memcache_client.get(memcache_user_key)
        if candidate_token:
            memcache_token_key = \
                '%s/token/%s' % (self.reseller_prefix, candidate_token)
            cached_auth_data = memcache_client.get(memcache_token_key)
            if cached_auth_data:
                expires, old_groups = cached_auth_data
                old_groups = old_groups.split(',')
                new_groups = self._get_user_groups(account, account_user,
                                                   account_id)

                if expires > time() and \
                        set(old_groups) == set(new_groups.split(',')):
                    token = candidate_token
        # Create a new token if one didn't exist
        if not token:
            # Generate new token
            token = '%stk%s' % (self.reseller_prefix, uuid4().hex)
            expires = time() + self.token_life
            groups = self._get_user_groups(account, account_user, account_id)
            # Save token
            memcache_token_key = '%s/token/%s' % (self.reseller_prefix, token)
            memcache_client.set(memcache_token_key, (expires, groups),
                                time=float(expires - time()))
            # Record the token with the user info for future use.
            memcache_user_key = \
                '%s/user/%s' % (self.reseller_prefix, account_user)
            memcache_client.set(memcache_user_key, token,
                                time=float(expires - time()))
        resp = Response(request=req, headers={
            'x-auth-token': token, 'x-storage-token': token})
        url = self.users[account_user]['url'].replace('$HOST', resp.host_url)
        if self.storage_url_scheme != 'default':
            url = self.storage_url_scheme + ':' + url.split(':', 1)[1]
        resp.headers['x-storage-url'] = url
        return resp


def filter_factory(global_conf, **local_conf):
    """Returns a WSGI filter app for use with paste.deploy."""
    conf = global_conf.copy()
    conf.update(local_conf)
    register_swift_info('tempauth', account_acls=True)

    def auth_filter(app):
        return TempAuth(app, conf)
    return auth_filter
