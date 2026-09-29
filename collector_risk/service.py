"""Локальные роли, учетные записи, реестр, поток, заявки и аудит демо."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time

import pandas as pd

UTC = timezone.utc
PERMISSIONS = {
    'forecasts:read': 'Просмотр прогнозов, схемы и потока событий',
    'objects:read': 'Просмотр дерева объектов и каналов',
    'decisions:write': 'Запись решения в карточке',
    'tickets:read': 'Просмотр статуса локальной заявки и внешней заглушки',
    'tickets:write': 'Создание и подтверждение черновика заявки',
    'notifications:read': 'Просмотр оповещений',
    'notifications:ack': 'Отметка оповещения как просмотренного',
    'reports:read': 'Создание и выгрузка отчетов',
    'equipment:write': 'Добавление оборудования в локальный реестр',
    'objects:write': 'Добавление объекта в локальный реестр',
    'registry:sync': 'Сверка реестра с CSV',
    'stream:write': 'Управление историческим воспроизведением',
    'audit:read': 'Прямой просмотр аудита через API',
    'roles:manage': 'Создание ролей и учетных записей',
}
DEFAULT_DISPATCHER = ['forecasts:read', 'objects:read', 'decisions:write', 'tickets:read',
                      'tickets:write', 'notifications:read', 'notifications:ack', 'reports:read', 'equipment:write']
ADMIN_ONLY = {'roles:manage', 'stream:write', 'objects:write', 'registry:sync', 'audit:read'}


def password_hash(password, salt=None):
    """Хеширует локальный пароль с солью, чтобы не хранить исходную строку."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 120_000).hex()
    return salt + '$' + digest


def password_matches(password, stored):
    """Проверяет пароль сравнением хешей с постоянным временем сравнения."""
    try:
        salt, digest = stored.split('$', 1)
        return secrets.compare_digest(password_hash(password, salt).split('$', 1)[1], digest)
    except (ValueError, TypeError):
        return False


def now():
    """Возвращает время UTC для записей аудита и событий демо."""
    return datetime.now(UTC).isoformat(timespec='milliseconds')


