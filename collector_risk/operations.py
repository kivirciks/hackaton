"""Версионирование источников, проверки качества, резервирование и измерения."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import statistics
import tempfile
import time

import joblib
import numpy as np
import pandas as pd
from .model import add_enhanced_features, make_features
from .pipeline import aggregate


def digest(path):
    """Считает SHA-256 файла порциями, сохраняя ограниченное потребление памяти."""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''): h.update(block)
    return h.hexdigest()


def quality(daily):
    """Проверяет уникальность ключей, даты и допустимость счетчиков телеметрии."""
    required = {'date', 'object_id', 'category', 'readings', 'alarms', 'channels'}
    if not required.issubset(daily):
        raise ValueError('Нет обязательных колонок ' + str(required - set(daily.columns)))
    errors = []
    if daily.duplicated(['date', 'object_id', 'category']).any():
        errors.append('duplicate keys')
    if (daily.readings <= 0).any() or (daily.alarms < 0).any() or (daily.alarms > daily.readings).any():
        errors.append('invalid counts')
    if pd.to_datetime(daily.date, errors='coerce').isna().any():
        errors.append('invalid dates')
    return {'ok': not errors, 'errors': errors, 'rows': len(daily), 'date_min': str(daily.date.min()),
            'date_max': str(daily.date.max()), 'objects': int(daily.object_id.nunique())}


def refresh(source, artifacts, confirmed_labels=None):
    """Создает версию набора по хешу входов и сохраняет подтверждения отдельно от метки тревоги."""
    source = Path(source)
    out = Path(artifacts)
    out.mkdir(parents=True, exist_ok=True)
    paths = sorted(source.glob('ext-journal-*.7z'))
    if not paths:
        paths = sorted(source.glob('журнал_событий*.csv'))
    if not paths:
        raise FileNotFoundError('Нет CSV/7z для ingest')
    channels = source / 'справочник_каналов_датчиков.csv'
    if not channels.exists():
        raise FileNotFoundError(channels)
    signature = hashlib.sha256(''.join(digest(p) for p in [*paths, channels]).encode()).hexdigest()
    manifest = out / 'dataset_manifest.json'
    previous = json.loads(manifest.read_text()) if manifest.exists() else {}
    label_hash = digest(confirmed_labels) if confirmed_labels else None
    if previous.get('source_hash') == signature and previous.get('confirmed_labels_hash') == label_hash:
        return {'status': 'unchanged', 'version': previous.get('version')}
    daily, stats = aggregate(paths, channels)
    q = quality(daily)
    if not q['ok']:
        raise ValueError('Quality gate: ' + ', '.join(q['errors']))
    # Версию вычисляем из содержимого, а не из изменяемого времени файла.
    version = signature[:12]
    versions = out / 'versions' / version
    versions.mkdir(parents=True, exist_ok=True)
    daily.to_csv(versions / 'daily.csv', index=False)
    report = {'source_hash': signature, 'version': version, 'source_files': [p.name for p in paths],
              'quality': q, 'source_stats': stats, 'created_at': datetime.now(timezone.utc).isoformat(),
              'confirmed_labels_hash': label_hash, 'confirmation_status': 'not supplied'}
    if confirmed_labels:
        labels = pd.read_csv(confirmed_labels)
        needed = {'object_id', 'incident_type', 'occurred_at', 'confirmed_by'}
        if not needed.issubset(labels):
            raise ValueError('Нужны колонки подтверждения ' + str(needed - set(labels)))
        labels.to_csv(versions / 'confirmed_labels.csv', index=False)
        report[
            'confirmation_status'] = f'{len(labels)} подтверждений на разбор; они не обучают модель тревожных сигналов'
    (versions / 'manifest.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    shutil.copy2(versions / 'daily.csv', out / 'daily.csv')
    (out / 'data_quality.json').write_text(json.dumps(q, ensure_ascii=False, indent=2))
    manifest.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return {'status': 'ingested', 'version': version, 'quality': q,
            'confirmation_status': report['confirmation_status']}


def backup(artifacts, destination):
    """Копирует SQLite согласованно с транзакциями и сохраняет артефакты модели."""
    source = Path(artifacts)
    dest = Path(destination)
    dest.mkdir(parents=True, exist_ok=True)
    db = source / 'feedback.sqlite3'
    if db.exists():
        with sqlite3.connect(db) as src, sqlite3.connect(dest / 'feedback.sqlite3') as dst:
            src.backup(dst)
    paths = ['model.joblib', 'metrics.json', 'forecast.csv', 'daily.csv', 'dataset_manifest.json', 'data_quality.json']
    for name in paths:
        if (source / name).exists():
            shutil.copy2(source / name, dest / name)
    manifest = {p.name: digest(p) for p in dest.iterdir() if p.is_file()}
    (dest / 'backup_manifest.json').write_text(json.dumps(manifest, indent=2))
    return manifest


def verify_backup(destination):
    """Проверяет хеши и пробное восстановление базы во временную папку."""
    dest = Path(destination)
    expected = json.loads((dest / 'backup_manifest.json').read_text())
    if any(digest(dest / name) != sha for name, sha in expected.items()):
        return False
    if (dest / 'feedback.sqlite3').exists():
        with tempfile.TemporaryDirectory() as tmp:
            with sqlite3.connect(dest / 'feedback.sqlite3') as src, sqlite3.connect(
                    Path(tmp) / 'restore.sqlite3') as dst:
                src.backup(dst)
            with sqlite3.connect(Path(tmp) / 'restore.sqlite3') as db:
                if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    return False
    return True


def infer_one(bundle, frame, object_id):
    """Считает прогноз для одного объекта только по наблюдаемой истории до отсечения."""
    rows = frame[frame.object_id.eq(int(object_id))].copy()
    if rows.empty:
        raise ValueError('Неизвестный object_id')
    f = add_enhanced_features(make_features(rows))
    latest = f.sort_values('date').groupby('category', as_index=False).tail(1)
    # Пропуски оставляем NaN, как при обучении; алгоритм HGB их обрабатывает.
    x = latest[bundle['features']].replace([np.inf, -np.inf], np.nan).astype('float32')
    x2 = latest[bundle['context_features']].replace([np.inf, -np.inf], np.nan).astype('float32')
    raw = .75 * bundle['model'].predict_proba(x)[:, 1] + .25 * bundle['context_model'].predict_proba(x2)[:, 1]
    return dict(zip(latest.category, bundle['calibrator'].predict(raw).tolist()))


def benchmark(artifacts, object_id, repeats=30):
    """Замеряет холодную загрузку и p50/p95/p99 одного объекта на локальном компьютере."""
    root = Path(artifacts)
    t0 = time.perf_counter()
    bundle = joblib.load(root / 'model.joblib')
    frame = pd.read_csv(root / 'daily.csv')
    cold = time.perf_counter() - t0
    times = []
    for _ in range(repeats):
        t = time.perf_counter()
        infer_one(bundle, frame, object_id)
        times.append(time.perf_counter() - t)
    ordered = sorted(times)

    def percentile(q):
        """Возвращает квантиль задержки для контроля порога p95."""
        return ordered[min(len(ordered) - 1, int(np.ceil(q * len(ordered))) - 1)]

    return {'object_id': object_id, 'rows': len(frame), 'repeats': repeats, 'cold_load_s': cold,
            'p50_s': statistics.median(times), 'p95_s': percentile(.95), 'p99_s': percentile(.99),
            'sla_300s_local': percentile(.95) <= 300,
            'environment': {'os': platform.platform(), 'python': platform.python_version(),
                            'cpu_count': os.cpu_count()},
            'scope': 'локальная машина; требуется повторить на целевом Linux сервере'}


def main():
    """Запускает команды ingest, копирования, проверки и измерения из CLI."""
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest='command', required=True)
    r = sub.add_parser('refresh')
    r.add_argument('--source', required=True)
    r.add_argument('--artifacts', default='artifacts')
    r.add_argument('--confirmed-labels')
    b = sub.add_parser('backup')
    b.add_argument('--artifacts', default='artifacts')
    b.add_argument('--destination', required=True)
    v = sub.add_parser('verify-backup')
    v.add_argument('--destination', required=True)
    t = sub.add_parser('benchmark')
    t.add_argument('--artifacts', default='artifacts')
    t.add_argument('--object-id', type=int, required=True)
    t.add_argument('--repeats', type=int, default=30)
    t.add_argument('--max-p95', type=float, default=300, help='Nonzero exit if local p95 exceeds seconds')
    a = p.parse_args()
    if a.command == 'refresh':
        result = refresh(a.source, a.artifacts, a.confirmed_labels)
    elif a.command == 'backup':
        result = backup(a.artifacts, a.destination)
    elif a.command == 'verify-backup':
        result = {'ok': verify_backup(a.destination)}
    else:
        result = benchmark(a.artifacts, a.object_id, a.repeats)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if a.command == 'benchmark' and result['p95_s'] > a.max_p95:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
