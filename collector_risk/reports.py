"""Общие данные для экранных, PDF и XLSX отчетов."""
from __future__ import annotations
from datetime import date
from io import BytesIO
import json
from pathlib import Path
import zipfile
from xml.sax.saxutils import escape

REPORT_KINDS = {'management': 'Управленческая сводка', 'forecast': 'Прогнозы по объектам',
                'activity': 'Оповещения и решения', 'quality': 'Качество данных', 'audit': 'Журнал действий'}


def validate_period(start: str, end: str):
    """Проверяет формат и длину интервала локального отчета."""
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    if b < a or (b - a).days > 366:
        raise ValueError('Период должен быть от 1 до 367 дней')
    return start, end


def summary(store, metrics_path, start, end):
    """Собирает локальные журналы периода и фиксированные метрики исторического теста."""
    validate_period(start, end)
    data = store.report_rows(start, end)
    metrics = json.loads(Path(metrics_path).read_text(encoding='utf-8'))
    return {'period': {'from': start, 'to': end},
            'counts': {name: len(rows) for name, rows in data.items()},
            'model_metrics': {k: metrics.get(k) for k in ('data_end', 'test_rows', 'alert_precision',
                                                          'alert_recall', 'average_precision')},
            'sources': ['metrics.json (исторический тест)',
                        'decisions / notifications / tickets / audit (SQLite, период создания)'],
            'caveat': 'События ремонта и подтвержденные аварии не представлены; '
                      'метрики модели относятся к тревожным сигналам.',
            'rows': data, 'audit_chain_ok': store.verify_audit()}


