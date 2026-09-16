"""Real Flask auth/rendering against an isolated SQLite database."""
from contextlib import closing
import importlib
import os
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import patch

import isg_database as db


class WebSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_patch = patch.object(db, 'DB_PATH', Path(cls.tmp.name) / 'web.db')
        cls.db_patch.start()
        cls.password = 'SmokeOnly!' + secrets.token_hex(16)
        cls.env_patch = patch.dict(os.environ, {
            'ISG_SECRET_KEY': secrets.token_urlsafe(48),
            'ISG_BOOTSTRAP_ADMIN_PASSWORD': cls.password,
        })
        cls.env_patch.start()
        cls.web = importlib.import_module('web_app.web')
        cls.web.veritabanini_hazirla()
        cls.web.app.config['TESTING'] = True
        with closing(db.connect_database()) as conn, conn:
            conn.execute("INSERT INTO ihlaller(tarih,ihlal_turu,guven_skoru,fotograf_yolu) VALUES('2026-09-08 00:00:00','Yasak Bolge Ihlali',90,'ihlaller/test.jpg')")
            conn.executemany("INSERT INTO ihlaller(tarih,ihlal_turu,guven_skoru,fotograf_yolu) VALUES('2026-09-08 00:00:00',?,90,'ihlaller/test.jpg')",
                             [('Baret Yok',), ('Reflektif Yelek Yok',)])
            conn.execute("INSERT INTO ihlaller(tarih,ihlal_turu,guven_skoru,fotograf_yolu) VALUES('2026-09-08 00:00:00','Yasak Bolge Ihlali',NULL,'ihlaller/test.jpg')")

    @classmethod
    def tearDownClass(cls):
        cls.env_patch.stop()
        cls.db_patch.stop()
        cls.tmp.cleanup()

    def test_login_and_protected_pages(self):
        client = self.web.app.test_client()
        self.assertEqual(client.get('/login').status_code, 200)
        self.assertEqual(client.get('/dashboard').status_code, 302)
        with client.session_transaction() as session:
            csrf = session['_csrf_token']
        response = client.post('/login', data={
            'kullanici_adi':'admin', 'sifre':self.password, 'csrf_token':csrf})
        self.assertEqual(response.status_code, 302)
        for route in ('/', '/dashboard', '/ihlal/1', '/kullanicilar', '/kameralar', '/raporlar'):
            with self.subTest(route=route):
                response = client.get(route, follow_redirects=True)
                self.assertEqual(response.status_code, 200)
                self.assertNotEqual(response.request.path, '/login')
        self.assertIn('Mevcut değil', client.get('/ihlal/4').get_data(as_text=True))
        dashboard = client.get('/dashboard').get_data(as_text=True)
        self.assertIn('Mevcut değil', dashboard)
        self.assertIn('Baret Yok', dashboard)
        self.assertIn('Reflektif Yelek Yok', dashboard)
        self.assertIn('İhlal Türü Sayısı', dashboard)
        self.assertIn('Baret Yok', client.get('/ihlal/2').get_data(as_text=True))
        self.assertEqual(client.get('/raporlar', query_string={'ihlal_turu':'Baret Yok'}).status_code, 200)

    def test_csrf_and_anonymous_access(self):
        client = self.web.app.test_client()
        self.assertEqual(client.post('/login', data={}).status_code, 400)
        for route in ('/dashboard','/ihlal/1','/kullanicilar','/kameralar','/raporlar'):
            with self.subTest(route=route):
                response = client.get(route)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.location.endswith('/login'))
