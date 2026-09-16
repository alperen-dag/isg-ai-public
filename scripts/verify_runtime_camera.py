"""Controlled real service smoke: existing camera 0, reversible helmet toggle, JSON evidence."""
from contextlib import closing
import json
import os
from pathlib import Path
import secrets
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('ISG_SECRET_KEY', secrets.token_urlsafe(48))
import isg_database as db
from ai_service import main
from camera_health import load_health_views


def run():
    report = {'frames': 0, 'person_frames': 0, 'rule_observations': [], 'health': [], 'zone_records': []}
    with closing(db.connect_database()) as conn:
        cameras = conn.execute('SELECT * FROM kameralar ORDER BY id').fetchall()
        rules = conn.execute('SELECT * FROM kamera_ppe_kurallari ORDER BY kamera_id,equipment').fetchall()
        selected = main.aktif_kamera_ayarini_getir(conn)
        assert selected['kamera_index'] == 0, 'Active camera must already be camera 0'
        cid = selected['id']
        helmet = next(r for r in rules if r[0] == cid and r[1] == 'helmet')
        old = conn.execute('SELECT * FROM ihlaller ORDER BY id').fetchall()
        max_id = max((r[0] for r in old), default=0)
    report['initial_camera_settings'] = cameras
    report['initial_ppe_settings'] = rules
    from web_app.web import app
    with closing(db.connect_database()) as conn:
        admin = conn.execute("SELECT id FROM kullanicilar WHERE rol='Admin' AND aktif=1 LIMIT 1").fetchone()
    client = app.test_client() if admin else None
    if client:
        with client.session_transaction() as session: session['kullanici_id'] = admin[0]
    changed = restored = False
    started = None
    previous = None
    original = main.PPEPipeline.evaluate

    def evaluate(pipeline, frame, people, configs):
        nonlocal started, previous, changed, restored
        now = time.monotonic()
        if started is None: started = now
        elapsed = now - started
        report['frames'] += 1
        report['person_frames'] += bool(people)
        signature = sorted((sorted(c.required), c.confidence_threshold) for c in configs)
        if signature != previous:
            report['rule_observations'].append({'seconds': round(elapsed, 2), 'rules': signature})
            previous = signature
            print('RULE_APPLIED', report['rule_observations'][-1], flush=True)
        if elapsed >= 5 and not changed:
            with closing(db.connect_database()) as conn, conn:
                conn.execute('UPDATE kamera_ppe_kurallari SET aktif=? WHERE kamera_id=? AND equipment=?', (1-helmet[2], cid, 'helmet'))
            changed = True
            report['toggle_seconds'] = round(elapsed, 2)
        if elapsed >= 15 and not restored:
            with closing(db.connect_database()) as conn, conn:
                conn.execute('UPDATE kamera_ppe_kurallari SET aktif=? WHERE kamera_id=? AND equipment=?', (helmet[2], cid, 'helmet'))
            restored = True
            report['restore_seconds'] = round(elapsed, 2)
        if not report['health'] or elapsed - report['health'][-1]['seconds'] >= 5:
            with closing(db.connect_database()) as conn:
                health = load_health_views(conn).get(cid)
            report['health'].append({'seconds': round(elapsed,2), 'view':health})
            if client:
                response = client.get('/kameralar')
                report['health'][-1]['panel_online'] = response.status_code == 200 and 'Bağlantı durumu: Çevrimiçi' in response.get_data(as_text=True)
            print('HEALTH', health, flush=True)
        return original(pipeline, frame, people, configs)

    def stop(_):
        return ord('q') if started is not None and time.monotonic()-started >= 40 else 0

    try:
        with patch.object(main.PPEPipeline, 'evaluate', evaluate), patch.object(main.cv2, 'imshow'), patch.object(main.cv2, 'waitKey', stop):
            main.servisi_calistir()
    except Exception as exc:
        report['error_type'] = type(exc).__name__
        raise
    finally:
        with closing(db.connect_database()) as conn, conn:
            conn.execute('UPDATE kamera_ppe_kurallari SET aktif=? WHERE kamera_id=? AND equipment=?', (helmet[2],cid,'helmet'))
        with closing(db.connect_database()) as conn:
            report['settings_restored'] = (cameras == conn.execute('SELECT * FROM kameralar ORDER BY id').fetchall() and rules == conn.execute('SELECT * FROM kamera_ppe_kurallari ORDER BY kamera_id,equipment').fetchall())
            report['old_records_unchanged'] = old == conn.execute('SELECT * FROM ihlaller WHERE id<=? ORDER BY id',(max_id,)).fetchall()
            report['integrity'] = conn.execute('PRAGMA integrity_check').fetchone()[0]
            report['foreign_key_errors'] = conn.execute('PRAGMA foreign_key_check').fetchall()
            report['final_health'] = load_health_views(conn).get(cid)
            report['zone_records'] = conn.execute("SELECT id,guven_skoru FROM ihlaller WHERE id>? AND ihlal_turu='Yasak Bolge Ihlali'",(max_id,)).fetchall()
        from web_app.web import app
        with closing(db.connect_database()) as conn:
            admin = conn.execute("SELECT id FROM kullanicilar WHERE rol='Admin' AND aktif=1 LIMIT 1").fetchone()
        if admin:
            client = app.test_client()
            with client.session_transaction() as session: session['kullanici_id'] = admin[0]
            response = client.get('/kameralar')
            report['camera_page_status'] = response.status_code
            report['camera_page_offline'] = 'Çevrimdışı' in response.get_data(as_text=True)
            report['zone_web_checks'] = []
            for key, confidence in report['zone_records']:
                expected = 'Mevcut değil' if confidence is None else f'%{confidence:.1f}'
                listing = client.get('/ihlaller', query_string={'ihlal_turu':'Yasak Bolge Ihlali'}).get_data(as_text=True)
                detail = client.get(f'/ihlal/{key}').get_data(as_text=True)
                report['zone_web_checks'].append({'id':key,'confidence':confidence,'detail':expected in detail,'list':expected in listing})
        report['seconds'] = round(time.monotonic()-started,2) if started else 0
        (ROOT/'reports/runtime_camera.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    run()