def build_report(store, metrics_path, records, kind, start, end,
                 object_id=None, system='', status='', min_score=None):
    """Создает один фильтрованный набор строк для экрана, Excel и PDF."""
    if kind not in REPORT_KINDS:
        raise ValueError('Неизвестный вид отчета')
    validate_period(start, end)
    oid = int(object_id) if object_id not in (None, '') else None
    if oid is not None and oid <= 0:
        raise ValueError('ID объекта должен быть положительным')
    threshold = float(min_score) / 100 if min_score not in (None, '') else None
    if threshold is not None and not 0 <= threshold <= 1:
        raise ValueError('Порог прогноза: 0–100%')
    source = summary(store, metrics_path, start, end)
    data = source['rows']
    metric = source['model_metrics']
    forecast = [r for r in records if (oid is None or int(r['object_id']) == oid)
                and (not system or r['category'] == system) and (not status or r['status'] == status)
                and (threshold is None or isinstance(r['score'], (int, float)) and r['score'] >= threshold)]
    applied = {'object_id': oid}
    if kind in ('forecast', 'quality', 'activity'):
        applied['system'] = system
    if kind in ('forecast', 'quality'):
        applied['status'] = status
    if kind == 'forecast':
        applied['min_score_percent'] = min_score
    base = {'kind': kind, 'title': REPORT_KINDS[kind],
            'period': source['period'], 'filters': applied,
            'audit_chain_ok': source['audit_chain_ok'], 'columns': [], 'rows': [], 'summary': {}, 'notes': []}

    def local(rows):
        """Фильтрует локальные журналы по ID объекта, если он указан."""
        return [r for r in rows if oid is None or r.get('object_id') == oid]

    if kind == 'management':
        decisions = local(data['decisions'])
        alerts = local(data['notifications'])
        tickets = local(data['tickets'])
        base['summary'] = {'Решения за период': len(decisions), 'Оповещения за период': len(alerts),
                           'Локальные заявки за период': len(tickets)}
        base['columns'] = ['Показатель', 'Значение', 'Источник']
        base['rows'] = [['Решения', len(decisions), 'Журнал решений'], ['Оповещения', len(alerts), 'Журнал оповещений'],
                        ['Локальные заявки', len(tickets), 'Черновики заявок']]
        base['rows'] += [[key, metric.get(key), 'metrics.json — исторический тест'] for key in
                         ('data_end', 'test_rows', 'alert_precision', 'alert_recall', 'average_precision')]
        base['notes'] = ['Даты ограничивают локальные записи. Показатели модели относятся к '
                         'фиксированному историческому тесту, а не к выбранному периоду.', source['caveat']]
    elif kind == 'forecast':
        base['columns'] = ['Дата источника', 'Объект ID', 'Объект', 'Система', 'Вероятность', 'Статус',
                           'Сигналы за 7 дней', 'Действует с', 'Действует до']
        base['rows'] = [[r['date'], r['object_id'], r['object_name'], r['category_ru'],
                         r['score'] if isinstance(r['score'], (int, float)) else '—',
                         r['status'], r['alarms_7d'], r['valid_from'], r['valid_to']] for r in forecast]
        base['summary'] = {'Пар объект/система': len(forecast), 'Объектов': len({r['object_id'] for r in forecast})}
        base['notes'] = ['Это один исторический снимок. Даты отчета не применяются к нему '
                         'и не создают прогноз за прошлый период.']
    elif kind == 'quality':
        base['columns'] = ['Дата источника', 'Объект ID', 'Объект', 'Система', 'Статус данных', 'Записей за 7 дней',
                           'Тревожных записей за 7 дней']
        gaps = [r for r in forecast if r['status'] in ('Нет свежих данных', 'Мало данных')]
        base['rows'] = [[r['date'], r['object_id'], r['object_name'],
                         r['category_ru'], r['status'], r['readings_7d'], r['alarms_7d']] for r in gaps]
        base['summary'] = {'Пар с нехваткой данных': len(gaps),
                           'Без свежих данных': sum(r['status'] == 'Нет свежих данных' for r in gaps)}
        base['notes'] = ['Нет свежих данных — отдельный повод проверить поступление телеметрии; '
                         'причина недоступности не установлена. Период не применяется к снимку.']
    elif kind == 'activity':
        rows = []
        for r in local(data['decisions']):
            if system and r['category'] != system:
                continue
            rows.append([r['created_at'], r['object_id'], r['category'], 'Решение', r['decision'], r['reason']])
        for r in local(data['notifications']):
            if system and r['category'] != system:
                continue
            rows.append([r['created_at'], r['object_id'], r['category'], 'Оповещение', r['priority'], r['title']])
        if not system:
            for r in local(data['tickets']):
                rows.append([r['created_at'], r['object_id'], '-', 'Локальная заявка', r['status'], r['reason']])
        rows.sort(key=lambda x: x[0], reverse=True)
        base['columns'] = ['Время UTC', 'Объект ID', 'Система', 'Тип записи', 'Результат', 'Основание']
        base['rows'] = rows
        base['summary'] = {'Записей': len(rows)}
        base['notes'] = ['При фильтре системы локальные заявки не выводятся: у них нет согласованного кода системы. '
                         'Статус и вероятность прогноза здесь не применяются.']
    else:
        audit = [r for r in data['audit'] if oid is None or r['object_id'] == oid]
        base['columns'] = ['Время UTC', 'Пользователь', 'Операция', 'Объект ID', 'Результат', 'Деталь']
        base['rows'] = [[r['at'], r['user_id'], r['action'], r['object_id'], r['result'], r['detail']] for r in audit]
        base['summary'] = {'Записей аудита': len(audit), 'Цепочка целая': 'Да' if source['audit_chain_ok'] else 'Нет'}
        base['notes'] = ['Система, статус и порог прогноза не применяются к аудиту. '
                         'Локальная цепочка хешей не заменяет внешнее неизменяемое хранилище.']
    return base


def _column(index):
    """Преобразует номер столбца в буквенный адрес Excel."""
    label = ''
    index += 1
    while index:
        index, n = divmod(index - 1, 26)
        label = chr(65 + n) + label
    return label


