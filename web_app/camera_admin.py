"""Camera mutation handlers with existing role, CSRF and audit enforcement."""
from contextlib import closing
from datetime import datetime
import sqlite3
import os

from flask import request, redirect, url_for
from ai_service.camera_sources import CameraConfig


def register_camera_admin(app, web):
    def save(camera_id=None):
        basic, error = web.kamera_formunu_dogrula(request.form)
        if error:
            return error, 400
        try:
            extra = dict(source_type=request.form.get('source_type', 'usb'),
                source_uri=request.form.get('source_uri', '').strip(),
                credential_env=request.form.get('credential_env', '').strip(),
                analysis_enabled=int(request.form.get('analysis_enabled', '1')),
                analysis_fps=float(request.form.get('analysis_fps', os.getenv('DEFAULT_CAMERA_ANALYSIS_FPS', '5'))),
                worker_group=request.form.get('worker_group', 'default').strip())
            CameraConfig(id=camera_id or 0, **basic, **extra)
            if extra['analysis_enabled'] not in (0, 1) or len(extra['source_uri']) > 2048 or not extra['worker_group']:
                raise ValueError('SOURCE_INVALID')
        except (ValueError, TypeError):
            return 'Geçersiz kamera kaynağı veya analiz ayarı. RTSP kimlik bilgileri için ortam değişkeni kullanın.', 400
        values = {**basic, **extra, 'guncelleme_tarihi': datetime.now().isoformat(timespec='seconds')}
        with closing(web.connect_database()) as connection:
            try:
                with connection:
                    if camera_id is None:
                        values.update(aktif=1, olusturma_tarihi=values['guncelleme_tarihi'])
                        columns = ','.join(values)
                        camera_id = connection.execute(f'INSERT INTO kameralar ({columns}) VALUES ({",".join("?" for _ in values)})', tuple(values.values())).lastrowid
                    else:
                        if not connection.execute('SELECT 1 FROM kameralar WHERE id=?', (camera_id,)).fetchone():
                            return 'Kamera bulunamadı.', 404
                        connection.execute('UPDATE kameralar SET ' + ','.join(f'{key}=?' for key in values) + ' WHERE id=?', (*values.values(), camera_id))
                    audit = {key: value for key, value in values.items() if key not in ('source_uri', 'credential_env')}
                    web.audit_kaydi_ekle(connection, 'kamera_ayarlari_degistirildi', 'kamera', camera_id, yeni_deger=audit)
            except sqlite3.IntegrityError:
                return 'Kamera ayarları mevcut kısıtlarla çakışıyor.', 400
        return redirect(url_for('kameralar'))

    @web.yetki_gerekli('kameralar')
    @web.admin_gerekli
    def add():
        return save()

    @web.yetki_gerekli('kameralar')
    @web.admin_gerekli
    def edit(kamera_id):
        return save(kamera_id)

    @web.yetki_gerekli('kameralar')
    @web.admin_gerekli
    def toggle(kamera_id):
        with closing(web.connect_database()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT aktif FROM kameralar WHERE id=?', (kamera_id,)).fetchone()
            if row is None:
                return 'Kamera bulunamadı.', 404
            connection.execute('UPDATE kameralar SET aktif=?,guncelleme_tarihi=? WHERE id=?',
                (1-row[0], datetime.now().isoformat(timespec='seconds'), kamera_id))
            web.audit_kaydi_ekle(connection, 'kamera_aktifligi_degistirildi', 'kamera', kamera_id,
                eski_deger={'aktif': bool(row[0])}, yeni_deger={'aktif': not row[0]})
        return redirect(url_for('kameralar'))

    app.view_functions.update(kamera_ekle=add, kamera_duzenle=edit, kamera_durum=toggle)
