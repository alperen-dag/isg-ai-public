"""Validated source descriptions. Secrets live only in the service environment."""
from dataclasses import dataclass
import math
import os
import re
import logging
from urllib.parse import urlsplit


@dataclass(frozen=True)
class CameraConfig:
    id: int
    kamera_adi: str = 'Camera'
    kamera_index: int = 0
    aktif: int = 1
    yasak_bolge_orani: float = .35
    source_type: str = 'usb'
    source_uri: str = ''
    credential_env: str = ''
    analysis_enabled: int = 1
    analysis_fps: float = 5
    worker_group: str = 'default'
    connection_timeout: float = 5
    reconnect_interval: float = 1
    priority: int = 1

    def __post_init__(self):
        if self.source_type not in ('usb', 'rtsp', 'video') or self.kamera_index < 0:
            raise ValueError('SOURCE_INVALID')
        if not isinstance(self.priority, int) or not 1 <= self.priority <= 10:
            raise ValueError('SOURCE_INVALID')
        for value in (self.analysis_fps, self.connection_timeout, self.reconnect_interval):
            if not math.isfinite(value) or not 0 < value <= 120:
                raise ValueError('SOURCE_INVALID')
        if self.credential_env and not re.fullmatch(r'ISG_CAMERA_[A-Z0-9_]+', self.credential_env):
            raise ValueError('SOURCE_INVALID')
        if self.source_type == 'rtsp':
            parsed = urlsplit(self.source_uri)
            if parsed.scheme not in ('rtsp', 'rtsps') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError('SOURCE_INVALID')
        if self.source_type == 'video' and (not self.source_uri or '://' in self.source_uri):
            raise ValueError('SOURCE_INVALID')

    @property
    def capture_key(self):
        return (self.source_type, self.source_uri, self.kamera_index,
                self.credential_env, self.connection_timeout)

    def source(self):
        if self.source_type == 'usb':
            return self.kamera_index
        if self.credential_env:
            # Environment contains the complete authenticated URL; never returned to UI/DB.
            value = os.environ.get(self.credential_env, '')
            parsed, endpoint = urlsplit(value), urlsplit(self.source_uri)
            if not value or (parsed.scheme, parsed.hostname, parsed.port, parsed.path, parsed.query, parsed.fragment) != (endpoint.scheme, endpoint.hostname, endpoint.port, endpoint.path, endpoint.query, endpoint.fragment):
                raise ValueError('SOURCE_INVALID')
            return value
        return self.source_uri


def load_cameras(connection, group=None):
    cursor = connection.execute('SELECT * FROM kameralar WHERE aktif=1 ORDER BY id')
    names = [c[0] for c in cursor.description]
    result = []
    for row in cursor:
        values = {k: v for k, v in zip(names, row) if k in CameraConfig.__dataclass_fields__}
        try:
            config = CameraConfig(**values)
        except (ValueError, TypeError):
            # A malformed externally edited row must not stop all healthy cameras.
            logging.getLogger(__name__).warning('camera_id=%s event=SOURCE_INVALID', values.get('id'))
            continue
        if group is None or config.worker_group == group:
            result.append(config)
    return result


def opencv_capture(config):
    import cv2
    # Disable native diagnostics before passing any authenticated URL to FFmpeg.
    os.environ['OPENCV_FFMPEG_LOGLEVEL'] = '-8'
    os.environ['OPENCV_LOG_LEVEL'] = 'SILENT'
    if hasattr(cv2, 'setLogLevel'):
        cv2.setLogLevel(0)
    if config.source_type == 'usb':
        return cv2.VideoCapture(config.source())
    timeout = int(config.connection_timeout * 1000)
    return cv2.VideoCapture(config.source(), cv2.CAP_FFMPEG, [
        cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout,
        cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout])
