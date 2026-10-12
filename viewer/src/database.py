"""Host-local SQLite accounts, one-use grants, owner reservations and journal."""

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path

from viewer.errors import ViewerError
from viewer.util import digest, json_bytes, parse_json, utc_now

LOGIN = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')


def password_hash(password, salt=None):
    if not isinstance(password, str) or not 1 <= len(password) <= 1024:
        raise ViewerError('invalid_password', 'Password must contain 1–1024 characters.')
    salt = salt or secrets.token_bytes(16)
    result = hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=1, maxmem=64<<20)
    return salt.hex() + ':' + result.hex()


class Database:
    def __init__(self, runtime: Path, *, clock=time.time, ttl=60):
        runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(runtime, 0o700)
        self.path, self.clock, self.ttl = runtime / 'viewer.sqlite', clock, ttl
        if self.path.is_symlink():
            raise ViewerError('unsafe_runtime', 'Database must be a regular local file.')
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600); os.close(fd)
        os.chmod(self.path, 0o600)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        version=self.connection.execute('PRAGMA user_version').fetchone()[0]
        if version>2:
            self.connection.close()
            raise ViewerError('database_version','Database schema is newer than this viewer.',503)
        if version==1:
            self.backup(runtime/('viewer-v1-backup-'+secrets.token_hex(8)+'.sqlite'))
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA synchronous=FULL')
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            for statement in (
                'CREATE TABLE IF NOT EXISTS users(login TEXT PRIMARY KEY,password TEXT NOT NULL,enabled INTEGER NOT NULL)',
                'CREATE TABLE IF NOT EXISTS grants(token_hash TEXT PRIMARY KEY,login TEXT NOT NULL,operation TEXT NOT NULL,run_id TEXT,expires REAL NOT NULL,consumed INTEGER NOT NULL DEFAULT 0)',
                'CREATE TABLE IF NOT EXISTS owners(run_id TEXT PRIMARY KEY,login TEXT NOT NULL,basket_hash TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS receipts(login TEXT NOT NULL,idem_key TEXT NOT NULL,fingerprint TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(login,idem_key))',
                'CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,data TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS operation_progress(id TEXT NOT NULL,step TEXT NOT NULL,position INTEGER NOT NULL,PRIMARY KEY(id,step))',
                'CREATE TABLE IF NOT EXISTS auth_sessions(token_hash TEXT PRIMARY KEY,login TEXT NOT NULL,expires REAL NOT NULL)',
                'CREATE TABLE IF NOT EXISTS auth_challenges(token_hash TEXT PRIMARY KEY,expires REAL NOT NULL)',
            ):self.connection.execute(statement)
            columns={r['name'] for r in self.connection.execute('PRAGMA table_info(users)')}
            if 'role' not in columns:
                self.connection.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('user','admin'))")
            self.connection.execute('PRAGMA user_version=2')

    def create_user(self, login, password, *, role='user'):
        if not isinstance(login, str) or not LOGIN.fullmatch(login):
            raise ViewerError('invalid_login', 'Invalid login.')
        if role not in ('user','admin'):raise ViewerError('invalid_role','Invalid role.')
        hashed = password_hash(password)
        try:
            with self.connection:self.connection.execute('INSERT INTO users(login,password,enabled,role) VALUES (?,?,1,?)',(login,hashed,role))
        except sqlite3.IntegrityError:
            raise ViewerError('user_exists', 'User already exists.', 409) from None
        return {'login': login, 'enabled': True}

    def list_users(self,details=False):
        return [{'login':r['login'],'enabled':bool(r['enabled']),**({'role':r['role']} if details else {})}
                for r in self.connection.execute('SELECT login,enabled,role FROM users ORDER BY login')]

    def actor(self,login):
        row=self.connection.execute('SELECT login,role,enabled FROM users WHERE login=?',(login,)).fetchone()
        return {'login':row['login'],'role':row['role']} if row and row['enabled'] else None

    def authenticate(self,login,password):
        if not isinstance(login,str) or not LOGIN.fullmatch(login) or not isinstance(password,str) or not 1<=len(password)<=1024:
            raise ViewerError('auth_denied','Invalid credentials.',401)
        row=self.connection.execute('SELECT * FROM users WHERE login=?',(login,)).fetchone()
        salt=bytes.fromhex(row['password'].split(':')[0]) if row else b'\x00'*16
        hashed=password_hash(password,salt)
        if row is None or not row['enabled'] or not hmac.compare_digest(hashed,row['password']):
            raise ViewerError('auth_denied','Invalid credentials.',401)
        return {'login':row['login'],'role':row['role']}

    def _revoke(self,login):
        self.connection.execute('DELETE FROM grants WHERE login=?',(login,))
        self.connection.execute('DELETE FROM auth_sessions WHERE login=?',(login,))

    def _guard_admin(self,login,loses_access):
        row=self.connection.execute('SELECT role,enabled FROM users WHERE login=?',(login,)).fetchone()
        if row is None:raise ViewerError('not_found','User not found.',404)
        count=self.connection.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND enabled=1").fetchone()[0]
        if row['role']=='admin' and row['enabled'] and loses_access and count<=1:
            raise ViewerError('last_admin','The last active administrator must remain enabled.',409)

    def set_role(self,login,role):
        if role not in ('user','admin'):raise ViewerError('invalid_role','Invalid role.')
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            self._guard_admin(login,role!='admin')
            self.connection.execute('UPDATE users SET role=? WHERE login=?',(role,login))
            self._revoke(login)
        return {'login':login,'role':role}

    def _guard_user_management(self,login):
        row=self.connection.execute('SELECT role FROM users WHERE login=?',(login,)).fetchone()
        if row is None:raise ViewerError('not_found','User not found.',404)
        if row['role']=='admin':raise ViewerError('protected_admin','Administrator accounts cannot be changed through user management.',403)

    def update_user(self, login, *, enabled, users_only=False):
        if type(enabled) is not bool: raise ViewerError('invalid_user', 'enabled must be boolean.')
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            if users_only:self._guard_user_management(login)
            self._guard_admin(login,not enabled)
            changed = self.connection.execute('UPDATE users SET enabled=? WHERE login=?', (enabled, login)).rowcount
            if not changed: raise ViewerError('not_found', 'User not found.', 404)
            if not enabled:self._revoke(login)
        return {'login': login, 'enabled': enabled}

    def set_password(self, login, password, *, users_only=False):
        hashed = password_hash(password)
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            if users_only:self._guard_user_management(login)
            if not self.connection.execute('UPDATE users SET password=? WHERE login=?', (hashed,login)).rowcount:
                raise ViewerError('not_found', 'User not found.', 404)
            self._revoke(login)
        return {'login': login}

    def issue(self, login, password, operation, run_id=None,*,verify=None):
        token, expires = secrets.token_urlsafe(32), self.clock() + self.ttl
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            (verify or self.authenticate)(login,password)
            self.connection.execute('DELETE FROM grants WHERE expires<? OR consumed=1', (self.clock(),))
            self.connection.execute('INSERT INTO grants VALUES (?,?,?,?,?,0)', (digest(token.encode()),login,operation,run_id,expires))
        return {'token': token, 'expires_at': utc_now(expires)}

    def authorize(self, token, operation, run_id=None):
        if not isinstance(token,str) or len(token)>1024:
            raise ViewerError('invalid_grant', 'Missing or expired grant.', 401)
        row = self.connection.execute('SELECT g.*,u.enabled FROM grants g JOIN users u ON u.login=g.login WHERE token_hash=?', (digest(token.encode()),)).fetchone()
        if row is None or row['consumed'] or not row['enabled'] or self.clock() >= row['expires'] or row['operation'] != operation or row['run_id'] != run_id:
            raise ViewerError('invalid_grant', 'Missing or expired grant.', 401)
        return row['login']

    def consume(self, token):
        with self.connection: self.connection.execute('UPDATE grants SET consumed=1 WHERE token_hash=?', (digest(token.encode()),))

    def owner(self, run_id):
        row = self.connection.execute('SELECT login,basket_hash FROM owners WHERE run_id=?', (run_id,)).fetchone()
        return dict(row) if row else None

    def reserve_owner(self, run_id, login, basket_hash):
        previous = self.owner(run_id)
        if previous and previous['login'] != login:
            raise ViewerError('foreign_run', 'Run belongs to another user.', 403)
        if previous and previous['basket_hash'] != basket_hash:
            raise ViewerError('basket_conflict', 'Run ID is reserved for another basket.', 409)
        with self.connection: self.connection.execute('INSERT OR IGNORE INTO owners VALUES (?,?,?)', (run_id,login,basket_hash))

    def receipt(self, login, key):
        row = self.connection.execute('SELECT fingerprint,data FROM receipts WHERE login=? AND idem_key=?',(login,key)).fetchone()
        return {'fingerprint':row['fingerprint'],'receipt':parse_json(row['data'].encode())} if row else None

    def operations(self):
        return {r['id']:parse_json(r['data'].encode()) for r in self.connection.execute('SELECT * FROM operations ORDER BY id')}

    def begin_publication(self, op_id, value, token=None):
        previous = self.owner(value['marker']['run_id'])
        if previous and previous['login'] != value['owner']:
            raise ViewerError('foreign_run', 'Run belongs to another user.', 403)
        if previous and previous['basket_hash'] != value['marker']['basket_sha256']:
            raise ViewerError('basket_conflict', 'Run ID is reserved for another basket.', 409)
        with self.connection:
            self.connection.execute('INSERT OR IGNORE INTO owners VALUES (?,?,?)',
                (value['marker']['run_id'], value['owner'], value['marker']['basket_sha256']))
            if token:
                self.connection.execute('UPDATE grants SET consumed=1 WHERE token_hash=?', (digest(token.encode()),))
            self.connection.execute('INSERT INTO operations VALUES (?,?)', (op_id,json_bytes(value).decode()))

    def save_operation(self, op_id, value):
        with self.connection: self.connection.execute('INSERT OR REPLACE INTO operations VALUES (?,?)', (op_id,json_bytes(value).decode()))

    def progress(self, op_id, step):
        row=self.connection.execute('SELECT position FROM operation_progress WHERE id=? AND step=?',(op_id,step)).fetchone()
        return row['position'] if row else 0

    def set_progress(self, op_id, step, position):
        with self.connection:self.connection.execute('INSERT OR REPLACE INTO operation_progress VALUES (?,?,?)',(op_id,step,position))

    def finish_operation(self, op_id, login=None, key=None, fingerprint=None, receipt=None):
        with self.connection:
            if receipt is not None:
                self.connection.execute('INSERT OR REPLACE INTO receipts VALUES (?,?,?,?)',(login,key,fingerprint,json_bytes(receipt).decode()))
            self.connection.execute('DELETE FROM operations WHERE id=?',(op_id,))
            self.connection.execute('DELETE FROM operation_progress WHERE id=?',(op_id,))

    def backup(self, target: Path):
        if target.exists(): raise ViewerError('backup_exists','Backup path already exists.',409)
        fd=os.open(target,os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600);os.close(fd)
        with sqlite3.connect(target) as output: self.connection.backup(output)

    def close(self):
        self.connection.close()
