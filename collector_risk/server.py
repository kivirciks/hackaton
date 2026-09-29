"""Локальный HTTP API: серверные проверки доступа и явно обозначенные заглушки."""
from __future__ import annotations
import argparse
from datetime import date, timedelta
import json
import mimetypes
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import pandas as pd
from .reports import build_report, xlsx, pdf
from .service import Store, PERMISSIONS, now

HERE = Path(__file__).parent


def create_server(artifact_dir, objects_csv, host='127.0.0.1', port=8080, channels_csv=None, registry_csv=None):
    """Собирает локальный сервер из готового прогноза, справочников и хранилища."""
    base = Path(artifact_dir)
    objects_csv = Path(objects_csv)
    forecasts = pd.read_csv(base / 'forecast.csv').fillna('')
    objects = pd.read_csv(objects_csv)
    names = dict(zip(objects['ид_объект'], objects['диспетчерское_название_объекта']))
    forecasts['object_name'] = forecasts.object_id.map(names).fillna('Объект ' + forecasts.object_id.astype(str))
    records = forecasts.to_dict('records')
    # Пакет на 23:59 дня t относится только к следующим календарным суткам.
    for record in records:
        next_day = date.fromisoformat(str(record['date'])) + timedelta(days=1)
        record['valid_from'] = next_day.isoformat() + 'T00:00:00'
        record['valid_to'] = (next_day + timedelta(days=1)).isoformat() + 'T00:00:00'
    channels = pd.read_csv(channels_csv or objects_csv.parent / 'справочник_каналов_датчиков.csv')
    categories = {int(oid): sorted(set(g.тип_инж_системы)) for oid, g in channels.groupby('ид_объект')}
    store = Store(base / 'feedback.sqlite3', objects_csv, base / 'daily.csv', records)
    registry_csv = registry_csv or objects_csv
    metrics = json.loads((base / 'metrics.json').read_text(encoding='utf-8'))

    class Handler(BaseHTTPRequestHandler):
        """Маршрутизирует HTTP-запросы и проверяет права перед каждым API-действием."""

        def log_message(self, fmt, *args):
            """Отключает консольный лог: доступ фиксируется в структурном аудите."""
            pass

        def output(self, code, payload, mime='application/json; charset=utf-8', headers=None):
            """Сериализует ответ и задает заголовки запрета кэширования."""
            body = (json.dumps(payload, ensure_ascii=False, allow_nan=False,
                               default=str).encode() if isinstance(payload, (dict, list)) else payload)
            self.send_response(code)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def actor(self, permission, object_id=None, write=False):
            """Проверяет сессию, право и CSRF-токен; отказ пишет в аудит."""
            user = store.identity(self.headers.get('Cookie', ''))
            who = user['user_id'] if user else 'anonymous'
            if not user:
                store.audit(who, permission, 'unauthorized',
                            object_id)
                self.output(401, {'error': 'Войдите в демо'})
                return None
            if (not store.allowed(user, permission, object_id)
                    or (write and self.headers.get('X-CSRF-Token') != user['csrf'])):
                store.audit(who, permission, 'denied', object_id)
                self.output(403, {'error': 'Нет прав на действие или объект'})
                return None
            store.audit(who, permission, 'ok', object_id)
            return user

        def body(self):
            """Ограничивает размер и форму входящего JSON для локального API."""
            n = int(self.headers.get('Content-Length', '0'))
            if not 2 <= n <= 8192:
                raise ValueError('Некорректный размер запроса')
            value = json.loads(self.rfile.read(n))
            if not isinstance(value, dict):
                raise ValueError('Ожидается JSON объект')
            return value

        def do_GET(self):
            """Обслуживает чтение прогнозов, реестра, ролей, отчетов и статусов."""
            p = urlparse(self.path)
            path = p.path
            qs = parse_qs(p.query)
            try:
                if path == '/health':
                    return self.output(200, {'ok': True, 'forecast_date': str(forecasts.date.max()),
                                             'mode': 'historical demo'})
                if path == '/api/me':
                    a = store.identity(self.headers.get('Cookie', ''))
                    return self.output(200, dict(a, permissions=sorted(store.permissions(a))) if a else {
                        'authenticated': False})
                if path == '/api/roles':
                    if not self.actor('roles:manage'):
                        return
                    return self.output(200,
                                       {'permissions': PERMISSIONS, 'roles': store.roles(),
                                        'users': store.users()})
                if path == '/api/report/options':
                    if not self.actor('reports:read'):
                        return
                    systems = sorted({(r['category'], r['category_ru']) for r in records}, key=lambda x: x[1])
                    return self.output(200, {'systems': [{'id': code, 'title': title} for code, title in systems]})
                if path == '/api/forecasts':
                    a = self.actor('forecasts:read')
                    if a:
                        return self.output(200, [r for r in records
                                                 if store.allowed(a, 'forecasts:read', r['object_id'])])
                elif path == '/api/objects':
                    a = self.actor('objects:read')
                    if a:
                        return self.output(200, [dict(o, categories=categories.get(o['object_id'], []))
                                                 for o in store.objects()
                                                 if store.allowed(a, 'objects:read', o['object_id'])])
                elif path == '/api/equipment':
                    oid = int(qs.get('object_id', [''])[0])
                    a = self.actor('objects:read', oid)
                    if a:
                        source = channels[channels['ид_объект'].eq(oid)]
                        system = qs.get('system', [''])[0].strip()
                        query = qs.get('q', [''])[0].strip().lower()[:100]
                        if system:
                            source = source[source['тип_инж_системы'].eq(system)]
                        if query:
                            source = source[
                                source[['название_датчика', 'тип_датчика', 'ид_канала_данных']].astype(str).apply(
                                    lambda col: col.str.lower().str.contains(query, regex=False)).any(axis=1)]
                        source_total = len(source)
                        source = source.head(200)
                        items = [{'channel_id': int(r['ид_канала_данных']), 'system_name': str(r['тип_инж_системы']),
                                  'sensor_type': str(r['тип_датчика']), 'name': str(r['название_датчика']),
                                  'source': 'справочник каналов'}
                                 for r in source.to_dict('records')]
                        with store.connect() as con:
                            local = [dict(r) for r in
                                     con.execute('SELECT * FROM equipment_local WHERE object_id=? ORDER BY id', (oid,))]
                        local = [r for r in local if (not system or r['system_name'] == system) and
                                 (not query or query in ' '.join(
                                     str(r[k]) for k in ('name', 'sensor_type', 'id')).lower())]
                        return self.output(200, {'object_id': oid, 'items': items + local, 'shown_source': len(source),
                                                 'source_total': int(channels['ид_объект'].eq(oid).sum()),
                                                 'matched_total': source_total + len(local)})
                elif path == '/api/metrics':
                    if self.actor('reports:read'):
                        return self.output(200, metrics)
                elif path in (
                        '/api/decisions', '/api/notifications', '/api/stream/events', '/api/audit',
                        '/api/registry/changes'):
                    setting = {'/api/decisions': ('decisions:write', 'decisions', 'id'),
                               '/api/notifications': ('notifications:read', 'notifications', 'id'),
                               '/api/stream/events': ('stream:write', 'stream_events', 'id'),
                               '/api/audit': ('audit:read', 'audit', 'id'), '/api/registry/changes':
                                   ('registry:sync', 'registry_changes', 'id')}
                    permission, table, order = setting[path]
                    a = self.actor(permission)
                    if a:
                        with store.connect() as con:
                            rows = [dict(r) for r in
                                    con.execute(f'SELECT * FROM {table} ORDER BY {order} DESC LIMIT 200')]
                        if path not in ('/api/audit', '/api/registry/changes'):
                            rows = [r for r in rows if store.allowed(a, permission, r['object_id'])]
                        return self.output(200, {'chain_ok': store.verify_audit(),
                                                 'rows': rows} if path == '/api/audit' else rows)
                elif path == '/api/stream':
                    if self.actor('stream:write'):
                        return self.output(200, store.replay.state())
                elif path.startswith('/api/tickets/'):
                    ticket_id = path.rsplit('/', 1)[-1]
                    if not ticket_id.isdigit():
                        raise ValueError('ID заявки — число')
                    a = self.actor('tickets:read')
                    if a:
                        with store.connect() as con:
                            ticket = con.execute('SELECT * FROM tickets WHERE id=?', (int(ticket_id),)).fetchone()
                        if ticket and not store.allowed(a, 'tickets:read', ticket['object_id']):
                            store.audit(a['user_id'], 'tickets:read', 'denied', ticket['object_id'])
                            return self.output(403, {'error': 'Нет доступа к объекту'})
                        return self.output(200, {'id': ticket_id,
                                                 'external_status': 'НЕДОСТУПЕН — ДЕМО-ЗАГЛУШКА',
                                                 'source': 'stub: внешняя система заявок не подключена',
                                                 'local_draft': dict(ticket) if ticket else None})
                elif path == '/api/reports' or path in ('/api/reports.xlsx', '/api/reports.pdf'):
                    if self.actor('reports:read'):
                        start = qs.get('from', [str(date.today() - timedelta(days=30))])[0]
                        end = qs.get('to', [str(date.today())])[0]
                        report = build_report(store, base / 'metrics.json', records,
                                              qs.get('kind', ['management'])[0], start, end,
                                              qs.get('object_id', [None])[0], qs.get('system', [''])[0],
                                              qs.get('status', [''])[0], qs.get('min_score', [None])[0])
                        if path.endswith('.xlsx'):
                            return self.output(200, xlsx(report),
                                               'application/vnd.openxmlformats-officedocument.'
                                               'spreadsheetml.sheet',
                                               {'Content-Disposition': 'attachment; '
                                                                       'filename="management-report.xlsx"'})
                        if path.endswith('.pdf'):
                            return self.output(200, pdf(report), 'application/pdf',
                                               {'Content-Disposition': 'attachment; '
                                                                       'filename="management-report.pdf"'})
                        return self.output(200, report)
                else:
                    file = {'/': 'index.html', '/app.css': 'app.css', '/app.js': 'app.js'}.get(path)
                    if file:
                        return self.output(200, (HERE / 'web' / file).read_bytes(),
                                           mimetypes.guess_type(file)[0] or 'text/html')
                    return self.output(404, {'error': 'Not found'})
            except (ValueError, TypeError) as ex:
                return self.output(400, {'error': str(ex)})

        def do_POST(self):
            """Проверяет и сохраняет решения, заявки, роли и настройки демо."""
            path = urlparse(self.path).path
            try:
                body = self.body()
                if path == '/api/login':
                    a = store.login(str(body.get('user', '')), str(body.get('password', '')))
                    if not a:
                        return self.output(401, {'error': 'Неверные данные'})
                    token = a.pop('token')
                    return self.output(200, a, headers={
                        'Set-Cookie': f'demo_session={token}; HttpOnly; SameSite=Strict; Path=/'})
                if path == '/api/logout':
                    store.logout(self.headers.get('Cookie', ''))
                    return self.output(200, {'ok': True}, headers={
                        'Set-Cookie': 'demo_session=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/'})
                if path == '/api/roles':
                    actor = self.actor('roles:manage', write=True)
                    if not actor:
                        return
                    return self.output(200, store.save_role(body, actor['user_id']))
                if path == '/api/users':
                    actor = self.actor('roles:manage', write=True)
                    if not actor:
                        return
                    return self.output(200, store.save_user(body, actor['user_id']))
                if path == '/api/decisions':
                    oid = int(body['object_id'])
                    category = str(body['category'])
                    a = self.actor('decisions:write', oid, True)
                    if not a:
                        return
                    if (oid, category) not in store.by_key:
                        raise ValueError('Пара объект/система отсутствует')
                    decision = str(body['decision'])
                    reason = str(body.get('reason', '')).strip()[:500]
                    if decision not in (
                            'проверка', 'выезд', 'ложное срабатывание', 'наблюдение') or not reason:
                        raise ValueError('Проверьте действие и основание')
                    with store.connect() as con:
                        cur = con.execute(
                            'INSERT INTO decisions(created_at, object_id, category, decision, reason, operator) '
                            'VALUES(?,?,?,?,?,?)',
                            (now(), oid, category, decision, reason, a['user_id']))
                    store.audit(a['user_id'], 'decisions.create', 'ok', oid, cur.lastrowid)
                    return self.output(201, {'id': cur.lastrowid, 'status': 'recorded'})
                if path == '/api/objects':
                    a = self.actor('objects:write', write=True)
                    if not a:
                        return
                    return self.output(201, store.add_object(body, a['user_id']))
                if path == '/api/equipment':
                    oid = int(body['object_id'])
                    a = self.actor('equipment:write', oid, True)
                    if not a:
                        return
                    if not any(o['object_id'] == oid for o in store.objects()):
                        raise ValueError('Объект не найден')
                    values = [str(body.get(k, '')).strip()[:120] for k in ('system_name', 'sensor_type', 'name')]
                    if not all(values):
                        raise ValueError('Заполните систему, тип и название')
                    with store.connect() as con:
                        cur = con.execute('INSERT INTO equipment_local(object_id, '
                                          'system_name, sensor_type, name,created_at) VALUES(?,?,?,?,?)',
                                          (oid, *values, now()))
                    store.audit(a['user_id'], 'equipment.create', 'ok', oid, cur.lastrowid)
                    return self.output(201, {'id': cur.lastrowid, 'status': 'local demo only'})
                if path == '/api/registry/sync':
                    a = self.actor('registry:sync', write=True)
                    if not a:
                        return
                    return self.output(200, dict(store.sync_registry(registry_csv, a['user_id']),
                                                 mode='stub: local CSV snapshot'))
                if path == '/api/stream/control':
                    if not self.actor('stream:write', write=True):
                        return
                    cmd = body.get('command')
                    if cmd == 'start':
                        out = store.replay.start(body.get('speed', 1))
                    elif cmd == 'pause':
                        out = store.replay.stop()
                    elif cmd == 'step':
                        out = store.replay.step(body.get('count', 1))
                    else:
                        raise ValueError('Команда: start, pause, step')
                    return self.output(200, out)
                if path.startswith('/api/notifications/') and path.endswith('/ack'):
                    nid = int(path.split('/')[-2])
                    with store.connect() as con:
                        r = con.execute('SELECT object_id FROM notifications WHERE id=?', (nid,)).fetchone()
                    if not r:
                        return self.output(404, {'error': 'Уведомление не найдено'})
                    a = self.actor('notifications:ack', r['object_id'], True)
                    if not a:
                        return
                    with store.connect() as con:
                        con.execute('UPDATE notifications SET acknowledged_at=?, '
                                    'acknowledged_by=? WHERE id=? AND acknowledged_at IS NULL',
                                    (now(), a['user_id'], nid))
                    store.audit(a['user_id'], 'notifications.ack', 'ok', r['object_id'], nid)
                    return self.output(200, {'status': 'acknowledged'})
                if path == '/api/tickets/drafts':
                    oid = int(body['object_id'])
                    a = self.actor('tickets:write', oid, True)
                    if not a:
                        return
                    if not any(o['object_id'] == oid for o in store.objects()):
                        raise ValueError('Объект не найден')
                    equipment = str(body.get('equipment', '')).strip()[:120]
                    reason = str(body.get('reason', '')).strip()[:500]
                    priority = str(body.get('priority', ''))
                    due = str(body.get('due_at', ''))
                    date.fromisoformat(due)
                    if not equipment or not reason or priority not in (
                            'обычный', 'высокий', 'критический'):
                        raise ValueError('Заполните оборудование, причину и приоритет')
                    with store.connect() as con:
                        cur = con.execute('INSERT INTO tickets(object_id, equipment, reason, priority, '
                                          'due_at,status, created_at) VALUES(?,?,?,?,?,?,?)',
                                          (oid, equipment, reason, priority, due, 'draft', now()))
                    store.audit(a['user_id'], 'tickets.draft', 'ok', oid, cur.lastrowid)
                    return self.output(201, {'id': cur.lastrowid, 'status': 'draft', 'external_sent': False})
                if path.startswith('/api/tickets/') and path.endswith('/confirm'):
                    tid = int(path.split('/')[-2])
                    with store.connect() as con:
                        r = con.execute('SELECT * FROM tickets WHERE id=?', (tid,)).fetchone()
                    if not r:
                        return self.output(404, {'error': 'Черновик не найден'})
                    a = self.actor('tickets:write', r['object_id'], True)
                    if not a:
                        return
                    if body.get('confirm') is not True:
                        raise ValueError('Требуется подтверждение человека')
                    with store.connect() as con:
                        con.execute('UPDATE tickets SET status=?,confirmed_by=? WHERE id=? AND status=?',
                                    ('confirmed_locally', a['user_id'], tid, 'draft'))
                    store.audit(a['user_id'], 'tickets.confirm', 'ok', r['object_id'], tid)
                    return self.output(200, {'id': tid, 'status': 'confirmed_locally', 'external_sent': False})
                return self.output(404, {'error': 'Not found'})
            except (KeyError, ValueError, TypeError, json.JSONDecodeError) as ex:
                return self.output(400, {'error': str(ex)})

    srv = ThreadingHTTPServer((host, port), Handler)
    srv.store = store
    return srv


def main():
    """Разбирает параметры CLI и запускает HTTP-сервер на localhost."""
    p = argparse.ArgumentParser()
    p.add_argument('--artifacts', default='artifacts')
    p.add_argument('--objects', default='upload/справочник_объектов_диспетчер.csv')
    p.add_argument('--channels', default=None)
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8080)
    a = p.parse_args()
    srv = create_server(a.artifacts, a.objects, a.host, a.port, a.channels)
    print(f'http://{a.host}:{srv.server_address[1]}', flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == '__main__':
    main()