def xlsx(report):
    """Упаковывает строки отчета и параметры в минимальный XLSX без внешнего сервиса."""
    data = [report['columns']] + report['rows']
    context = [['Название', report['title']], ['Дата с', report['period']['from']], ['Дата по', report['period']['to']]]
    context += [[k, v] for k, v in report['summary'].items()]
    context += [['Фильтр ' + k, v] for k, v in report['filters'].items()]
    context += [['Примечание', note] for note in report['notes']]
    sheets = [('Данные', data), ('Параметры и методика', context)]

    def sheet_xml(rows):
        """Сериализует строки листа в XML со строковыми и числовыми ячейками."""
        out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
               '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
               '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
               'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>',
               '<cols><col min="1" max="20" width="24" customWidth="1"/></cols><sheetData>']
        for i, row in enumerate(rows, 1):
            out.append(f'<row r="{i}">')
            for j, value in enumerate(row):
                if value is None:
                    value = ''
                ref = f'{_column(j)}{i}'
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    out.append(f'<c r="{ref}"><v>{value}</v></c>')
                else:
                    out.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>')
            out.append('</row>')
        out.append('</sheetData></worksheet>')
        return ''.join(out)

    output = BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml',
                   '''<?xml version="1.0" encoding="UTF-8"?><Types 
                   xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" 
                   ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" 
                   ContentType="application/xml"/><Override PartName="/xl/workbook.xml" 
                   ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override 
                   PartName="/xl/worksheets/sheet1.xml" 
                   ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override 
                   PartName="/xl/worksheets/sheet2.xml" 
                   ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>''')
        z.writestr('_rels/.rels',
                   '''<?xml version="1.0" encoding="UTF-8"?><Relationships 
                   xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship 
                   Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" 
                   Target="xl/workbook.xml"/></Relationships>''')
        z.writestr('xl/workbook.xml',
                   '''<?xml version="1.0" encoding="UTF-8"?><workbook 
                   xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" 
                   xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet 
                   name="Данные" sheetId="1" r:id="rId1"/><sheet name="Параметры и методика" 
                   sheetId="2" r:id="rId2"/></sheets></workbook>''')
        z.writestr('xl/_rels/workbook.xml.rels',
                   '''<?xml version="1.0" encoding="UTF-8"?><Relationships 
                   xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship 
                   Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" 
                   Target="worksheets/sheet1.xml"/><Relationship Id="rId2" 
                   Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" 
                   Target="worksheets/sheet2.xml"/></Relationships>''')
        for index, (_, rows) in enumerate(sheets, 1):
            z.writestr(f'xl/worksheets/sheet{index}.xml', sheet_xml(rows))
    return output.getvalue()


def pdf(report):
    """Печатает тот же срез в PDF с кириллическим шрифтом и переносом страниц."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, LongTable, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    fonts = Path(__file__).parent / 'fonts'
    pdfmetrics.registerFont(TTFont('DejaVu', str(fonts / 'DejaVuSans.ttf')))
    pdfmetrics.registerFont(TTFont('DejaVu-Bold', str(fonts / 'DejaVuSans-Bold.ttf')))
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='Ru', fontName='DejaVu', fontSize=9, leading=13))
    styles.add(ParagraphStyle(name='RuTitle', fontName='DejaVu-Bold', fontSize=16, leading=22))
    many = len(report['columns']) > 5
    size = landscape(A4) if many else A4
    out = BytesIO()
    doc = SimpleDocTemplate(out, pagesize=size, rightMargin=34, leftMargin=34, topMargin=38, bottomMargin=38)
    elements = [Paragraph(escape(report['title']) + ' · историческая демонстрация', styles['RuTitle']),
                Spacer(1, 12),
                Paragraph(f"Период: {report['period']['from']} — {report['period']['to']}", styles['Ru'])]
    active = ', '.join(f'{key}: {value}' for key, value in report['filters'].items() if value not in ('', None))
    elements.append(Paragraph(escape('Фильтры: ' + (active or 'не заданы')), styles['Ru']))
    for key, value in report['summary'].items():
        elements.append(Paragraph(escape(f'{key}: {value}'), styles['Ru']))
    for note in report['notes']:
        elements.extend([Spacer(1, 6), Paragraph(escape(note), styles['Ru'])])
    elements.append(Spacer(1, 12))
    small = ParagraphStyle(name='RuSmall', fontName='DejaVu', fontSize=7, leading=10)
    width = (size[0] - 68) / len(report['columns'])
    rows = [report['columns']] + report['rows']
    table = LongTable([[Paragraph(escape(str(x if x is not None else '')[:500]), small) for x in row] for row in rows],
                      colWidths=[width] * len(report['columns']), repeatRows=1)
    table.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#d9eeea')),
                               ('LINEBELOW', (0, 0), (-1, -1), .25, colors.lightgrey),
                               ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    elements.append(table)
    doc.build(elements)
    return out.getvalue()
