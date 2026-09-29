"""Ежедневный запуск ingest и обновления модели с проверкой качества."""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import tempfile
import time

from .model import train
from .operations import refresh, backup, verify_backup


def run_once(source, artifacts, confirmed_labels=None, max_ap_drop=.10):
    """Ставит новые данные и модель в промежуточную версию, проверяет качество и делает резервную копию."""
    out = Path(artifacts)
    old = json.loads((out / 'metrics.json').read_text()) if (out / 'metrics.json').exists() else None
    with tempfile.TemporaryDirectory(dir=out) as staged:
        previous = Path(staged) / 'previous_daily.csv'
        if (out / 'daily.csv').exists():
            shutil.copy2(out / 'daily.csv', previous)
        previous_manifest = Path(staged) / 'previous_manifest.json'
        if (out / 'dataset_manifest.json').exists():
            shutil.copy2(out / 'dataset_manifest.json', previous_manifest)
        status = refresh(source, out, confirmed_labels)
        if status['status'] == 'unchanged':
            destination = out / 'backups' / datetime.now(timezone.utc).date().isoformat()
            backup(out, destination)
            return dict(status, backup_verified=verify_backup(destination))
        try:
            new = train(out / 'daily.csv', staged)
            if old and new['average_precision'] < old['average_precision'] - max_ap_drop:
                if previous.exists():
                    shutil.copy2(previous, out / 'daily.csv')
                if previous_manifest.exists():
                    shutil.copy2(previous_manifest, out / 'dataset_manifest.json')
                else:
                    (out / 'dataset_manifest.json').unlink(missing_ok=True)
                return {'status': 'held_for_review', 'reason': 'average_precision drop on new chronological test',
                        'old_ap': old['average_precision'], 'new_ap': new['average_precision']}
            version = out / 'versions' / status['version']
            for name in ('model.joblib', 'metrics.json', 'forecast.csv'):
                shutil.copy2(Path(staged) / name, version / name)
                shutil.copy2(Path(staged) / name, out / name)
        except Exception:
            if previous.exists():
                shutil.copy2(previous, out / 'daily.csv')
            if previous_manifest.exists():
                shutil.copy2(previous_manifest, out / 'dataset_manifest.json')
            else:
                (out / 'dataset_manifest.json').unlink(missing_ok=True)
            raise
    destination = out / 'backups' / datetime.now(timezone.utc).date().isoformat()
    backup(out, destination)
    return {'status': 'promoted', 'version': status['version'], 'average_precision': new['average_precision'],
            'backup_verified': verify_backup(destination),
            'caveat': 'New chronological test is not the fixed historical holdout; compare with caution'}


def next_run(hour, minute):
    """Вычисляет ближайшее время запуска ежедневной задачи в UTC."""
    now = datetime.now(timezone.utc)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def main():
    """Запускает одно обновление или цикл ежедневных обновлений по расписанию."""
    p = argparse.ArgumentParser()
    p.add_argument('--source', required=True)
    p.add_argument('--artifacts', default='artifacts')
    p.add_argument('--confirmed-labels')
    p.add_argument('--utc-time', default='23:59')
    p.add_argument('--once', action='store_true')
    a = p.parse_args()
    h, m = map(int, a.utc_time.split(':'))
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError('utc-time HH:MM')
    if a.once:
        print(json.dumps(run_once(a.source, a.artifacts, a.confirmed_labels), ensure_ascii=False))
        return
    while True:
        target = next_run(h, m)
        while datetime.now(timezone.utc) < target:
            time.sleep(min(30, max(.1, (target - datetime.now(timezone.utc)).total_seconds())))
        try:
            result = run_once(a.source, a.artifacts, a.confirmed_labels)
            print(json.dumps({'at': datetime.now(timezone.utc).isoformat(), **result}, ensure_ascii=False), flush=True)
        except Exception as ex:
            print(json.dumps({'at': datetime.now(timezone.utc).isoformat(), 'error': str(ex)}, ensure_ascii=False),
                  flush=True)


if __name__ == '__main__':
    main()
