"""Coalesced asynchronous frame health; web reads infer stale status without writes."""
from contextlib import closing
from datetime import datetime
import logging
import math
from queue import Queue, Empty, Full
import sqlite3
from threading import Thread, Condition
import time
from uuid import uuid4

import isg_database

ERROR_SUMMARIES = {
    'STREAM_OPEN_FAILED': 'Kamera bağlantısı açılamadı.',
    'STREAM_READ_FAILED': 'Kamera görüntüsü alınamadı.',
    'STREAM_TIMEOUT': 'Kamera bağlantısı zaman aşımına uğradı.',
    'STALE_FRAME': 'Güncel kamera görüntüsü yok.',
    'SOURCE_INVALID': 'Kamera kaynağı geçersiz.',
    'open_failed': 'Kamera açılamadı.',
    'read_failed': 'Kameradan görüntü alınamadı.',
    'service_failed': 'AI servisi beklenmedik şekilde durdu.',
}


def write_health(database_path, config, sample):
    with closing(sqlite3.connect(database_path, timeout=config.db_timeout_seconds)) as connection, connection:
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('''INSERT INTO camera_health
            (kamera_id,session_id,run_started_at,status,last_frame_at,last_error_at,error_code,fps,updated_at)
            VALUES(:kamera_id,:session_id,:run_started_at,:status,:last_frame_at,:last_error_at,:error_code,:fps,:updated_at)
            ON CONFLICT(kamera_id) DO UPDATE SET
                session_id=excluded.session_id,run_started_at=excluded.run_started_at,status=excluded.status,
                last_frame_at=COALESCE(excluded.last_frame_at,camera_health.last_frame_at),
                last_error_at=COALESCE(excluded.last_error_at,camera_health.last_error_at),
                error_code=COALESCE(excluded.error_code,camera_health.error_code),
                fps=excluded.fps,updated_at=excluded.updated_at
            WHERE excluded.run_started_at>=camera_health.run_started_at
              AND excluded.updated_at>=camera_health.updated_at''', sample)


class CameraHealthReporter:
    def __init__(self, camera_id, config, database_path=None, writer=write_health,
                 clock=time.monotonic, wall_clock=time.time, dispatcher=None):
        self.dispatcher = dispatcher
        self.config, self.writer = config, writer
        self.database_path = database_path if database_path is not None else isg_database.DB_PATH
        self.clock, self.wall_clock = clock, wall_clock
        now = wall_clock()
        self.sample = dict(kamera_id=camera_id,session_id=uuid4().hex,run_started_at=now,
                           status='unknown',last_frame_at=None,last_error_at=None,error_code=None,
                           fps=None,updated_at=now)
        self.next_write = float('-inf')
        self.window_start = clock()
        self.window_frames = 0
        self.closed = False
        self.queue = Queue(maxsize=1)
        self.worker = Thread(target=self._run, name='camera-health', daemon=True)
        if dispatcher is None:
            self.worker.start()
        self._publish(force=True)

    def _run(self):
        failed = False
        while True:
            sample = self.queue.get()
            try:
                if sample is None:
                    return
                self.writer(self.database_path, self.config, sample)
                failed = False
            except Exception:
                if not failed:
                    logging.getLogger(__name__).exception('Camera health write failed; inference continues')
                failed = True
            finally:
                self.queue.task_done()

    def _offer(self, sample):
        if self.dispatcher is not None:
            self.dispatcher.offer(self.database_path, self.config, sample)
            return
        try:
            self.queue.put_nowait(sample)
        except Full:
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except Empty:
                pass
            self.queue.put_nowait(sample)

    def _publish(self, force=False):
        now = self.clock()
        if force or now >= self.next_write:
            self.next_write = now + self.config.health_write_seconds
            self.sample['updated_at'] = self.wall_clock()
            self._offer(dict(self.sample))

    def frame_received(self):
        if self.closed:
            return
        self.sample['last_frame_at'] = self.wall_clock()
        previous = self.sample['status']
        self.sample['status'] = 'online'
        self.window_frames += 1
        elapsed = self.clock() - self.window_start
        if elapsed >= 1:
            self.sample['fps'] = round(self.window_frames / elapsed, 2)
            self.window_start = self.clock()
            self.window_frames = 0
        self._publish(force=previous != 'online')

    def error(self, code):
        if self.closed:
            return
        changed = self.sample['status'] != 'offline' or self.sample['error_code'] != code
        self.sample.update(status='offline',last_error_at=self.wall_clock(),
                           error_code=code if code in ERROR_SUMMARIES else 'service_failed',fps=None)
        self._publish(force=changed)

    def close(self):
        if self.closed:
            return
        self.sample.update(status='offline',fps=None)
        self._publish(force=True)
        self.closed = True
        if self.dispatcher is not None:
            return
        # Only shutdown waits; frame/inference calls never wait for SQLite.
        # Bounded SQLite timeout means normal writes drain quickly.
        try:
            self.queue.put(None, timeout=self.config.db_timeout_seconds + .5)
        except Full:
            return  # Failed shutdown write becomes stale/offline on the reader.
        self.worker.join(timeout=self.config.db_timeout_seconds + .5)


