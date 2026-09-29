"""Прогноз следующего тревожного дня; подтвержденные аварии не размечены."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score, brier_score_loss, precision_recall_fscore_support, roc_auc_score
from sklearn.metrics import precision_recall_curve

FEATURES = ['readings', 'channels', 'numeric_mean', 'numeric_max', 'alarms_3d', 'alarms_7d', 'alarms_30d',
            'readings_7d', 'coverage_7d', 'days_since_alarm', 'day_of_year_sin', 'day_of_year_cos', 'weekday',
            'fire', 'gas', 'access', 'thermal', 'utilities', 'device', 'flood', 'pump', 'power', 'ventilation']

EXTRA = ['alarm_rate_7d', 'alarm_rate_30obs', 'alarm_days_7obs', 'numeric_range',
         'readings_log', 'channels_log', 'alarms_log_30d', 'month', 'object_alarms',
         'object_readings', 'other_active_systems']

OTHER = ['fire', 'flood', 'access', 'gas', 'power', 'pump', 'thermal', 'ventilation']

EXTRA += [f'other_{c}_alarms' for c in OTHER]

FEATURES_2 = FEATURES + EXTRA + ['object_id']

CATEGORIES = ['fire', 'gas', 'access', 'thermal', 'utilities', 'device', 'flood', 'pump', 'power', 'ventilation']

DISPLAY = {'fire': 'Пожарная охрана', 'gas': 'Газовая охрана', 'access': 'Доступ',
           'thermal': 'Температура', 'utilities': 'Инженерные системы', 'device': 'Диагностика',
           'flood': 'Подтопление', 'pump': 'Насосы', 'power': 'Электропитание', 'ventilation': 'Вентиляция'}

ACTION = {'fire': 'Проверить дымовые и тепловые каналы, связаться с объектом; '
                  'при подтверждении действовать по регламенту пожарной безопасности.',
          'flood': 'Проверить датчик затопления и насосы, оценить объект на месте.',
          'access': 'Сверить режим охраны и доступ по СКУД, проверить соседние датчики и камеры.',
          'gas': 'Проверить газовые каналы и организовать контрольный замер по регламенту.',
          'power': 'Проверить питание и резервный источник, оформить осмотр.',
          'pump': 'Проверить режим насосов и показания затопления.',
          'device': 'Диагностика канала и сверка с графиком обслуживания.',
          'thermal': 'Проверить тренд температуры и соседние каналы.',
          'utilities': 'Проверить инженерные подсистемы объекта.',
          'ventilation': 'Проверить работу вентиляции и питание.'}


def make_features(daily):
    """Строит признаки до конца текущего дня и метку следующего наблюдаемого дня."""
    d = daily.copy()
    d['date'] = pd.to_datetime(d['date'])
    d = d.sort_values(['object_id', 'category', 'date']).reset_index(drop=True)
    grp = d.groupby(['object_id', 'category'], sort=False)

    def trailing(series, days, count=False):
        # Окна измеряются календарными днями: редкий канал не подменяет пропуск старой записью.
        """Считает скользящее окно по календарным датам, не заполняя пропуски ложными нулями."""
        window = pd.Series(series.to_numpy(), index=pd.DatetimeIndex(d.loc[series.index, 'date']))
        values = window.gt(0).astype('int8') if count else window
        return pd.Series(values.rolling(f'{days}D', min_periods=1).sum().to_numpy(), index=series.index)

    for window in (3, 7, 30):
        d[f'alarms_{window}d'] = grp['alarms'].transform(lambda s: trailing(s, window))
    d['readings_7d'] = grp['readings'].transform(lambda s: trailing(s, 7))
    # Цель размечаем только на наблюдаемых днях; пропуски не считаем отсутствием тревоги.
    d['coverage_7d'] = grp['readings'].transform(lambda s: trailing(s, 7, True))
    last = d['date'].where(d.alarms.gt(0))
    last = last.groupby([d.object_id, d.category]).ffill()
    d['days_since_alarm'] = (d.date - last).dt.days.fillna(365).clip(0, 365)
    d['day_of_year_sin'] = np.sin(2 * np.pi * d.date.dt.dayofyear / 365.25)
    d['day_of_year_cos'] = np.cos(2 * np.pi * d.date.dt.dayofyear / 365.25)
    d['weekday'] = d.date.dt.dayofweek
    for name in CATEGORIES:
        d[name] = d.category.eq(name).astype('int8')
    d['next_date'] = grp.date.shift(-1)
    d['target'] = (grp.alarms.shift(-1) > 0).astype('int8')
    # Новый тревожный день прогнозируем только при отсутствии активного сигнала сегодня.
    d['eligible'] = d.alarms.eq(0) & d.next_date.eq(d.date + pd.Timedelta(days=1))
    return d


def add_enhanced_features(d):
    """Добавляет контекст других систем объекта из данных не позже дня прогноза."""
    d = d.copy()
    group = d.groupby(['object_id', 'category'], sort=False)
    d['alarm_rate_7d'] = d.alarms_7d / (d.readings_7d + 1)
    prior_30 = group.readings.transform(lambda s: s.rolling(30, min_periods=1).sum())
    d['alarm_rate_30obs'] = d.alarms_30d / (prior_30 + 1)
    d['alarm_days_7obs'] = group.alarms.transform(lambda s: s.gt(0).rolling(7, min_periods=1).sum())
    d['numeric_range'] = d.numeric_max - d.numeric_mean
    d['readings_log'] = np.log1p(d.readings)
    d['channels_log'] = np.log1p(d.channels)
    d['alarms_log_30d'] = np.log1p(d.alarms_30d)
    d['month'] = d.date.dt.month
    same_day = d.groupby(['object_id', 'date'], sort=False)
    d['object_alarms'] = same_day.alarms.transform('sum') - d.alarms
    d['object_readings'] = same_day.readings.transform('sum')
    d['other_active_systems'] = same_day.alarms.transform(lambda s: s.gt(0).sum()) - d.alarms.gt(0)
    keys = pd.MultiIndex.from_frame(d[['object_id', 'date']])
    for category in OTHER:
        observations = d[d.category.eq(category)].set_index(['object_id', 'date']).alarms
        d[f'other_{category}_alarms'] = pd.Series(keys.map(observations).to_numpy(), index=d.index).fillna(0)
        d.loc[d.category.eq(category), f'other_{category}_alarms'] = 0
    return d


def train(daily_path, output, holdout_days=28, validation_days=28):
    """Обучает модель тревожного дня на прошлом, выбирает порог по валидации и тестирует позже."""
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    d = add_enhanced_features(make_features(pd.read_csv(daily_path)))
    labeled = d[d.eligible].copy()
    max_date = labeled.date.max()
    test_start = max_date - pd.Timedelta(days=holdout_days - 1)
    val_start = test_start - pd.Timedelta(days=validation_days)
    tr = labeled[labeled.date < val_start]
    va = labeled[(labeled.date >= val_start) & (labeled.date < test_start)]
    te = labeled[labeled.date >= test_start]
    if min(len(tr), len(va), len(te)) < 100 or min(tr.target.sum(), va.target.sum(), te.target.sum()) < 5:
        raise ValueError('Insufficient chronological positives for three-way validation; add history.')
    X = lambda frame, cols: frame[cols].replace([np.inf, -np.inf], np.nan).astype('float32')
    model = HistGradientBoostingClassifier(max_iter=180, max_leaf_nodes=12, min_samples_leaf=90,
                                           l2_regularization=5, learning_rate=.05, random_state=42)
    context_model = HistGradientBoostingClassifier(max_iter=180, max_leaf_nodes=12, min_samples_leaf=180,
                                                   l2_regularization=5, learning_rate=.05, random_state=42)
    model.fit(X(tr, FEATURES), tr.target)
    context_model.fit(X(tr, FEATURES_2), tr.target)

    def raw_score(frame):
        """Смешивает два ансамбля с фиксированными весами без обращения к будущим меткам."""
        return (.75 * model.predict_proba(X(frame, FEATURES))[:, 1]
                + .25 * context_model.predict_proba(X(frame, FEATURES_2))[:, 1])

    # Вариант модели и порог выбираем только на валидации; тест лежит позже по времени.
    from sklearn.isotonic import IsotonicRegression
    raw_val = raw_score(va)
    calibrator = IsotonicRegression(y_min=0, y_max=1, out_of_bounds='clip').fit(raw_val, va.target)
    prec_val, rec_val, candidates = precision_recall_curve(va.target, raw_val)
    # Подбираем порог по балансу precision и recall на валидации.
    best = int(np.argmax(np.minimum(prec_val[:-1], rec_val[:-1])))
    threshold = float(candidates[best])
    raw_test = raw_score(te)
    p_test = calibrator.predict(raw_test)
    chosen = raw_test >= threshold
    precision, recall, _, _ = precision_recall_fscore_support(te.target, chosen, average='binary', zero_division=0)
    base = te.alarms_7d.clip(0, 1).to_numpy()
    metrics = {
        'target': 'First alarm day within the next calendar day, among object/system days without an active alarm',
        'horizon': 'Next calendar day (up to 48h for an early-day observation; daily batch at 23:59 gives 24h)',
        'data_end': str(max_date.date()), 'train_end': str((val_start - pd.Timedelta(days=1)).date()),
        'validation_start': str(val_start.date()), 'test_start': str(test_start.date()),
        'train_rows': len(tr), 'validation_rows': len(va), 'test_rows': len(te),
        'train_positives': int(tr.target.sum()), 'validation_positives': int(va.target.sum()),
        'test_positives': int(te.target.sum()), 'prevalence': float(te.target.mean()),
        'average_precision': float(average_precision_score(te.target, raw_test)),
        'baseline_ap_7d_alarm': float(average_precision_score(te.target, base)),
        'roc_auc': float(roc_auc_score(te.target, p_test)) if te.target.nunique() > 1 else None,
        'brier': float(brier_score_loss(te.target, p_test)),
        'threshold_raw': threshold, 'threshold_calibrated': float(calibrator.predict([threshold])[0]),
        'selection_policy': 'Maximize min(precision, recall) on validation; blend 0.75 base + 0.25 context',
        'validation_precision': float(prec_val[best]), 'validation_recall': float(rec_val[best]),
        'alert_precision': float(precision), 'alert_recall': float(recall),
        'alerts': int(chosen.sum()), 'test_onsets': int(te.target.sum()),
        'by_category': {}}
    for category, g in te.assign(score=p_test, alert=chosen).groupby('category'):
        metrics['by_category'][category] = {'rows': len(g), 'onsets': int(g.target.sum()),
                                            'alerts': int(g.alert.sum()), 'precision': float(
                g.loc[g.alert, 'target'].mean()) if g.alert.sum() else None,
                                            'average_precision': float(
                                                average_precision_score(g.target, g.score)) if g.target.sum() else None}
    # Оцениваем последние пригодные наблюдения; без покрытия прогноз не показываем.
    latest = d.sort_values('date').groupby(['object_id', 'category'], as_index=False).tail(1).copy()
    latest['raw_score'] = raw_score(latest)
    latest['score'] = calibrator.predict(latest.raw_score)
    latest['confidence'] = np.where((latest.readings_7d >= 10) & (latest.coverage_7d >= 4), 'достаточное покрытие',
                                    'мало данных')
    latest['confidence'] = np.where(latest.date.lt(d.date.max()), 'нет свежих данных', latest.confidence)
    latest['action'] = latest.category.map(ACTION)
    latest['category_ru'] = latest.category.map(DISPLAY)
    latest['status'] = np.select([latest.confidence.eq('нет свежих данных'), latest.alarms.gt(0),
                                  latest.confidence.eq('мало данных'), latest.raw_score.ge(threshold)],
                                 ['Нет свежих данных', 'Активный сигнал', 'Мало данных', 'Проверить'],
                                 default='Наблюдение')
    # Модель обучалась на днях без тревог; на остальных днях скрываем непроверенную оценку.
    latest['score'] = latest['score'].where(latest.status.isin(['Проверить', 'Наблюдение'])).round(4)
    priority = {'Активный сигнал': 0, 'Проверить': 1, 'Наблюдение': 2, 'Мало данных': 3, 'Нет свежих данных': 4}
    latest['priority'] = latest.status.map(priority)
    latest.sort_values(['priority', 'score'], ascending=[True, False])[
        ['date', 'object_id', 'category', 'category_ru', 'score', 'status',
         'confidence', 'action', 'readings', 'alarms', 'readings_7d', 'alarms_7d', 'numeric_mean',
         'numeric_max']].to_csv(out / 'forecast.csv', index=False)
    joblib.dump({'model': model, 'context_model': context_model, 'calibrator': calibrator,
                 'features': FEATURES, 'context_features': FEATURES_2, 'threshold_raw': threshold},
                out / 'model.joblib')
    (out / 'metrics.json').write_text(json.dumps(metrics, ensure_ascii=False, indent=2))
    return metrics


def main():
    """Читает CLI-пути, запускает обучение и печатает измеренные метрики."""
    p = argparse.ArgumentParser()
    p.add_argument('--daily', default='artifacts/daily.csv')
    p.add_argument('--output', default='artifacts')
    a = p.parse_args()
    print(json.dumps(train(a.daily, a.output), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
