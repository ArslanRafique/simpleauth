# -*- coding: utf-8 -*-
import os
import sys
import logging

from urllib import urlencode
import urlparse

# for CSRF state tokens
import time
import base64

# Get available json parser
try:
  # should be the fastest on App Engine py27.
  import json
except ImportError:
  try: 
    import simplejson as json
  except ImportError:
    from django.utils import simplejson as json
    # at this point ImportError will be raised 
    # if none of the above could be imported
    
# already in the App Engine libs, see app.yaml on how to specify libraries
# need this for providers like LinkedIn
from lxml import etree

# it's a OAuth 1.0 spec even though the lib is called oauth2
import oauth2 as oauth1

# users module is needed for OpenID authentication.
from google.appengine.api import urlfetch, users
from webapp2_extras import security

__all__ = ['SimpleAuthHandler']


class SimpleAuthHandler(object):
  """A mixin to be used with a real request handler, 
  e.g. webapp2.RequestHandler. See README for getting started and 
  a usage example, or look through the code. It really is simple.

  Docs on authentication flow of various providers:

  Google auth
  http://code.google.com/apis/accounts/docs/OAuth2WebServer.html
  
  Facbook auth
  http://developers.facebook.com/docs/authentication/
  
  LinkedIn auth.
  https://developer.linkedin.com/documents/linkedins-oauth-details
  
  Windows Live.
  http://msdn.microsoft.com/en-us/library/hh243648.aspx#user
  
  Twtitter.
  https://dev.twitter.com/docs/auth/oauth
  """
  
  PROVIDERS = {
    'google'      : ('oauth2', 
      'https://accounts.google.com/o/oauth2/auth?{0}', 
      'https://accounts.google.com/o/oauth2/token'),
    'windows_live': ('oauth2',
      'https://oauth.live.com/authorize?{0}',
      'https://oauth.live.com/token'),
    'facebook'    : ('oauth2',
      'https://www.facebook.com/dialog/oauth?{0}',
      'https://graph.facebook.com/oauth/access_token'),
    'linkedin'    : ('oauth1', {
      'request': 'https://api.linkedin.com/uas/oauth/requestToken', 
      'auth'   : 'https://www.linkedin.com/uas/oauth/authenticate?{0}'
    },           'https://api.linkedin.com/uas/oauth/accessToken'),
    'twitter'     : ('oauth1', {
       'request': 'https://api.twitter.com/oauth/request_token', 
       'auth'   : 'https://api.twitter.com/oauth/authenticate?{0}'
    },            'https://api.twitter.com/oauth/access_token'),
    'openid'      : ('openid', None)
  }
  
  
  TOKEN_RESPONSE_PARSERS = {
    'google'      : '_json_parser',
    'windows_live': '_json_parser',
    'facebook'    : '_query_string_parser',
    'linkedin'    : '_query_string_parser',
    'twitter'     : '_query_string_parser'
  }

  # Set this to True in your handler if you want to use 
  # 'state' param during authorization phase to guard agains
  # cross-site-request-forgery
  # 
  # CSRF protection assumes there's self.session method on the handler 
  # instance. See BaseRequestHandler in example/handlers.py for sample usage.
  OAUTH2_CSRF_STATE = False
  OAUTH2_CSRF_SESSION_PARAM = 'oauth2_state'
  OAUTH2_CSRF_TOKEN_TIMEOUT = 3600 # 1 hour
  # This will form the actual state parameter, e.g. token:timestamp
  # You don't normally need to override it.
  OAUTH2_CSRF_DELIMITER = ':'
  
  def _simple_auth(self, provider=None):
    """Dispatcher of auth init requests, e.g.
    GET /auth/PROVIDER
    
    It'll call _<authtype>_init() method, where
    <authtype> is oauth2, oauth1 or openid (defined in PROVIDERS dict).
    
    If a particular provider is not defined in the PROVIDERS
    or _<authtype>_init() does not exist for a specific auth type, 
    it'll fall back to self._provider_not_supported() passing in the 
    original provider name.
    """
    cfg = self.PROVIDERS.get(provider, (None,))
    meth = '_%s_init' % cfg[0]
    if hasattr(self, meth):
      try:
        
        # initiate openid, oauth1 or oauth2 authentication
        # we don't respond directly in here: specific methods are in charge 
        # with redirecting user to an auth endpoint
        getattr(self, meth)(provider, cfg[1])
        
      except:
        error_msg = str(sys.exc_info()[1])
        logging.error(error_msg)
        self._auth_error(provider, msg=error_msg)
        
    else:
      logging.error('Provider %s is not supported', provider)
      self._provider_not_supported(provider)
      
  def _auth_callback(self, provider=None):
    """Dispatcher of callbacks from auth providers, e.g.
    /auth/PROVIDER/callback?params=...
    
    It'll call _<authtype>_callback() method, where
    <authtype> is oauth2, oauth1 or openid (defined in PROVIDERS dict).
    
    Falls back to self._provider_not_supported(provider).
    """
    cfg = self.PROVIDERS.get(provider, (None,))
    meth = '_%s_callback' % cfg[0]
    if hasattr(self, meth):
      try:
        
        user_data, auth_info = getattr(self, meth)(provider, *cfg[-1:])
        # we're done here. the rest should be implemented by the actual app
        self._on_signin(user_data, auth_info, provider)
        
      except:
        error_msg = str(sys.exc_info()[1])
        logging.error(error_msg)
        self._auth_error(provider, msg=error_msg)
    else:
      logging.error('Provider %s is not supported', provider)
      self._provider_not_supported(provider)
    
  def _provider_not_supported(self, provider=None):
    """Callback triggered whenever user's trying to authenticate agains 
    a provider we don't support, or provider wasn't specified for some reason.
    
    Defaults to redirecting to / (root). 
    Override this method for a custom behaviour.
    """
    self.redirect('/')
    
  def _auth_error(self, provider, msg=None):
    """Being called on any error during auth process, with optional text 
    message provided. 

    Defaults to redirecting to /
    """
    self.redirect('/')

  def _oauth2_init(self, provider, auth_url):
    """Initiates OAuth 2.0 dance. 

    Falls back to self._provider_not_supported(provider) if either key 
    or secret is missing.
    """
    key, secret, scope = self._get_consumer_info_for(provider)
    callback_url = self._callback_uri_for(provider)
    
    _valid = key and secret and auth_url and callback_url
    if not _valid:
      logging.error('Provider %s is not supported', provider)
      self._provider_not_supported(provider)
      return

    params = {
      'response_type': 'code', 
      'client_id': key, 
      'redirect_uri': callback_url 
    }

    if scope:
      params.update(scope=scope)

    if self.OAUTH2_CSRF_STATE:
      state = self._generate_csrf_token()
      params.update(state=state)
      self.session[self.OAUTH2_CSRF_SESSION_PARAM] = state

    target_url = auth_url.format(urlencode(params)) 
    logging.debug('Redirecting user to %s', target_url)

    self.redirect(target_url)      
    
  def _oauth2_callback(self, provider, access_token_url):
    """Step 2 of OAuth 2.0, whenever the user accepts or denies access."""
    code = self.request.get('code', None)
    error = self.request.get('error', None)
    callback_url = self._callback_uri_for(provider)
    client_id, client_secret, scope = self._get_consumer_info_for(provider)
    
    if error:
      raise Exception(error)

    if self.OAUTH2_CSRF_STATE:
      _expected = self.session.pop(self.OAUTH2_CSRF_SESSION_PARAM, '')
      _actual = self.request.get('state')
      if not self._validate_csrf_token(_expected, _actual):
        raise Exception('State parameter is not valid. '
          'Expected [%s], got [%s]' % (_expected, _actual))
      
    payload = {
      'code': code,
      'client_id': client_id,
      'client_secret': client_secret,
      'redirect_uri': callback_url,
      'grant_type': 'authorization_code'
    }
    
    resp = urlfetch.fetch(
      url=access_token_url, 
      payload=urlencode(payload), 
      method=urlfetch.POST,
      headers={'Content-Type': 'application/x-www-form-urlencoded'}
    )

    _parser = getattr(self, self.TOKEN_RESPONSE_PARSERS[provider])
    _fetcher = getattr(self, '_get_%s_user_info' % provider)

    auth_info = _parser(resp.content)
    user_data = _fetcher(auth_info, key=client_id, secret=client_secret)
    return (user_data, auth_info)
    
  def _oauth1_init(self, provider, auth_urls):
    """Initiates OAuth 1.0 dance"""
    key, secret = self._get_consumer_info_for(provider)
    callback_url = self._callback_uri_for(provider)
    token_request_url = auth_urls.get('request', None)
    auth_url = auth_urls.get('auth', None)
    _parser = getattr(self, self.TOKEN_RESPONSE_PARSERS[provider], None)

    _valid = key or secret or \
             token_request_url or auth_url or callback_url or _parser
    if not(_valid):
      raise Exception('Provider %s is not supported' % provider)
      
    # make a request_token request
    client = self._oauth1_client(consumer_key=key, consumer_secret=secret)
    resp, content = client.request(auth_urls['request'], "GET")
    
    if resp.status != 200:
      raise Exception("Could not fetch a valid response from %s" % provider)
    
    # parse token request response
    request_token = _parser(content)
    if not request_token.get('oauth_token', None):
      raise Exception("Couldn't get a valid token from %s\n%s" % 
        (provider, str(request_token)))
      
    target_url = auth_urls['auth'].format(urlencode({
      'oauth_token': request_token.get('oauth_token', None),
      'oauth_callback': callback_url
    }))
    
    logging.debug('Redirecting user to %s', target_url)
    
    # save request token for later, the callback
    self.session['req_token'] = request_token
    self.redirect(target_url)      
    
  def _oauth1_callback(self, provider, access_token_url):
    """Third step of OAuth 1.0 dance."""
    request_token = self.session.pop('req_token', None)
    verifier = self.request.get('oauth_verifier', None)
    consumer_key, consumer_secret = self._get_consumer_info_for(provider)
    
    if not request_token:
      raise Exception("Couldn't find request token")
      
    if not verifier:
      raise Exception("No OAuth verifier was provided")
      
    token = oauth1.Token(request_token['oauth_token'], 
                         request_token['oauth_token_secret'])
    token.set_verifier(verifier)
    client = self._oauth1_client(token, consumer_key, consumer_secret)
    resp, content = client.request(access_token_url, "POST")

    _parser = getattr(self, self.TOKEN_RESPONSE_PARSERS[provider])
    _fetcher = getattr(self, '_get_%s_user_info' % provider)

    auth_info = _parser(content)
    user_data = _fetcher(auth_info, key=consumer_key, secret=consumer_secret)
    return (user_data, auth_info)
    
  def _openid_init(self, provider='openid', identity=None):
    """Initiates OpenID dance using App Engine users module API."""
    identity_url = identity or self.request.get('identity_url', None)
    callback_url = self._callback_uri_for(provider)
    
    if identity_url and callback_url:
      target_url = users.create_login_url(
        dest_url=callback_url,
        federated_identity=identity_url
      )
      logging.debug('Redirecting user to %s', target_url)
      self.redirect(target_url)
      
    else:
      logging.error(
        'Either identity or callback were not specified (%s, %s)',
        identity_url, callback_url)
      self._provider_not_supported(provider)
      
  def _openid_callback(self, provider='openid', _identity=None):
    """Being called back by an OpenID provider 
    after the user has been authenticated.
    """
    user = users.get_current_user()
    
    if not user or not user.federated_identity():
      raise Exception('OpenID Authentication failed')
      
    uinfo = {
      'id'      : user.federated_identity(),
      'nickname': user.nickname(),
      'email'   : user.email()
    }
    
    return (uinfo, {'provider': user.federated_provider()})

    
  #
  # callbacks and consumer key/secrets
  #
  
  def _callback_uri_for(self, provider):
    """Returns a callback URL for a 2nd step of the auth process.
    
    Override this with something like:
    self.uri_for('auth_callback', provider=provider, _full=True)
    """
    return None
    
  def _get_consumer_info_for(self, provider):
    """Returns a (key, secret, desired_scopes) tuple.

    Defaults to None. You should redefine this method and return real values.

    For OAuth 2.0 it should be a 3 elements tuple:
    (client_ID, client_secret, scopes)

    OAuth 1.0 doesn't have scope so this should return just a
    (consumer_key, consumer_secret) tuple.

    OpenID needs neither scope nor key/secret, so this method is never called
    for OpenID authentication.

    See README for more info on scopes and where to get consumer/client
    key/secrets.
    """
    return (None, None, None)
    
  #
  # user profile/info
  #
    
  def _get_google_user_info(self, auth_info, key=None, secret=None):
    """Returns a dict of currenly logging in user.
    Google API endpoint:
    https://www.googleapis.com/oauth2/v1/userinfo
    """
    resp = self._oauth2_request(
      'https://www.googleapis.com/oauth2/v1/userinfo?{0}', 
      auth_info['access_token']
    )
    return json.loads(resp)
    
  def _get_windows_live_user_info(self, auth_info, key=None, secret=None):
    """Windows Live API user profile endpoint.
    https://apis.live.net/v5.0/me
    
    Profile picture:
    https://apis.live.net/v5.0/USER_ID/picture
    """
    resp = self._oauth2_request('https://apis.live.net/v5.0/me?{0}', 
                                auth_info['access_token'])
    uinfo = json.loads(resp)
    avurl = 'https://apis.live.net/v5.0/{0}/picture'.format(uinfo['id'])
    uinfo.update(avatar_url=avurl)
    return uinfo
    
  def _get_facebook_user_info(self, auth_info, key=None, secret=None):
    """Facebook Graph API endpoint.
    https://graph.facebook.com/me
    """
    resp = self._oauth2_request('https://graph.facebook.com/me?{0}', 
                                auth_info['access_token'])
    return json.loads(resp)
    
  def _get_linkedin_user_info(self, auth_info, key=None, secret=None):
    """Returns a dict of currently logging in linkedin user.

    LinkedIn user profile API endpoint:
    http://api.linkedin.com/v1/people/~
    or
    http://api.linkedin.com/v1/people/~:<fields>
    where <fields> is something like
    (id,first-name,last-name,picture-url,public-profile-url,headline)
    """
    token = oauth1.Token(key=auth_info['oauth_token'], 
                         secret=auth_info['oauth_token_secret'])
    client = self._oauth1_client(token, key, secret)

    fields = 'id,first-name,last-name,picture-url,public-profile-url,headline'
    url = 'http://api.linkedin.com/v1/people/~:(%s)' % fields
    resp, content = client.request(url)
    
    person = etree.fromstring(content)
    uinfo = {}
    for e in person:
      uinfo.setdefault(e.tag, e.text)
    
    return uinfo
    
  def _get_twitter_user_info(self, auth_info, key=None, secret=None):
    """Returns a dict of twitter user using
    https://api.twitter.com/1/account/verify_credentials.json
    """
    token = oauth1.Token(key=auth_info['oauth_token'],
                         secret=auth_info['oauth_token_secret'])
    client = self._oauth1_client(token, key, secret)
    
    resp, content = client.request(
      'https://api.twitter.com/1/account/verify_credentials.json'
    )
    uinfo = json.loads(content)
    uinfo.setdefault('link', 'http://twitter.com/%s' % uinfo['screen_name'])
    return uinfo
    
  #
  # aux methods
  #
  
  def _oauth1_client(self, token=None, consumer_key=None, 
                     consumer_secret=None):
    """Returns OAuth 1.0 client that is capable of signing requests."""
    args = [oauth1.Consumer(key=consumer_key, secret=consumer_secret)]
    if token:
      args.append(token)
    
    return oauth1.Client(*args)
  
  def _oauth2_request(self, url, token):
    """Makes an HTTP request with OAuth 2.0 access token using App Engine 
    URLfetch API.
    """
    target_url = url.format(urlencode({'access_token':token}))
    return urlfetch.fetch(target_url).content
    
  def _query_string_parser(self, body):
    """Parses response body of an access token request query and returns
    the result in JSON format.
    
    Facebook, LinkedIn and Twitter respond with a query string, not JSON.
    """
    return dict(urlparse.parse_qsl(body))
    
  def _json_parser(self, body):
    """Parses body string into JSON dict"""
    return json.loads(body)

  def _generate_csrf_token(self, _time=None):
    """Creates a new random token that can be safely used as a URL param.

    Token would normally be stored in a user session and passed as 'state' 
    parameter during OAuth 2.0 authorization step.
    """
    now = str(_time or long(time.time()))
    secret = security.generate_random_string(30, pool=security.ASCII_PRINTABLE)
    token = self.OAUTH2_CSRF_DELIMITER.join([secret, now])
    return base64.urlsafe_b64encode(token)

  def _validate_csrf_token(self, expected, actual):
    """Validates expected token against the actual.

    Args:
      expected: String, existing token. Normally stored in a user session.
      actual: String, token provided via 'state' param.
    """
    if expected != actual:
      return False

    try:
      decoded = base64.urlsafe_b64decode(expected.encode('ascii'))
      token_key, token_time = decoded.rsplit(self.OAUTH2_CSRF_DELIMITER, 1)
      token_time = long(token_time)
      if not token_key:
        return False
    except (TypeError, ValueError, UnicodeDecodeError):
      return False

    now = long(time.time())
    timeout = now - token_time > self.OAUTH2_CSRF_TOKEN_TIMEOUT

    if timeout:
      logging.error("CSRF token timeout (issued at %d)", token_time)

    return not timeout