class HealthDispatcher:
    """One SQLite writer shared by all cameras; latest sample per camera wins."""
    def __init__(self, writer=write_health):
        self.writer = writer
        self.pending = {}
        self.condition = Condition()
        self.stopping = False
        self.thread = Thread(target=self.run, name='camera-health-writer', daemon=True)
        self.thread.start()

    def offer(self, path, config, sample):
        with self.condition:
            self.pending[sample['kamera_id']] = (path, config, sample)
            self.condition.notify()

    def run(self):
        failed = False
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.pending or self.stopping)
                if not self.pending:
                    return
                key = next(iter(self.pending))
                args = self.pending.pop(key)
            try:
                self.writer(*args)
                failed = False
            except Exception:
                if not failed:
                    logging.getLogger(__name__).warning('event=camera_health_write_failed')
                failed = True

    def close(self):
        with self.condition:
            self.stopping = True
            self.condition.notify()
        self.thread.join(15)


def health_view(row=None, stale_seconds=10, now=None):
    result = dict(status='unknown',label='Bilinmiyor',last_frame='Mevcut değil',
                  last_error='Mevcut değil',error_summary='',fps=None,stale=False)
    if not row:
        return result
    now = time.time() if now is None else now
    def timestamp(value):
        return isinstance(value,(int,float)) and math.isfinite(value) and 0 < value <= now + 2
    for field, name in [('last_frame_at','last_frame'), ('last_error_at','last_error')]:
        if timestamp(row.get(field)):
            try:
                result[name] = datetime.fromtimestamp(row[field]).strftime('%Y-%m-%d %H:%M:%S')
            except (ValueError, OverflowError, OSError):
                pass
    result['error_summary'] = ERROR_SUMMARIES.get(row.get('error_code'), '')
    status = row.get('status')
    if status == 'offline':
        result.update(status='offline',label='Çevrimdışı')
    elif status == 'online' and timestamp(row.get('last_frame_at')) and timestamp(row.get('updated_at')):
        fresh = (now-row['last_frame_at'] <= stale_seconds and now-row['updated_at'] <= stale_seconds
                 and row['last_frame_at'] >= row.get('run_started_at', row['last_frame_at']))
        result.update(status='online' if fresh else 'offline',label='Çevrimiçi' if fresh else 'Çevrimdışı',stale=not fresh)
        fps = row.get('fps')
        if fresh and isinstance(fps,(int,float)) and math.isfinite(fps) and fps >= 0:
            result['fps'] = fps
    return result


def load_health_views(connection, stale_seconds=10, now=None):
    try:
        cursor = connection.execute('SELECT * FROM camera_health')
        names = [column[0] for column in cursor.description]
        return {row[0]: health_view(dict(zip(names,row)), stale_seconds, now) for row in cursor.fetchall()}
    except (sqlite3.Error, ValueError, TypeError):
        logging.getLogger(__name__).warning('Camera health unavailable; displaying unknown')
        return {}