class Store:
    """Управляет локальным состоянием: реестром, доступом, заявками и аудитом."""

    def __init__(self, path: Path, objects_csv: Path, daily_csv: Path, records: list):
        """Создает таблицы, начальные учетные записи, реестр и источник повторного воспроизведения."""
        self.path = Path(path)
        self.lock = threading.RLock()
        self.records = records
        self.by_key = {(int(r['object_id']), r['category']): r for r in records}
        self.sessions = {}
        self._init_db()
        self._seed_access()
        self.sync_registry(objects_csv, actor='system:init')
        self.replay = Replay(self, daily_csv)

    def connect(self):
        """Открывает отдельное соединение SQLite для текущего запроса или потока."""
        con = sqlite3.connect(self.path, timeout=20)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA busy_timeout=20000')
        return con

    def _init_db(self):
        """Создает таблицы и SQL-триггеры, запрещающие изменение записей аудита."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript('''
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY, created_at TEXT,
                object_id INTEGER, category TEXT, decision TEXT, reason TEXT, operator TEXT);
              CREATE TABLE IF NOT EXISTS registry(object_id INTEGER PRIMARY KEY, parent_id INTEGER,
                level INTEGER, kind TEXT, name TEXT, active INTEGER DEFAULT 1, updated_at TEXT);
              CREATE TABLE IF NOT EXISTS registry_changes(id INTEGER PRIMARY KEY, at TEXT,
                object_id INTEGER, action TEXT, detail TEXT);
              CREATE TABLE IF NOT EXISTS equipment_local(id INTEGER PRIMARY KEY,
                object_id INTEGER, system_name TEXT, sensor_type TEXT, name TEXT,
                created_at TEXT);
              CREATE TABLE IF NOT EXISTS tickets(id INTEGER PRIMARY KEY, object_id INTEGER,
                equipment TEXT, reason TEXT, priority TEXT, due_at TEXT,
                status TEXT, confirmed_by TEXT, created_at TEXT);
              CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY, object_id INTEGER,
                category TEXT, source_day TEXT, priority TEXT, title TEXT,
                created_at TEXT, acknowledged_at TEXT, acknowledged_by TEXT,
                UNIQUE(object_id,category,source_day));
              CREATE TABLE IF NOT EXISTS stream_events(id INTEGER PRIMARY KEY,
                source_at TEXT, received_at TEXT, processed_at TEXT,
                object_id INTEGER, category TEXT, readings INTEGER, alarms INTEGER,
                UNIQUE(source_at,object_id,category));
              CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, at TEXT, user_id TEXT,
                action TEXT, object_id INTEGER, result TEXT, detail TEXT,
                previous_hash TEXT, entry_hash TEXT);
              CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
                BEGIN SELECT RAISE(ABORT,'audit is append-only'); END;
              CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
                BEGIN SELECT RAISE(ABORT,'audit is append-only'); END;
              CREATE TABLE IF NOT EXISTS roles(role_id TEXT PRIMARY KEY, title TEXT NOT NULL,
                permissions_json TEXT NOT NULL, created_at TEXT);
              CREATE TABLE IF NOT EXISTS demo_accounts(user_id TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL, role_id TEXT NOT NULL, created_at TEXT);
            ''')

    def _seed_access(self):
        """Создает роль диспетчера и системные учетные записи без перезаписи настроек администратора."""
        with self.connect() as con:
            con.execute('INSERT OR IGNORE INTO roles VALUES(?,?,?,?)',
                        ('dispatcher', 'Диспетчер', json.dumps(DEFAULT_DISPATCHER), now()))
            for user, role in (('dispatcher', 'dispatcher'), ('admin', 'admin')):
                con.execute('INSERT OR IGNORE INTO demo_accounts VALUES(?,?,?,?)',
                            (user, password_hash('demo'), role, now()))

    def audit(self, user: str, action: str, result: str, object_id=None, detail=''):
        """Добавляет запись в цепочку хешей, включая отказы и чтение данных."""
        with self.lock, self.connect() as con:
            row = con.execute('SELECT entry_hash FROM audit ORDER BY id DESC LIMIT 1').fetchone()
            prev = row['entry_hash'] if row else 'GENESIS'
            parts = [now(), user, action, object_id, result, str(detail)[:500], prev]
            digest = hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()
            con.execute('INSERT INTO audit(at, user_id, action, object_id, result, detail, previous_hash, '
                        'entry_hash) VALUES(?,?,?,?,?,?,?,?)',
                        (*parts, digest))

    def verify_audit(self):
        """Проверяет последовательность хешей локального журнала на повреждение."""
        with self.connect() as con:
            rows = con.execute('SELECT * FROM audit ORDER BY id').fetchall()
        prev = 'GENESIS'
        for r in rows:
            parts = [r[k] for k in ('at', 'user_id', 'action', 'object_id', 'result', 'detail', 'previous_hash')]
            if (r['previous_hash'] != prev or
                    hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest() != r['entry_hash']):
                return False
            prev = r['entry_hash']
        return True

    def login(self, user, password):
        """Проверяет пароль и выдает серверную сессию с CSRF-токеном."""
        with self.connect() as con:
            account = con.execute('SELECT role_id,password_hash FROM demo_accounts WHERE user_id=?',
                                  (user,)).fetchone()
        if not account or not password_matches(password, account['password_hash']):
            self.audit(user, 'login', 'denied')
            return None
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        self.sessions[token] = (user, csrf, time.monotonic() + 8 * 3600)
        self.audit(user, 'login', 'ok')
        return {'user_id': user, 'role': account['role_id'], 'csrf': csrf, 'token': token}

    def identity(self, cookie):
        """Получает текущую роль учетной записи для открытой сессии."""
        token = next((v.split('=', 1)[1] for v in cookie.split('; ') if v.startswith('demo_session=')), '')
        session = self.sessions.get(token)
        if not session or session[2] < time.monotonic():
            return None
        with self.connect() as con:
            account = con.execute('SELECT role_id FROM demo_accounts WHERE '
                                  'user_id=?', (session[0],)).fetchone()
        return {'user_id': session[0], 'role': account['role_id'], 'csrf': session[1]} if account else None

    def logout(self, cookie):
        """Удаляет серверную сессию и пишет событие выхода в аудит."""
        token = next((v.split('=', 1)[1] for v in cookie.split('; ') if v.startswith('demo_session=')), '')
        session = self.sessions.pop(token, None)
        if session:
            self.audit(session[0], 'logout', 'ok')

    def allowed(self, actor, permission, object_id=None):
        """Проверяет право роли; ограничение по object_id пока не настроено в демо."""
        return bool(actor and permission in self.permissions(actor))

    def permissions(self, actor):
        """Читает актуальные права роли из БД; роль браузеру не доверяется."""
        if not actor:
            return set()
        if actor['role'] == 'admin':
            return set(PERMISSIONS)
        with self.connect() as con:
            row = con.execute('SELECT permissions_json FROM roles WHERE role_id=?',
                              (actor['role'],)).fetchone()
        return set(json.loads(row['permissions_json'])) if row else set()

    def roles(self):
        """Возвращает редактируемые роли и неизменяемого системного администратора."""
        with self.connect() as con:
            rows = [dict(r) for r in con.execute('SELECT * FROM roles ORDER BY role_id')]
        return [{'role_id': 'admin', 'title': 'Администратор системы',
                 'permissions': sorted(PERMISSIONS), 'system': True}] + [
            {'role_id': r['role_id'], 'title': r['title'],
             'permissions': json.loads(r['permissions_json']), 'system': False} for r in rows]

    def users(self):
        """Перечисляет учетные записи без хешей паролей."""
        with self.connect() as con:
            return [dict(r) for r in con.execute('SELECT user_id,role_id,'
                                                 'created_at FROM demo_accounts ORDER BY user_id')]

    def save_role(self, payload, actor):
        """Создает либо обновляет набор прав роли и фиксирует изменение в аудите."""
        role_id = str(payload.get('role_id', '')).strip().lower()
        title = str(payload.get('title', '')).strip()[:80]
        rights = payload.get('permissions', [])
        if not re.fullmatch(r'[a-z][a-z0-9_]{1,31}', role_id) or role_id == 'admin':
            raise ValueError('Код роли: латинские буквы, цифры, _, от 2 до 32 знаков; admin зарезервирован')
        if (not title or not isinstance(rights, list) or
                any(not isinstance(right, str) for right in rights) or
                len(rights) != len(set(rights)) or not set(rights) <= set(PERMISSIONS)):
            raise ValueError('Нужно название и уникальные права из каталога')
        # Управление настройками остается у системного администратора.
        if set(rights) & ADMIN_ONLY:
            raise ValueError('Права настройки зарезервированы за администратором')
        with self.lock, self.connect() as con:
            con.execute('INSERT INTO roles VALUES(?,?,?,?) ON CONFLICT(role_id) '
                        'DO UPDATE SET title=excluded.title, permissions_json=excluded.permissions_json',
                        (role_id, title, json.dumps(rights), now()))
        self.audit(actor, 'roles.save', 'ok', detail=role_id + ': ' + ','.join(rights))
        return {'role_id': role_id, 'title': title, 'permissions': rights}

    def save_user(self, payload, actor):
        """Создает учетную запись или меняет ей роль и пароль."""
        user_id = str(payload.get('user_id', '')).strip().lower()
        role_id = str(payload.get('role_id', '')).strip()
        password = str(payload.get('password', ''))
        if not re.fullmatch(r'[a-z][a-z0-9_]{1,31}', user_id) or user_id == 'admin':
            raise ValueError('Логин: латиница, цифры, _, 2–32 знака; admin зарезервирован')
        with self.connect() as con:
            if not con.execute('SELECT 1 FROM roles WHERE role_id=?', (role_id,)).fetchone():
                raise ValueError('Сначала создайте роль')
            exists = con.execute('SELECT password_hash FROM demo_accounts WHERE user_id=?', (user_id,)).fetchone()
        if not exists and len(password) < 4:
            raise ValueError('Пароль новой записи — не менее 4 символов')
        if password and len(password) < 4:
            raise ValueError('Пароль — не менее 4 символов')
        hashed = password_hash(password) if password else exists['password_hash']
        with self.lock, self.connect() as con:
            con.execute('INSERT INTO demo_accounts VALUES(?,?,?,?) ON CONFLICT(user_id) '
                        'DO UPDATE SET role_id=excluded.role_id,password_hash=excluded.password_hash',
                        (user_id, hashed, role_id, now()))
        self.audit(actor, 'users.save', 'ok', detail=user_id + ' -> ' + role_id)
        return {'user_id': user_id, 'role_id': role_id}

    def sync_registry(self, csv_path, actor='admin'):
        """Сверяет CSV по стабильному ID, записывая новые, измененные и отсутствующие объекты."""
        frame = pd.read_csv(csv_path).fillna('')
        count = {'added': 0, 'updated': 0, 'missing': 0}
        with self.lock, self.connect() as con:
            existing = {r['object_id']: dict(r) for r in con.execute('SELECT * FROM registry')}
            seen = set()
            for r in frame.to_dict('records'):
                oid = int(r['ид_объект'])
                seen.add(oid)
                values = (int(r['родитель']) if r['родитель'] != '' else None,
                          int(r['иерархия_уровень']), str(r['вид_объекта']), str(r['диспетчерское_название_объекта']))
                old = existing.get(oid)
                if old and tuple(old[k] for k in ('parent_id', 'level', 'kind', 'name')) == values and old['active']:
                    continue
                change = 'updated' if old else 'added'
                count[change] += 1
                con.execute('INSERT INTO registry(object_id, parent_id, level,kind, name,active, '
                            'updated_at) VALUES(?,?,?,?,?,1,?) '
                            'ON CONFLICT(object_id) DO UPDATE SET parent_id=excluded.parent_id, '
                            'level=excluded.level, kind=excluded.kind, name=excluded.name, '
                            'active=1, updated_at=excluded.updated_at',
                            (oid, *values, now()))
                con.execute('INSERT INTO registry_changes(at, object_id, action, detail) VALUES(?,?,?,?)',
                            (now(), oid, change, str(values)))
            # Пропавшими считаем только записи источника; локальные дополнения сохраняем.
            for oid, old in existing.items():
                if oid not in seen and old['active'] and old['kind'] != 'manual':
                    count['missing'] += 1
                    con.execute('UPDATE registry SET active=0,updated_at=? WHERE object_id=?', (now(), oid))
                    con.execute('INSERT INTO registry_changes(at,object_id,action,detail) VALUES(?,?,?,?)',
                                (now(), oid, 'missing', 'source registry'))
        self.audit(actor, 'registry.sync', 'ok', detail=count)
        return count

    def add_object(self, payload, actor):
        """Добавляет локальный объект с отрицательным ID без конфликта с реестром заказчика."""
        parent = int(payload['parent_id'])
        name = str(payload['name']).strip()[:120]
        if not name:
            raise ValueError('Укажите название')
        with self.lock, self.connect() as con:
            row = con.execute('SELECT level FROM registry WHERE object_id=? AND active=1',
                              (parent,)).fetchone()
            if not row or row['level'] >= 3:
                raise ValueError('Нужен родитель уровня 1 или 2')
            # Отрицательные ID зарезервированы для локальных объектов и не пересекаются
            # с положительными ID реестра заказчика.
            oid = min(-1, int(con.execute('SELECT COALESCE(MIN(object_id),0) FROM registry').fetchone()[0]) - 1)
            con.execute('INSERT INTO registry VALUES(?,?,?,?,?,?,?)',
                        (oid, parent, row['level'] + 1, 'manual', name, 1, now()))
            con.execute('INSERT INTO registry_changes(at,object_id,action,detail) VALUES(?,?,?,?)',
                        (now(), oid, 'local_add', name))
        self.audit(actor, 'objects.create', 'ok', oid)
        return {'object_id': oid, 'status': 'local demo only'}

    def objects(self):
        """Возвращает активную иерархию объектов для дерева и схемы."""
        with self.connect() as con:
            return [dict(r) for r in con.execute('SELECT * FROM registry WHERE active=1 ORDER BY level,object_id')]

    def notify(self, object_id, category, source_day, priority, title):
        """Создает одно оповещение на объект, систему и день источника."""
        with self.lock, self.connect() as con:
            cur = con.execute(
                'INSERT OR IGNORE INTO notifications(object_id, category, source_day, priority, title, '
                'created_at) VALUES(?,?,?,?,?,?)',
                (object_id, category, source_day, priority, title, now()))
            return cur.rowcount == 1

    def report_rows(self, since, until):
        """Выбирает одинаковый период локальных журналов для экрана, PDF и Excel."""
        with self.connect() as con:
            decisions = con.execute(
                'SELECT * FROM decisions WHERE date(created_at) BETWEEN ? AND ? ORDER BY created_at',
                (since, until)).fetchall()
            alerts = con.execute(
                'SELECT * FROM notifications WHERE date(created_at) BETWEEN ? AND ? ORDER BY created_at',
                (since, until)).fetchall()
            tickets = con.execute('SELECT * FROM tickets WHERE date(created_at) BETWEEN ? AND ? ORDER BY created_at',
                                  (since, until)).fetchall()
            audit = con.execute('SELECT * FROM audit WHERE date(at) BETWEEN ? AND ? ORDER BY id',
                                (since, until)).fetchall()
        return {k: [dict(r) for r in v] for k, v in
                {'decisions': decisions, 'notifications': alerts, 'tickets': tickets, 'audit': audit}.items()}


class Replay:
    """Воспроизводит последние исторические дни как демонстрационный поток."""

    def __init__(self, store: Store, path: Path):
        """Загружает последние семь дат дневного ряда и готовит курсор воспроизведения."""
        self.store = store
        self.lock = threading.Lock()
        self.running = False
        self.speed = 1.0
        self.events = pd.read_csv(path).sort_values(['date', 'object_id', 'category']).to_dict('records') if Path(
            path).exists() else []
        # Берем последние даты источника для короткого демо; позиция воспроизведения стабильна.
        dates = sorted({e['date'] for e in self.events})[-7:]
        self.events = [e for e in self.events if e['date'] in dates]
        self.cursor = 0
        self.thread = None
        self.last_processed = None
        self.failures = 0

    def state(self):
        """Возвращает прогресс воспроизведения и день последнего принятого источника."""
        with self.lock:
            watermark = self.events[self.cursor - 1]['date'] if self.cursor else None
            return {'running': self.running, 'cursor': self.cursor, 'total': len(self.events),
                    'source_watermark': watermark, 'last_processed_at': self.last_processed,
                    'failures': self.failures, 'mode': 'ИСТОРИЧЕСКАЯ ЭМУЛЯЦИЯ — не live'}

    def step(self, count=1):
        """Обрабатывает ограниченную пачку с защитой от дублей и повторами при блокировке БД."""
        count = max(1, min(100, int(count)))
        processed = 0
        with self.lock:
            chunk = self.events[self.cursor:self.cursor + count]
            self.cursor += len(chunk)
        for e in chunk:
            stamp = str(e['date']) + 'T23:59:00+00:00'
            key = (int(e['object_id']), str(e['category']))
            for attempt in range(3):
                try:
                    with self.store.connect() as con:
                        cur = con.execute(
                            'INSERT OR IGNORE INTO stream_events(source_at, received_at, processed_at, object_id, '
                            'category, readings, alarms) VALUES(?,?,?,?,?,?,?)',
                            (stamp, now(), now(), *key, int(e['readings']), int(e['alarms'])))
                    if cur.rowcount:
                        risk = self.store.by_key.get(key)
                        # Не присоединяем поздний прогноз к раннему дню воспроизведения.
                        # Исторические тревоги создают демонстрационные оповещения о сигнале;
                        # прогнозное оповещение требует совпадения даты прогноза.
                        if int(e['alarms']) > 0:
                            title = f"Исторический сигнал: {risk['category_ru'] if risk else key[1]}"
                            self.store.notify(*key, str(e['date']), 'critical', title)
                        elif risk and str(e['date']) == str(risk['date']) and risk['status'] == 'Проверить':
                            self.store.notify(*key, str(e['date']),
                                              'high', f"Прогноз: {risk['category_ru']}")
                    processed += 1
                    self.last_processed = now()
                    break
                except sqlite3.OperationalError:
                    if attempt == 2:
                        self.failures += 1
                    else:
                        time.sleep(0.05 * (2 ** attempt))
        return {'processed': processed, **self.state()}

    def start(self, speed=1.0):
        """Запускает фоновое воспроизведение с заданным числом записей в секунду."""
        self.speed = max(.2, min(20, float(speed)))
        if self.running:
            return self.state()
        self.running = True

        def run():
            """До конца источника по одной записи вызывает обработку и выдерживает темп."""
            while self.running and self.cursor < len(self.events):
                self.step(1)
                time.sleep(1 / self.speed)
            self.running = False

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        return self.state()

    def stop(self):
        """Приостанавливает поток, сохраняя позицию курсора."""
        self.running = False
        return self.state()
