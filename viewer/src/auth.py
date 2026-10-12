"""Opaque browser sessions and CSRF challenges backed by the existing SQLite database."""
import hmac
import secrets
from collections import OrderedDict
from urllib.parse import urlsplit
from viewer.errors import ViewerError
from viewer.util import digest,utc_now

SESSION_COOKIE='prsim_session'
CSRF_COOKIE='prsim_csrf'

class BrowserAuth:
    def __init__(self,db,config):
        self.db,self.config=db,config
        self.failures=OrderedDict()

    def session(self,request):
        token=request.cookies.get(SESSION_COOKIE,'')
        if not token or len(token)>1024:return None
        row=self.db.connection.execute(
            'SELECT s.login,s.expires FROM auth_sessions s WHERE s.token_hash=? AND s.expires>?',
            (digest(token.encode()),self.db.clock())).fetchone()
        if row is None:return None
        actor=self.db.actor(row['login'])
        return {**actor,'expires':row['expires']} if actor else None

    def challenge(self,request):
        now=self.db.clock()
        token=request.cookies.get(CSRF_COOKIE,'')
        row=self.db.connection.execute('SELECT 1 FROM auth_challenges WHERE token_hash=? AND expires>?',
                                      (digest(token.encode()),now)).fetchone() if token and len(token)<=1024 else None
        if row is None:
            token=secrets.token_urlsafe(32)
            with self.db.connection:
                self.db.connection.execute('DELETE FROM auth_challenges WHERE expires<=?',(now,))
                self.db.connection.execute('INSERT INTO auth_challenges VALUES (?,?)',(digest(token.encode()),now+600))
                self.db.connection.execute('DELETE FROM auth_challenges WHERE token_hash IN (SELECT token_hash FROM auth_challenges ORDER BY expires DESC LIMIT -1 OFFSET 4096)')
        return token

    def info(self,request):
        actor=self.session(request)
        return {'authenticated':bool(actor),'actor':({'login':actor['login'],'role':actor['role']} if actor else None),
                'expires_at':utc_now(actor['expires']) if actor else None,'csrf_token':self.challenge(request)}

    def secure(self,request):
        return (self.config.public_origin or str(request.url)).startswith('https://')

    def csrf_cookie(self,response,token,request):
        response.set_cookie(CSRF_COOKIE,token,max_age=600,httponly=True,samesite='strict',secure=self.secure(request),path='/')

    def check_csrf(self,request):
        expected=self.config.public_origin
        if expected is None:
            if request.url.hostname not in ('localhost','127.0.0.1','::1'):
                raise ViewerError('csrf_denied','Browser authentication requires the configured public origin.',403)
            expected=request.url.scheme+'://'+request.url.netloc
        origin=request.headers.get('origin','')
        cookie=request.cookies.get(CSRF_COOKIE,'')
        submitted=request.headers.get('x-csrf-token','')
        if origin!=expected or not cookie or len(cookie)>1024 or not hmac.compare_digest(cookie,submitted):
            raise ViewerError('csrf_denied','Request origin or CSRF challenge is invalid.',403)
        row=self.db.connection.execute('SELECT 1 FROM auth_challenges WHERE token_hash=? AND expires>?',
                                      (digest(cookie.encode()),self.db.clock())).fetchone()
        if row is None:raise ViewerError('csrf_denied','CSRF challenge expired; reload and try again.',403)

    def authenticate(self,request,login,password):
        now=self.db.clock()
        peer=request.client.host if request.client else 'unknown'
        # Invalid request payloads can fill the body limit; cache only bounded identifiers.
        keys=[('peer',peer),('login',login if isinstance(login,str) and len(login)<=64 else '')]
        limited=False
        for key in keys:
            recent=[t for t in self.failures.get(key,[]) if t>now-self.config.login_window_seconds]
            self.failures[key]=recent
            self.failures.move_to_end(key)
            limited=limited or len(recent)>=self.config.login_attempts
        while len(self.failures)>2048:self.failures.popitem(last=False)
        if limited:raise ViewerError('login_rate_limit','Too many login attempts; try again later.',429)
        try:actor=self.db.authenticate(login,password)
        except ViewerError:
            for key in keys:self.failures[key].append(now)
            raise
        for key in keys:self.failures.pop(key,None)
        return actor

    def login(self,request,login,password):
        self.check_csrf(request)
        now=self.db.clock()
        token=secrets.token_urlsafe(32);expires=now+self.config.session_ttl_seconds
        with self.db.connection:
            self.db.connection.execute('BEGIN IMMEDIATE')
            actor=self.authenticate(request,login,password)
            self.db.connection.execute('DELETE FROM auth_sessions WHERE expires<=?',(now,))
            self.db.connection.execute('INSERT INTO auth_sessions VALUES (?,?,?)',(digest(token.encode()),actor['login'],expires))
        return token,{'authenticated':True,'actor':actor,'expires_at':utc_now(expires),'csrf_token':self.challenge(request)}

    def logout(self,request):
        self.check_csrf(request)
        with self.db.connection:
            self.db.connection.execute('DELETE FROM auth_sessions WHERE token_hash=?',(digest(request.cookies.get(SESSION_COOKIE,'').encode()),))
        return {'authenticated':False,'actor':None,'expires_at':None,'csrf_token':self.challenge(request)}
