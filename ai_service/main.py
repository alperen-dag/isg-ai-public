from collections import deque
from pathlib import Path
import sys

import cv2


PROJE_KOKU = Path(__file__).resolve().parent.parent

if str(PROJE_KOKU) not in sys.path:
    sys.path.insert(0, str(PROJE_KOKU))

from isg_database import connect_database, prepare_database
from camera_health import CameraHealthReporter
from ai_service.runtime_config import load_runtime_config
from ai_service.rule_cache import CameraRuleCache, changed_violation_types
from ai_service.event_tracking import PersonTracker, PPEEventGate, load_tracking_config
from ai_service.pose_adapter import append_person
from ai_service.ppe_pipeline import PPEPipeline
from ai_service.rules import load_camera_rules, restricted_zone_violation
from ai_service.violation_store import save_violation
from ai_service.yolo_ppe_detector import create_ppe_detector
import logging
import time


ANALIZ_PENCERESI = 10
GEREKLI_IHLAL_KARESI = 5
GEREKLI_GUVENLI_KARE = 5
OMUZ_GUVEN_ESIGI = 0.20
KALCA_GUVEN_ESIGI = 0.15
MINIMUM_KUTU_GENISLIGI = 70
MINIMUM_KUTU_YUKSEKLIGI = 100
MODEL_GUVEN_ESIGI = 0.30

MODEL_YOLU = PROJE_KOKU / "yolo11n-pose.pt"
IHLAL_KLASORU = PROJE_KOKU / "ihlaller"
IHLAL_TURU = "Yasak Bolge Ihlali"


class IhlalTakipDurumu:
    def __init__(self):
        self.ihlal_gecmisi = deque(maxlen=ANALIZ_PENCERESI)
        self.confidence_history = deque(maxlen=ANALIZ_PENCERESI)
        self.ihlal_aktif = False
        self.guvenli_kare_sayaci = 0

    def kareyi_isle(self, bu_karede_ihlal, confidence=None):
        self.confidence_history.append(confidence if bu_karede_ihlal and confidence is not None and 0 < confidence <= 1 else None)
        if bu_karede_ihlal:
            self.ihlal_gecmisi.append(1)
            self.guvenli_kare_sayaci = 0
        else:
            self.ihlal_gecmisi.append(0)
            self.guvenli_kare_sayaci += 1

        ihlal_puani = sum(self.ihlal_gecmisi)

        if self.guvenli_kare_sayaci >= GEREKLI_GUVENLI_KARE:
            self.ihlal_aktif = False

        yeni_ihlal = (
            ihlal_puani >= GEREKLI_IHLAL_KARESI
            and not self.ihlal_aktif
        )
        return ihlal_puani, yeni_ihlal

    @property
    def confidence(self):
        return max((v for v in self.confidence_history if v is not None), default=None)

    def ihlal_kaydedildi(self):
        self.ihlal_aktif = True
        self.ihlal_gecmisi.clear()
        self.confidence_history.clear()


def modeli_yukle():
    from ultralytics import YOLO
    if not MODEL_YOLU.is_file():
        raise FileNotFoundError(f"Model dosyası bulunamadı: {MODEL_YOLU}")

    print(f"Model yükleniyor: {MODEL_YOLU}")
    return YOLO(str(MODEL_YOLU))


def aktif_kamera_ayarini_getir(baglanti):
    aktif_kameralar = baglanti.execute(
        """
        SELECT id, kamera_adi, kamera_index, yasak_bolge_orani
        FROM kameralar
        WHERE aktif = 1
        ORDER BY id
        LIMIT 2
        """
    ).fetchall()

    if not aktif_kameralar:
        raise RuntimeError("Aktif kamera bulunamadı; AI servisi başlatılamıyor.")

    if len(aktif_kameralar) > 1:
        print(
            "UYARI: Birden fazla aktif kamera bulundu; "
            "en düşük ID değerine sahip kamera kullanılacak."
        )

    kamera = aktif_kameralar[0]
    return {
        "id": kamera[0],
        "kamera_adi": kamera[1],
        "kamera_index": kamera[2],
        "yasak_bolge_orani": kamera[3],
    }


