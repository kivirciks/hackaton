"""Смешанная локальная нагрузка GET/POST с измерением квантилей."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError


def percentile(values, q):
    """Находит эмпирический квантиль задержки по правилу ближайшего ранга."""
    values = sorted(values)
    return values[max(0, min(len(values) - 1, math.ceil(q * len(values)) - 1))]


def probe(base, users=20, requests_each=10, object_id=None, category=None):
    """Создает отдельные сессии и измеряет параллельные GET/POST на копии демо-БД."""

    def request(path, data=None, cookie='', csrf=''):
        """Отправляет HTTP-запрос и возвращает задержку, код и ответ."""
        payload = json.dumps(data).encode() if data is not None else None
        headers = {'Cookie': cookie, 'X-CSRF-Token': csrf, 'Content-Type': 'application/json'}
        req = Request(base + path, data=payload, headers=headers)
        start = time.perf_counter()
        try:
            with urlopen(req, timeout=15) as r:
                return time.perf_counter() - start, r.status, r.read(), r.headers
        except HTTPError as e:
            return time.perf_counter() - start, e.code, e.read(), e.headers

    if object_id is None:
        _, _, body, _ = request('/health')
        raise ValueError('Укажите object_id и category существующего прогноза')
    sessions = []
    for _ in range(users):
        _, code, body, headers = request('/api/login', {'user': 'dispatcher', 'password': 'demo'})
        if code != 200:
            raise RuntimeError('login failed')
        sessions.append((headers.get('Set-Cookie').split(';')[0], json.loads(body)['csrf']))

    def worker(i):
        """Выполняет одну операцию чтения или записи с отдельной пользовательской сессией."""
        cookie, csrf = sessions[i % users]
        if i % 2:
            body = {'object_id': object_id, 'category': category, 'decision': 'наблюдение',
                    'reason': 'Нагрузочная проверка демо'}
            elapsed, status, _, _ = request('/api/decisions', body, cookie, csrf)
        else:
            elapsed, status, _, _ = request('/api/forecasts', cookie=cookie)
        return elapsed, status

    with ThreadPoolExecutor(max_workers=users) as pool:
        measurements = [f.result() for f in
                        as_completed([pool.submit(worker, i) for i in range(users * requests_each)])]
    values = [x[0] for x in measurements]
    return {'users': users, 'requests': len(values), 'get_post_mix': '50/50', 'p95_s': percentile(values, .95),
            'p99_s': percentile(values, .99), 'errors': sum(status >= 400 for _, status in measurements),
            'scope': 'локальный тест; целевую инфраструктуру и production ASGI/WSGI проверять отдельно'}


def main():
    """Читает параметры локального нагрузочного теста из командной строки."""
    p = argparse.ArgumentParser()
    p.add_argument('--base', default='http://127.0.0.1:8080')
    p.add_argument('--users', type=int, default=20)
    p.add_argument('--requests-each', type=int, default=10)
    p.add_argument('--object-id', type=int, required=True)
    p.add_argument('--category', required=True)
    a = p.parse_args()
    print(json.dumps(probe(a.base, a.users, a.requests_each, a.object_id, a.category), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
