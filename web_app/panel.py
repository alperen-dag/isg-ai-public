"""Presentation queries and authorized panel routes; no AI runtime dependencies."""
from camera_health import load_health_views, health_view
from ai_service.runtime_config import load_runtime_config
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta
import json
import sqlite3

from flask import abort, flash, redirect, render_template, request, session, url_for

LABELS = {'Baret Yok': 'Baret Eksik', 'no_helmet': 'Baret Eksik',
          'Reflektif Yelek Yok': 'Yelek Eksik', 'no_vest': 'Yelek Eksik',
          'no_safety_vest': 'Yelek Eksik', 'Yasak Bolge Ihlali': 'Yasak Bölge İhlali',
          'forbidden_zone': 'Yasak Bölge İhlali'}


def register_panel(app, web):
    from web_app.camera_admin import register_camera_admin
    register_camera_admin(app, web)
    app.jinja_env.filters['violation_label'] = lambda value: LABELS.get(value, value or 'Belirtilmemiş')
    app.jinja_env.filters['status_label'] = lambda value: 'Açık' if value in ('Yeni', None, '') else value

    def rows(connection, sql, params=()):
        cursor = connection.execute(sql, params)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def cameras(connection):
        return rows(connection, 'SELECT * FROM kameralar ORDER BY id')

    def records(connection, where='', params=(), limit=25, offset=0):
        return rows(connection, '''SELECT * FROM (
            SELECT i.*, COALESCE(k.kamera_adi, 'Kamera bilgisi yok') AS kamera_adi
            FROM ihlaller i LEFT JOIN kameralar k ON k.id=i.kamera_id
        )''' + where + ' ORDER BY tarih DESC, id DESC LIMIT ? OFFSET ?', [*params, limit, offset])

    def page_number():
        try:
            page = int(request.args.get('sayfa', 1))
            if not 1 <= page <= 1000000:
                raise ValueError
            return page
        except ValueError:
            abort(400, description='Geçersiz sayfa numarası.')

    @app.context_processor
    def panel_context():
        return dict(can_edit_violations=('ihlaller' in session.get('yetkiler', [])
                                        and session.get('rol') != 'Görüntüleyici'))

    @web.yetki_gerekli('dashboard')
    def dashboard():
        today = datetime.now().date()
        first = today - timedelta(days=6)
        with closing(web.connect_database()) as connection:
            totals = connection.execute('''SELECT COUNT(*),
                SUM(CASE WHEN durum IS NULL OR durum IN ('Yeni','İnceleniyor','') THEN 1 ELSE 0 END),
                SUM(CASE WHEN durum='Çözüldü' THEN 1 ELSE 0 END)
                FROM ihlaller''').fetchone()
            today_types = Counter(dict(connection.execute(
                'SELECT ihlal_turu, COUNT(*) FROM ihlaller WHERE substr(tarih,1,10)=? GROUP BY ihlal_turu',
                (str(today),)).fetchall()))
            distribution = Counter()
            for kind, count in connection.execute('SELECT ihlal_turu,COUNT(*) FROM ihlaller GROUP BY ihlal_turu'):
                distribution[LABELS.get(kind, kind or 'Belirtilmemiş')] += count
            days = dict(connection.execute('''SELECT substr(tarih,1,10), COUNT(*) FROM ihlaller
                WHERE substr(tarih,1,10)>=? AND substr(tarih,1,10)<=? GROUP BY substr(tarih,1,10)''',
                (str(first), str(today))).fetchall())
            trend = [dict(date=str(first+timedelta(days=i)), label=(first+timedelta(days=i)).strftime('%d.%m'),
                          count=days.get(str(first+timedelta(days=i)), 0)) for i in range(7)]
            camera_summary = rows(connection, '''SELECT k.id,k.kamera_adi,k.aktif,COUNT(i.id) AS total,
                SUM(CASE WHEN substr(i.tarih,1,10)=? THEN 1 ELSE 0 END) AS today
                FROM kameralar k LEFT JOIN ihlaller i ON i.kamera_id=k.id GROUP BY k.id ORDER BY total DESC''', (str(today),))
            unknown = connection.execute('SELECT COUNT(*) FROM ihlaller WHERE kamera_id IS NULL').fetchone()[0]
            recent = records(connection, limit=8)
            active = connection.execute('SELECT COUNT(*) FROM kameralar WHERE aktif=1').fetchone()[0]
        by_label = Counter()
        for kind, count in today_types.items():
            by_label[LABELS.get(kind, kind)] += count
        return render_template('index.html', title='Dashboard', ihlaller=recent,
            today=str(today), total=totals[0], open_count=totals[1] or 0, resolved=totals[2] or 0,
            today_count=sum(today_types.values()), active_cameras=active, today_types=by_label,
            distribution=distribution.most_common(), trend=trend, trend_max=max(1, *(d['count'] for d in trend)),
            camera_summary=camera_summary, unknown_cameras=unknown)

    app.view_functions['ana_sayfa'] = dashboard

    @app.route('/ihlaller')
    @web.yetki_gerekli('ihlaller')
    def ihlal_listesi():
        page = page_number()
        with closing(web.connect_database()) as connection:
            kinds = web.ihlal_turlerini_getir(connection)
            filters, error = web.rapor_filtrelerini_dogrula(request.args, kinds)
            if error:
                abort(400, description=error)
            where, params = web.rapor_where_clause(filters)
            total = connection.execute('SELECT COUNT(*) FROM ihlaller'+where, params).fetchone()[0]
            items = records(connection, where, params, offset=(page-1)*25)
            camera_list = cameras(connection)
        query = dict(request.args)
        query.pop('sayfa', None)
        return render_template('ihlaller.html', title='İhlaller', ihlaller=items, filtreler=filters,
            ihlal_turleri=kinds, durumlar=web.IZINLI_IHLAL_DURUMLARI, kameralar=camera_list,
            page=page, pages=max(1, (total+24)//25), total=total, query=query)

    @web.yetki_gerekli('ihlaller')
    def detail(ihlal_id):
        with closing(web.connect_database()) as connection:
            items = records(connection, ' WHERE id=?', (ihlal_id,), limit=1)
        if not items:
            abort(404, description='İhlal bulunamadı.')
        return render_template('detay.html', title=f'İhlal #{ihlal_id}', ihlal=items[0], durumlar=web.IZINLI_IHLAL_DURUMLARI)

    app.view_functions['ihlal_detay'] = detail

    @web.yetki_gerekli('kameralar')
    def camera_page():
        with closing(web.connect_database()) as connection:
            camera_list = cameras(connection)
            runtime = load_runtime_config()
            health = load_health_views(connection, runtime.health_stale_seconds)
            for camera in camera_list:
                camera["health"] = health.get(camera["id"], health_view())
            rules = {(r[0], r[1]): bool(r[2]) for r in connection.execute(
                "SELECT kamera_id,equipment,aktif FROM kamera_ppe_kurallari WHERE equipment IN ('helmet','vest')")}
        return render_template('kameralar.html', title='Kameralar', kameralar=camera_list, rules=rules, runtime=runtime)

    app.view_functions['kameralar'] = camera_page

    @app.route('/kameralar/<int:kamera_id>/ppe', methods=['POST'])
    @web.yetki_gerekli('kameralar')
    @web.admin_gerekli
    def kamera_ppe(kamera_id):
        allowed = {'csrf_token', 'helmet', 'vest'}
        if set(request.form) - allowed or any(request.form.get(key, '0') not in ('0', '1') for key in ('helmet','vest')):
            abort(400, description='Yalnızca baret ve yelek kuralları destekleniyor.')
        with closing(web.connect_database()) as connection:
            with connection:
                connection.execute('BEGIN IMMEDIATE')
                if not connection.execute('SELECT 1 FROM kameralar WHERE id=?', (kamera_id,)).fetchone():
                    abort(404)
                before = dict(connection.execute("SELECT equipment,aktif FROM kamera_ppe_kurallari WHERE kamera_id=? AND equipment IN ('helmet','vest')", (kamera_id,)))
                after = {key: int(request.form.get(key, '0')) for key in ('helmet', 'vest')}
                for key, enabled in after.items():
                    connection.execute('''INSERT INTO kamera_ppe_kurallari(kamera_id,equipment,aktif) VALUES(?,?,?)
                        ON CONFLICT(kamera_id,equipment) DO UPDATE SET aktif=excluded.aktif''', (kamera_id,key,enabled))
                web.audit_kaydi_ekle(connection, 'kamera_ppe_degistirildi', 'kamera', kamera_id, before, after)
        flash('PPE kuralları kaydedildi. Çalışan AI servisi kuralları otomatik yeniler; yeniden başlatma gerekmez.', 'success')
        return redirect(url_for('kameralar'))

    @app.route('/islem-kayitlari')
    @web.admin_gerekli
    def islem_kayitlari():
        page = page_number()
        with closing(web.connect_database()) as connection:
            total = connection.execute('SELECT COUNT(*) FROM audit_log').fetchone()[0]
            logs = rows(connection, 'SELECT * FROM audit_log ORDER BY id DESC LIMIT 25 OFFSET ?', ((page-1)*25,))
        # Audit payloads stay escaped and are rendered as compact field/value pairs.
        # Never display credentials, even if a legacy audit payload contains them.
        safe_fields = {'durum','aciklama','cozum_notu','cozen_kullanici_id','cozum_tarihi',
                       'kamera_adi','kamera_index','aktif','yasak_bolge_orani','rol','yetkiler',
                       'eklenen','kaldirilan','ad_soyad','kullanici_adi','helmet','vest'}
        for log in logs:
            log['changes'] = []
            for field, label in [('eski_deger','Önce'), ('yeni_deger','Sonra')]:
                try:
                    payload = json.loads(log[field] or '{}')
                except (ValueError, TypeError):
                    payload = {}
                if isinstance(payload, dict):
                    log['changes'].extend((label, key.replace('_',' '), str(value)[:500])
                                          for key, value in payload.items() if key in safe_fields)
        return render_template('audit.html', title='İşlem Kayıtları', logs=logs,
                               page=page, pages=max(1,(total+24)//25), total=total)

    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(400)
    def panel_error(error):
        return render_template('error.html', title=f'İşlem tamamlanamadı · {error.code}', error=error), error.code