def kamerayi_ac(kamera_indexi):
    kamera = cv2.VideoCapture(kamera_indexi)

    if not kamera.isOpened():
        kamera.release()
        raise RuntimeError(f"Kamera açılamadı (index={kamera_indexi}).")

    print(f"Kamera açıldı (index={kamera_indexi}).")
    return kamera


def kareyi_analiz_et(model, kare, yasak_bolge_orani, people=None):
    yukseklik, genislik, _ = kare.shape
    yasak_bolge_x = int(genislik * (1 - yasak_bolge_orani))

    cv2.rectangle(
        kare,
        (yasak_bolge_x, 0),
        (genislik - 1, yukseklik - 1),
        (0, 0, 255),
        3,
    )
    cv2.putText(
        kare,
        "YASAK BOLGE",
        (yasak_bolge_x + 10, 30),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (0, 0, 255),
        2,
    )

    results = model(kare, verbose=False, conf=MODEL_GUVEN_ESIGI)
    bu_karede_ihlal = False
    en_yuksek_guven = 0.0

    for result in results:
        if result.boxes is None or result.keypoints is None:
            continue

        for i, box in enumerate(result.boxes):
            confidence = float(box.conf[0])
            x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
            kutu_genisligi = x2 - x1
            kutu_yuksekligi = y2 - y1

            if kutu_genisligi < MINIMUM_KUTU_GENISLIGI:
                continue

            if kutu_yuksekligi < MINIMUM_KUTU_YUKSEKLIGI:
                continue

            keypoint_conf = result.keypoints.conf[i]

            if keypoint_conf is None:
                continue

            sol_omuz = float(keypoint_conf[5])
            sag_omuz = float(keypoint_conf[6])
            sol_kalca = float(keypoint_conf[11])
            sag_kalca = float(keypoint_conf[12])

            omuz_var = (
                sol_omuz > OMUZ_GUVEN_ESIGI
                or sag_omuz > OMUZ_GUVEN_ESIGI
            )
            kalca_var = (
                sol_kalca > KALCA_GUVEN_ESIGI
                or sag_kalca > KALCA_GUVEN_ESIGI
            )

            if not (omuz_var and kalca_var):
                continue

            if people is not None:
                append_person(people, (x1, y1, x2, y2), confidence,
                              result.keypoints, i, keypoint_conf)

            kontrol_x = (x1 + x2) // 2
            kontrol_y = y2

            if kontrol_x > yasak_bolge_x:
                renk = (0, 0, 255)
                etiket = "IHLAL"
                bu_karede_ihlal = True
                en_yuksek_guven = max(en_yuksek_guven, confidence)
            else:
                renk = (0, 255, 0)
                etiket = "GUVENLI"

            cv2.rectangle(kare, (x1, y1), (x2, y2), renk, 3)
            cv2.circle(kare, (kontrol_x, kontrol_y), 7, renk, -1)
            cv2.putText(
                kare,
                f"{etiket} %{confidence * 100:.0f}",
                (x1, max(y1 - 10, 25)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                renk,
                2,
            )

    return bu_karede_ihlal, en_yuksek_guven


def ihlal_kaydi_olustur(baglanti, kare, en_yuksek_guven, simdi=None, kamera_id=None, person_id=None):
    return save_violation(baglanti, kare,
                          restricted_zone_violation(en_yuksek_guven, person_id),
                          IHLAL_KLASORU, kamera_id, simdi)


def servisi_calistir(ppe_detector=None):
    kamera = None
    baglanti = None
    rule_connection = None
    health = None
    runtime = load_runtime_config()

    try:
        prepare_database()
        baglanti = connect_database()
        kamera_ayari = aktif_kamera_ayarini_getir(baglanti)
        health = CameraHealthReporter(kamera_ayari["id"], runtime)
        print(
            "Seçilen kamera: "
            f"{kamera_ayari['kamera_adi']} | "
            f"index={kamera_ayari['kamera_index']} | "
            "yasak bölge="
            f"%{kamera_ayari['yasak_bolge_orani'] * 100:.1f}"
        )
        model = modeli_yukle()
        try:
            kamera = kamerayi_ac(kamera_ayari["kamera_index"])
        except Exception:
            health.error("open_failed")
            raise
        takip_durumu = IhlalTakipDurumu()
        ppe_pipeline = PPEPipeline(ppe_detector if ppe_detector is not None else create_ppe_detector())
        print('PPE desteklenen sınıflar: ' + ', '.join(sorted(
            getattr(ppe_pipeline.detector, 'supported_classes', ()))))
        rule_connection = connect_database()
        rule_connection.execute(f"PRAGMA busy_timeout={int(runtime.db_timeout_seconds * 1000)}")
        rule_cache = CameraRuleCache(lambda: load_camera_rules(rule_connection, kamera_ayari["id"]), runtime)
        ppe_configs = rule_cache.get()
        tracking_config = load_tracking_config()
        tracker = PersonTracker(tracking_config)
        event_gate = PPEEventGate(tracking_config)
        IHLAL_KLASORU.mkdir(parents=True, exist_ok=True)

        print("İSG AI servisi başladı. Kapatmak için Q tuşuna basın.")

        while True:
            basarili, kare = kamera.read()

            if not basarili:
                health.error("read_failed")
                raise RuntimeError("Kamera görüntüsü alınamadı.")

            health.frame_received()
            refreshed = rule_cache.get()
            changed = changed_violation_types(ppe_configs, refreshed)
            if changed:
                event_gate.reset_rules(kamera_ayari["id"], changed)
                logging.getLogger(__name__).info("PPE rules refreshed: %s", sorted(changed))
            ppe_configs = refreshed
            raw_frame = kare.copy() if ppe_configs else None
            people = []
            bu_karede_ihlal, en_yuksek_guven = kareyi_analiz_et(
                model,
                kare,
                kamera_ayari["yasak_bolge_orani"],
                people=people,
            )
            ihlal_puani, yeni_ihlal = takip_durumu.kareyi_isle(
                bu_karede_ihlal, en_yuksek_guven
            )

            cv2.putText(
                kare,
                f"Ihlal kontrol: {ihlal_puani}/{GEREKLI_IHLAL_KARESI}",
                (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (255, 255, 255),
                2,
            )

            if yeni_ihlal:
                ihlal_kaydi_olustur(
                    baglanti,
                    kare,
                    takip_durumu.confidence,
                    kamera_id=kamera_ayari["id"],
                )
                takip_durumu.ihlal_kaydedildi()

            people = tracker.update(people)
            now = time.monotonic()
            events = ppe_pipeline.evaluate(raw_frame, people, ppe_configs)
            for event in event_gate.evaluate(kamera_ayari["id"], events, now):
                try:
                    save_violation(baglanti, kare, event, IHLAL_KLASORU, kamera_ayari["id"])
                    event_gate.saved(kamera_ayari["id"], event, now)
                except Exception:
                    logging.exception("PPE kaydı başarısız; yasak bölge akışı devam ediyor")

            cv2.imshow("ISG AI - AI Servisi", kare)

            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
    except KeyboardInterrupt:
        print("Kapatma isteği alındı.")
    except Exception:
        if health is not None and health.sample['status'] != 'offline':
            health.error('service_failed')
        raise
    finally:
        if health is not None:
            health.close()
        if kamera is not None:
            kamera.release()

        if baglanti is not None:
            baglanti.close()

        if rule_connection is not None:
            rule_connection.close()

        cv2.destroyAllWindows()
        print("AI servisi kapatıldı.")


def main():
    try:
        from ai_service.multi_camera_service import run
        run()
    except Exception:
        print("KRİTİK HATA: AI servis başlatma/çalışma hatası.", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
