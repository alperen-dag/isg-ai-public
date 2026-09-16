"""Panel behavior, additive schema and security on disposable databases only."""
from contextlib import closing
from datetime import datetime
import importlib
import io
import os
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import patch

import isg_database as db


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(db, 'DB_PATH', Path(self.tmp.name) / 'panel.db')
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.password = 'PanelOnly!' + secrets.token_hex(12)
        self.env = patch.dict(os.environ, {'ISG_SECRET_KEY': secrets.token_urlsafe(48),
                                          'ISG_BOOTSTRAP_ADMIN_PASSWORD': self.password})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.web = importlib.import_module('web_app.web')
        self.web.veritabanini_hazirla()
        self.web.app.config['TESTING'] = True
        with closing(db.connect_database()) as c, c:
            self.admin_id = c.execute("SELECT id FROM kullanicilar WHERE kullanici_adi='admin'").fetchone()[0]
            c.execute("INSERT INTO kullanicilar(ad_soyad,kullanici_adi,sifre,rol,aktif) VALUES('Observer','observer','unused','Görüntüleyici',1)")
            self.viewer_id = c.execute("SELECT id FROM kullanicilar WHERE kullanici_adi='observer'").fetchone()[0]
            c.executemany('INSERT INTO kullanici_yetkileri(kullanici_id,modul) VALUES(?,?)',
                          [(self.viewer_id, key) for key,_ in self.web.MODULLER])
        self.client = self.authenticated(self.admin_id)

    def authenticated(self, user_id):
        client = self.web.app.test_client()
        with client.session_transaction() as session:
            session['kullanici_id'] = user_id
        client.get('/dashboard')
        return client

    def post(self, route, data=None, client=None):
        client = client or self.client
        with client.session_transaction() as session:
            token = session['_csrf_token']
        return client.post(route, data={'csrf_token': token, **(data or {})})

    def seed(self):
        with closing(db.connect_database()) as c, c:
            today = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            c.execute("INSERT INTO ihlaller(tarih,ihlal_turu,guven_skoru,durum,kamera_id,person_id,aciklama,fotograf_yolu) VALUES(?, 'Baret Yok', NULL,'Yeni',1,'track-a','<script>alert(1)</script>','')", (today,))
            first = c.execute('SELECT last_insert_rowid()').fetchone()[0]
            c.execute("INSERT INTO ihlaller(tarih,ihlal_turu,guven_skoru,durum,kamera_id,person_id,fotograf_yolu) VALUES('2020-01-01 10:00:00','Reflektif Yelek Yok',85,'Çözüldü',NULL,'track-b','')")
        return first

    def test_dashboard_empty_and_real_counts(self):
        response = self.client.get('/dashboard')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Son 7 günde ihlal kaydı bulunmuyor', response.get_data(as_text=True))
        self.seed()
        from flask import template_rendered
        contexts = []
        def collect(sender, template, context, **extra):
            contexts.append(context)
        with template_rendered.connected_to(collect, self.web.app):
            self.assertEqual(self.client.get('/dashboard').status_code, 200)
        self.assertEqual(contexts[0]['today_count'], 1)
        self.assertEqual(contexts[0]['open_count'], 1)
        self.assertEqual(contexts[0]['resolved'], 1)
        self.assertEqual(sum(day['count'] for day in contexts[0]['trend']), 1)

    def test_multi_camera_admin_and_secret_rejection(self):
        values = dict(kamera_adi='IP camera', kamera_index='0', yasak_bolge_yuzdesi='35',
                      source_type='rtsp', source_uri='rtsp://10.0.0.1/test',
                      credential_env='ISG_CAMERA_TEST', analysis_fps='5', analysis_enabled='1')
        self.assertEqual(self.post('/kameralar/ekle', values).status_code, 302)
        with closing(db.connect_database()) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM kameralar WHERE aktif=1').fetchone()[0], 2)
        self.assertEqual(self.post('/kameralar/1/durum').status_code, 302)
        with closing(db.connect_database()) as connection:
            self.assertEqual(connection.execute('SELECT aktif FROM kameralar WHERE id=1').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT aktif FROM kameralar WHERE id=2').fetchone()[0], 1)
        secret = 'secret123'
        values['source_uri'] = f'rtsp://admin:{secret}@10.0.0.1/test'
        response = self.post('/kameralar/2/duzenle', values)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(secret, response.get_data(as_text=True))
        self.assertNotIn(secret, self.client.get('/kameralar').get_data(as_text=True))
        with closing(db.connect_database()) as connection:
            self.assertNotIn(secret, str(connection.execute('SELECT * FROM audit_log').fetchall()))
        viewer = self.authenticated(self.viewer_id)
        self.assertEqual(self.post('/kameralar/ekle', values, viewer).status_code, 403)

    def test_camera_health_states_and_runtime_notice(self):
        from camera_health import CameraHealthReporter
        from ai_service.runtime_config import RuntimeConfig
        html = self.client.get('/kameralar').get_data(as_text=True)
        self.assertIn('Bağlantı durumu: Bilinmiyor', html)
        reporter = CameraHealthReporter(1, RuntimeConfig())
        try:
            reporter.frame_received()
            reporter.queue.join()
            html = self.client.get('/kameralar').get_data(as_text=True)
            self.assertIn('Bağlantı durumu: Çevrimiçi', html)
            self.assertIn('yeniden başlatma gerekmez', html)
            with closing(db.connect_database()) as c, c:
                c.execute('UPDATE camera_health SET last_frame_at=last_frame_at-20')
            self.assertIn('Bağlantı durumu: Çevrimdışı', self.client.get('/kameralar').get_data(as_text=True))
        finally:
            reporter.close()

    def test_zone_confidence_list_detail_and_legacy_zero_preserved(self):
        with closing(db.connect_database()) as c, c:
            keys = []
            for score in (0, None, 87.5):
                keys.append(c.execute("INSERT INTO ihlaller(tarih,ihlal_turu,guven_skoru,fotograf_yolu) VALUES('2026-09-08 12:00:00','Yasak Bolge Ihlali',?,'')", (score,)).lastrowid)
        for key, expected in zip(keys, ('%0.0', 'Mevcut değil', '%87.5')):
            self.assertIn(expected, self.client.get(f'/ihlal/{key}').get_data(as_text=True))
            self.assertIn(expected, self.client.get('/ihlaller').get_data(as_text=True))
        db.prepare_database()
        with closing(db.connect_database()) as c:
            self.assertEqual(c.execute('SELECT guven_skoru FROM ihlaller WHERE id=?',(keys[0],)).fetchone()[0], 0)

    def test_combined_filters_search_and_invalid_input(self):
        self.seed()
        response = self.client.get('/ihlaller', query_string={'kamera_id':'1','durum':'Yeni',
            'ihlal_turu':'Baret Yok','baslangic':datetime.now().strftime('%Y-%m-%d'), 'q':'track-a'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('track-a', response.get_data(as_text=True))
        self.assertNotIn('track-b', response.get_data(as_text=True))
        for query in ({'kamera_id':'1 OR 1=1'},{'durum':'unknown'},{'sayfa':'-1'},
                      {'baslangic':'2026-12-01','bitis':'2020-01-01'},{'q':'x'*121}):
            self.assertEqual(self.client.get('/ihlaller',query_string=query).status_code,400)
        self.assertIn('Gösterilecek ihlal yok', self.client.get('/ihlaller?q=nonexistent').get_data(as_text=True))

    def test_detail_null_escaping_and_legacy_routes(self):
        key = self.seed()
        html = self.client.get(f'/ihlal/{key}').get_data(as_text=True)
        self.assertIn('Mevcut değil', html)
        self.assertIn('track-a', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<script>alert(1)</script>', html)
        for route in ('/dashboard','/ihlaller','/kameralar','/kullanicilar','/raporlar','/islem-kayitlari'):
            self.assertEqual(self.client.get(route).status_code, 200, route)
        self.assertEqual(self.client.get('/ihlal/999999').status_code,404)
        self.assertEqual(self.client.get('/').status_code,302)

    def test_resolve_reopen_and_audit(self):
        key = self.seed()
        response = self.post(f'/durum-guncelle/{key}', {'durum':'Çözüldü','cozum_notu':'Baret teslim edildi'})
        self.assertEqual(response.status_code,302)
        with closing(db.connect_database()) as c:
            before = c.execute('SELECT durum,cozum_notu,cozen_kullanici_id,cozum_tarihi FROM ihlaller WHERE id=?',(key,)).fetchone()
            self.assertEqual(before[:3],('Çözüldü','Baret teslim edildi',self.admin_id))
            self.assertIsNotNone(before[3])
        self.post(f'/durum-guncelle/{key}', {'durum':'Çözüldü','cozum_notu':'changed'})
        with closing(db.connect_database()) as c:
            self.assertEqual(c.execute('SELECT durum,cozum_notu,cozen_kullanici_id,cozum_tarihi FROM ihlaller WHERE id=?',(key,)).fetchone(),before)
        self.post(f'/durum-guncelle/{key}', {'durum':'Yeni'})
        with closing(db.connect_database()) as c:
            self.assertEqual(c.execute('SELECT durum,cozum_tarihi FROM ihlaller WHERE id=?',(key,)).fetchone(),('Yeni',None))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM audit_log WHERE islem_turu='ihlal_durumu_degistirildi'").fetchone()[0],2)
        self.assertIn('Baret teslim edildi',self.client.get('/islem-kayitlari').get_data(as_text=True))

    def test_viewer_cannot_mutate_or_read_audit(self):
        key = self.seed()
        viewer = self.authenticated(self.viewer_id)
        for route,data in [(f'/durum-guncelle/{key}',{'durum':'Çözüldü'}),
                           (f'/ihlal/{key}/aciklama',{'aciklama':'unauthorized'}),
                           ('/kameralar/1/ppe',{'helmet':'1'})]:
            self.assertEqual(self.post(route,data,viewer).status_code,403)
        self.assertEqual(viewer.get('/islem-kayitlari').status_code,403)
        self.assertNotIn('İşlem Kayıtları',viewer.get('/dashboard').get_data(as_text=True))
        self.assertNotIn('Durumu güncelle',viewer.get(f'/ihlal/{key}').get_data(as_text=True))
        with closing(db.connect_database()) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM audit_log').fetchone()[0],0)

    def test_ppe_rules_preserve_threshold_and_reject_unsupported(self):
        with closing(db.connect_database()) as c, c:
            c.execute("INSERT INTO kamera_ppe_kurallari VALUES(1,'helmet',0,.7)")
        self.assertEqual(self.post('/kameralar/1/ppe',{'helmet':'1','vest':'1'}).status_code,302)
        with closing(db.connect_database()) as c:
            self.assertEqual(c.execute('SELECT equipment,aktif,confidence_threshold FROM kamera_ppe_kurallari ORDER BY equipment').fetchall(),[('helmet',1,.7),('vest',1,.5)])
        self.post('/kameralar/1/ppe',{'vest':'1'})
        for data in ({'ear_protection':'1'},{'safety_shoes':'1'},{'equipment':'ear_protection'}, {'helmet':'yes'}):
            self.assertEqual(self.post('/kameralar/1/ppe',data).status_code,400)
        with closing(db.connect_database()) as c:
            self.assertEqual(c.execute('SELECT equipment,aktif FROM kamera_ppe_kurallari ORDER BY equipment').fetchall(),[('helmet',0),('vest',1)])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM audit_log WHERE islem_turu='kamera_ppe_degistirildi'").fetchone()[0],2)
        self.assertEqual(self.post('/kameralar/999/ppe',{'helmet':'1'}).status_code,404)

    def test_csrf_auth_headers_and_login(self):
        key = self.seed()
        for route in (f'/durum-guncelle/{key}','/kameralar/1/ppe'):
            self.assertEqual(self.client.post(route,data={}).status_code,400)
        anonymous = self.web.app.test_client()
        for route in ('/ihlaller','/islem-kayitlari','/kameralar','/raporlar'):
            self.assertEqual(anonymous.get(route).status_code,302)
        anonymous.get('/login')
        with anonymous.session_transaction() as session:
            token = session['_csrf_token']
        self.assertEqual(anonymous.post('/login',data={'csrf_token':token,'kullanici_adi':'admin','sifre':self.password}).status_code,302)
        response = anonymous.get('/dashboard')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.headers['X-Frame-Options'],'DENY')
        self.assertEqual(response.headers['X-Content-Type-Options'],'nosniff')
        self.assertIn("default-src 'self'",response.headers['Content-Security-Policy'])
        self.assertTrue(self.web.app.config['SESSION_COOKIE_HTTPONLY'])
        self.assertEqual(self.web.app.config['SESSION_COOKIE_SAMESITE'],'Lax')

    def test_exports_filter_and_formula_protection(self):
        key = self.seed()
        with closing(db.connect_database()) as c, c:
            c.execute('UPDATE ihlaller SET aciklama=? WHERE id=?',('=HYPERLINK("bad")',key))
        response = self.client.get('/raporlar/csv?kamera_id=1')
        self.assertEqual(response.status_code,200)
        self.assertIn("'=HYPERLINK",response.get_data(as_text=True))
        self.assertNotIn('Reflektif Yelek Yok',response.get_data(as_text=True))
        for kind,signature in [('xlsx',b'PK'),('pdf',b'%PDF')]:
            response = self.client.get(f'/raporlar/{kind}?kamera_id=1')
            self.assertEqual(response.status_code,200,kind)
            self.assertTrue(response.data.startswith(signature),kind)

    def test_additive_migration_preserves_existing_values(self):
        self.seed()
        with closing(db.connect_database()) as c:
            before = c.execute('SELECT * FROM ihlaller ORDER BY id').fetchall()
        db.prepare_database()
        db.prepare_database()
        with closing(db.connect_database()) as c:
            self.assertEqual(c.execute('SELECT * FROM ihlaller ORDER BY id').fetchall(),before)
            self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_self_admin_guards_and_revoked_permission(self):
        self.assertEqual(self.post(f'/kullanicilar/{self.admin_id}/durum').status_code,400)
        self.assertEqual(self.post(f'/kullanicilar/{self.admin_id}/rol',{'rol':'Görüntüleyici'}).status_code,400)
        viewer = self.authenticated(self.viewer_id)
        with closing(db.connect_database()) as c, c:
            c.execute("DELETE FROM kullanici_yetkileri WHERE kullanici_id=? AND modul='ihlaller'",(self.viewer_id,))
        self.assertEqual(viewer.get('/ihlaller').status_code,403)
        self.assertNotIn('href="/ihlaller"',viewer.get('/dashboard').get_data(as_text=True))

    def test_optional_email_creation(self):
        response = self.post('/kullanicilar/ekle',{'ad_soyad':'Test User','kullanici_adi':'testuser',
            'sifre':'StrongTest!123','rol':'Görüntüleyici','eposta':'user@example.test'})
        self.assertEqual(response.status_code,302)
        with closing(db.connect_database()) as c:
            self.assertEqual(c.execute("SELECT eposta FROM kullanicilar WHERE kullanici_adi='testuser'").fetchone(),('user@example.test',))

    def test_rate_limit_remains_enforced(self):
        client = self.web.app.test_client()
        client.get('/login')
        with client.session_transaction() as session:
            token = session['_csrf_token']
        for _ in range(self.web.LOGIN_MAX_FAILURES):
            response = client.post('/login', data={'csrf_token':token,'kullanici_adi':'missing','sifre':'Wrong!123'})
            self.assertEqual(response.status_code,200)
        response = client.post('/login',data={'csrf_token':token,'kullanici_adi':'missing','sifre':'Wrong!123'})
        self.assertEqual(response.status_code,429)

    def test_pagination_preserves_filters(self):
        with closing(db.connect_database()) as c, c:
            c.executemany("INSERT INTO ihlaller(tarih,ihlal_turu,fotograf_yolu,kamera_id) VALUES('2026-09-08 00:00:00','Baret Yok','',1)", [()] * 27)
        response = self.client.get('/ihlaller?kamera_id=1&sayfa=2')
        text = response.get_data(as_text=True)
        self.assertEqual(text.count('class="track"'),2)
        self.assertIn('kamera_id=1',text)
        self.assertIn('Sayfa 2 / 2',text)

    def test_audit_escapes_changes_and_hides_secrets(self):
        with closing(db.connect_database()) as c, c:
            db.audit_kaydi_ekle(c,self.admin_id,'admin','test','ihlal',1,
                yeni_deger={'aciklama':'<script>bad</script>','sifre':'DoNotExpose','token':'HiddenToken'})
        html = self.client.get('/islem-kayitlari').get_data(as_text=True)
        self.assertIn('&lt;script&gt;bad&lt;/script&gt;',html)
        self.assertNotIn('DoNotExpose',html)
        self.assertNotIn('HiddenToken',html)


if __name__ == '__main__':
    unittest.main()
