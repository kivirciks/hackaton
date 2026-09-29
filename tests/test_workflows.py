"""Интеграционные проверки прав, воспроизведения потока, заявок и выгрузок."""
import json
from datetime import datetime
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from collector_risk.server import create_server
from collector_risk.operations import quality, verify_backup, backup

ROOT=Path(__file__).resolve().parents[1]


class WorkflowTests(unittest.TestCase):
    """Проверяет интеграционные сценарии ролей, заявок, потока и отчетов."""
    def setUp(self):
        """Создает изолированную копию артефактов и HTTP-сервер для каждого теста."""
        self.tmp=tempfile.TemporaryDirectory();out=Path(self.tmp.name)
        for name in ('forecast.csv','daily.csv','metrics.json'):
            shutil.copy2(ROOT/'artifacts'/name,out/name)
        self.server=create_server(out,ROOT/'upload'/'справочник_объектов_диспетчер.csv',port=0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_address[1]}'

    def tearDown(self):
        """Останавливает тестовый сервер и удаляет временные файлы."""
        self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()

    def call(self,path,data=None,cookie='',csrf=''):
        """Выполняет запрос к тестовому API и возвращает код, тело и заголовки."""
        payload=json.dumps(data).encode() if data is not None else None
        req=Request(self.url+path,data=payload,headers={'Content-Type':'application/json','Cookie':cookie,'X-CSRF-Token':csrf})
        try:
            with urlopen(req) as r:return r.status,r.read(),r.headers
        except HTTPError as e:return e.code,e.read(),e.headers

    def login(self,user):
        """Получает сессионную cookie и CSRF-токен заданной учетной записи."""
        code,body,h=self.call('/api/login',{'user':user,'password':'demo'})
        self.assertEqual(code,200)
        return h['Set-Cookie'].split(';')[0],json.loads(body)['csrf']

    def test_rbac_tickets_and_reports(self):
        """Проверяет создание ролей, отказ 403, заявки и пять видов отчетов."""
        self.assertEqual(self.call('/api/forecasts')[0],401)
        self.assertEqual(self.call('/api/login',{'user':'auditor','password':'demo'})[0],401)
        dispatch,csrf=self.login('dispatcher')
        self.assertEqual(self.call('/api/roles',cookie=dispatch)[0],403)
        self.assertEqual(self.call('/api/roles',{'role_id':'auditor','title':'Аудитор','permissions':['reports:read']},dispatch,csrf)[0],403)
        self.assertEqual(self.call('/api/objects',{'parent_id':5773,'name':'test'},dispatch,csrf)[0],403)
        code,body,_=self.call('/api/forecasts',cookie=dispatch);self.assertEqual(code,200)
        row=json.loads(body)[0]
        self.assertIn('valid_from',row)
        self.assertIn('valid_to',row)
        self.assertEqual((datetime.fromisoformat(row['valid_to'])-datetime.fromisoformat(row['valid_from'])).total_seconds(),86400)
        draft={'object_id':row['object_id'],'equipment':'Датчик','reason':'Проверка',
               'priority':'высокий','due_at':'2026-10-01'}
        code,body,_=self.call('/api/tickets/drafts',draft,dispatch,csrf)
        self.assertEqual(code,201);ticket=json.loads(body)['id']
        self.assertFalse(json.loads(body)['external_sent'])
        self.assertEqual(self.call(f'/api/tickets/{ticket}/confirm',{'confirm':True},dispatch,csrf)[0],200)
        _,body,_=self.call(f'/api/tickets/{ticket}',cookie=dispatch)
        self.assertIn('ДЕМО-ЗАГЛУШКА',json.loads(body)['external_status'])
        self.assertEqual(self.call('/api/stream/control',{'command':'step'},dispatch,csrf)[0],403)
        self.assertEqual(self.call('/api/stream',cookie=dispatch)[0],403)
        self.assertEqual(self.call('/api/stream/events',cookie=dispatch)[0],403)
        # Диспетчер может читать реестр оборудования и дополнять локальные записи.
        self.assertEqual(self.call('/api/equipment?object_id='+str(row['object_id']),cookie=dispatch)[0],200)
        self.assertEqual(self.call('/api/reports.xlsx?from=2026-01-01&to=2026-12-31',cookie=dispatch)[0],200)
        self.assertEqual(self.call('/api/reports.pdf?from=2026-01-01&to=2026-12-31',cookie=dispatch)[0],200)
        for kind in ('management','forecast','activity','quality','audit'):
            code,body,_=self.call('/api/reports?kind='+kind+'&from=2026-01-01&to=2026-12-31',cookie=dispatch)
            self.assertEqual(code,200)
            self.assertEqual(json.loads(body)['kind'],kind)
        code,body,_=self.call('/api/reports?kind=forecast&from=2026-01-01&to=2026-12-31&object_id='+str(row['object_id']),cookie=dispatch)
        self.assertTrue(all(int(r[1])==row['object_id'] for r in json.loads(body)['rows']))
        admin,admin_csrf=self.login('admin')
        self.assertEqual(self.call('/api/roles',{'role_id':'bad','title':'Неверная роль','permissions':[['reports:read']]},admin,admin_csrf)[0],400)
        self.assertEqual(self.call('/api/roles',{'role_id':'bad','title':'Неверная роль','permissions':['roles:manage']},admin,admin_csrf)[0],400)
        self.assertEqual(self.call('/api/roles',{'role_id':'auditor','title':'Аудитор','permissions':['reports:read']},admin,admin_csrf)[0],200)
        self.assertEqual(self.call('/api/users',{'user_id':'auditor','role_id':'auditor','password':'demo'},admin,admin_csrf)[0],200)
        auditor,token=self.login('auditor')
        self.assertEqual(self.call('/api/forecasts',cookie=auditor)[0],403)
        self.assertEqual(self.call('/api/audit',cookie=auditor)[0],403)
        code,body,_=self.call('/api/reports?kind=audit&from=2026-01-01&to=2026-12-31',cookie=auditor)
        self.assertEqual(code,200);self.assertTrue(json.loads(body)['audit_chain_ok'])
        # Право отзывается в уже открытой сессии, поскольку роли читаются из БД.
        self.assertEqual(self.call('/api/roles',{'role_id':'auditor','title':'Аудитор','permissions':[]},admin,admin_csrf)[0],200)
        self.assertEqual(self.call('/api/reports?kind=audit&from=2026-01-01&to=2026-12-31',cookie=auditor)[0],403)

    def test_replay_is_idempotent_and_backup_restores(self):
        """Проверяет воспроизведение событий без дублей и восстановление копии."""
        admin,csrf=self.login('admin')
        code,body,_=self.call('/api/stream/control',{'command':'step','count':10},admin,csrf)
        self.assertEqual(code,200);self.assertEqual(json.loads(body)['cursor'],10)
        code,events,_=self.call('/api/stream/events',cookie=admin)
        self.assertEqual(code,200);self.assertEqual(len(json.loads(events)),10)
        dest=Path(self.tmp.name)/'backup';backup(self.tmp.name,dest)
        self.assertTrue(verify_backup(dest))


if __name__=='__main__':unittest.main()
