"""Подготовка дневной телеметрии без использования будущих наблюдений."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import time
import pandas as pd
from .archive import ArchiveCSV

CATEGORIES = {
    'Пожарная охрана': 'fire', 'Газовая охрана': 'gas',
    'Охранная подсистема': 'access', 'Температурная подсистема': 'thermal',
    'Диспетчерский контроль': 'utilities', 'Диагностическая подсистема': 'device',
}
TYPE_CATEGORY = {
    'Датчик затопления': 'flood', 'Состояние насоса': 'pump',
    'Состояние фазы': 'power', 'Состояние вентилятора': 'ventilation',
    'ИБП': 'power', 'Состояние охраны': 'access',
}
COLS = ['ид_события', 'ид_канала_данных', 'дата', 'время', 'тревожное', 'значение_датчика']
KEY = ['date', 'object_id', 'category']


def aggregate(paths, channels_path, cutoff=None, chunk_size=250_000):
    """Читает CSV/7z порциями, связывает канал по точному ID и считает дневные ряды."""
    channels = pd.read_csv(channels_path, usecols=['ид_канала_данных', 'ид_объект', 'тип_инж_системы', 'тип_датчика'])
    channels = channels.drop_duplicates('ид_канала_данных').set_index('ид_канала_данных')
    channels['category'] = channels['тип_инж_системы'].map(CATEGORIES)
    channels['category'] = channels['тип_датчика'].map(TYPE_CATEGORY).fillna(channels['category'])
    all_parts = []
    stats = {'rows': 0, 'mapped_rows': 0, 'alarm_rows': 0, 'files': []}
    for path in paths:
        tic = time.time()
        parts = []
        if str(path).endswith('.7z'):
            stream = ArchiveCSV(path)
        else:
            stream = open(path, 'rb')
        with stream:
            chunks = pd.read_csv(stream, chunksize=chunk_size, dtype={'ид_канала_данных': 'string', 'дата': 'string',
                                                                      'тревожное': 'string'}, usecols=COLS,
                                 on_bad_lines='skip', low_memory=False)
            for chunk in chunks:
                stats['rows'] += len(chunk)
                # В некоторых архивах заголовки CSV повторяются внутри данных.
                chunk['ид_канала_данных'] = pd.to_numeric(chunk['ид_канала_данных'], errors='coerce')
                chunk = chunk[chunk['ид_канала_данных'].notna() & chunk['дата'].str.fullmatch(r'\d{4}-\d{2}-\d{2}',
                                                                                              na=False)]
                if cutoff:
                    chunk = chunk[chunk['дата'] <= cutoff]
                if chunk.empty:
                    continue
                # Соединяем по точному ID: названия датчиков повторяются у разных объектов.
                meta = channels.reindex(chunk['ид_канала_данных'].to_numpy())
                mask = meta['ид_объект'].notna().to_numpy()
                stats['mapped_rows'] += int(mask.sum())
                if not mask.any():
                    continue
                d = chunk.loc[mask, ['дата', 'тревожное', 'значение_датчика']].reset_index(drop=True)
                m = meta.iloc[mask.nonzero()[0]].reset_index(drop=True)
                d['date'] = d['дата'].astype(str)
                d['object_id'] = m['ид_объект'].astype('int32')
                d['category'] = m['category']
                d['alarm'] = d['тревожное'].str.lower().isin(['t', 'true', '1']).astype('int8')
                # Строковые значения содержат состояния и даты; числовую статистику считаем только по числам.
                d['numeric'] = pd.to_numeric(d['значение_датчика'], errors='coerce')
                d['numeric'] = d['numeric'].where(d['numeric'].between(-100, 1000))
                d['channel_id'] = chunk.loc[mask, 'ид_канала_данных'].to_numpy()
                stats['alarm_rows'] += int(d['alarm'].sum())
                g = d.groupby(KEY, sort=False, observed=True).agg(
                    readings=('alarm', 'size'), alarms=('alarm', 'sum'),
                    channels=('channel_id', 'nunique'), numeric_sum=('numeric', 'sum'),
                    numeric_n=('numeric', 'count'), numeric_max=('numeric', 'max'))
                parts.append(g)
        if parts:
            year = pd.concat(parts).groupby(level=KEY).agg({
                'readings': 'sum', 'alarms': 'sum', 'channels': 'max',
                'numeric_sum': 'sum', 'numeric_n': 'sum', 'numeric_max': 'max'})
            all_parts.append(year)
        stats['files'].append({'name': Path(path).name, 'seconds': round(time.time() - tic, 1)})
    if not all_parts:
        raise ValueError('No rows matched the channel directory')
    daily = pd.concat(all_parts).groupby(level=KEY).agg({
        'readings': 'sum', 'alarms': 'sum', 'channels': 'max',
        'numeric_sum': 'sum', 'numeric_n': 'sum', 'numeric_max': 'max'}).reset_index()
    daily['numeric_mean'] = daily['numeric_sum'] / daily['numeric_n'].replace(0, float('nan'))
    return daily.sort_values(KEY).reset_index(drop=True), stats


def main():
    """Выбирает входные архивы и сохраняет дневные данные с отчетом качества."""
    p = argparse.ArgumentParser(description='Bounded-memory archive aggregation')
    p.add_argument('--input', required=True, help='Directory of CSV and 7z journals')
    p.add_argument('--output', default='artifacts')
    p.add_argument('--cutoff', default=None, help='Last observed date, YYYY-MM-DD')
    p.add_argument('--years', nargs='*', default=None)
    a = p.parse_args()
    root = Path(a.input)
    out = Path(a.output)
    out.mkdir(parents=True, exist_ok=True)
    paths = sorted(root.glob('ext-journal-*.7z'))
    if a.years:
        paths = [x for x in paths if x.stem.rsplit('-', 1)[-1] in a.years]
    if not paths:
        paths = [root / 'журнал_событий_пример.csv']
    daily, stats = aggregate(paths, root / 'справочник_каналов_датчиков.csv', a.cutoff)
    daily.to_csv(out / 'daily.csv', index=False)
    stats.update({'daily_rows': len(daily), 'date_min': daily.date.min(), 'date_max': daily.date.max(),
                  'objects': int(daily.object_id.nunique()), 'categories': daily.category.value_counts().to_dict()})
    (out / 'data_quality.json').write_text(json.dumps(stats, ensure_ascii=False, indent=2))
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
