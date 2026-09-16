from datetime import datetime
from uuid import uuid4
import sys
import cv2


def save_violation(baglanti, kare, violation, image_dir, camera_id=None, simdi=None):
    simdi = simdi or datetime.now()
    tarih_veritabani = simdi.strftime("%Y-%m-%d %H:%M:%S")
    tarih_dosya = simdi.strftime("%Y-%m-%d_%H-%M-%S")
    dosya_adi = f"ihlal_{tarih_dosya}_{uuid4().hex}.jpg"
    dosya_yolu = image_dir / dosya_adi

    image_dir.mkdir(parents=True, exist_ok=True)

    if dosya_yolu.exists():
        raise FileExistsError(
            f"İhlal fotoğrafı zaten mevcut, üzerine yazılmadı: {dosya_yolu}"
        )

    if not cv2.imwrite(str(dosya_yolu), kare):
        raise RuntimeError(f"İhlal fotoğrafı kaydedilemedi: {dosya_yolu}")

    fotograf_db_yolu = f"ihlaller/{dosya_adi}"

    try:
        baglanti.execute(
            """
            INSERT INTO ihlaller (
                tarih,
                ihlal_turu,
                guven_skoru,
                fotograf_yolu, kamera_id, person_id, aciklama, durum
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'Yeni')
            """,
            (
                tarih_veritabani,
                violation.ihlal_turu,
                round(violation.confidence * 100, 2) if violation.confidence is not None else None,
                fotograf_db_yolu, camera_id, violation.person_id, violation.note,
            ),
        )
        baglanti.commit()
    except Exception:
        baglanti.rollback()

        try:
            dosya_yolu.unlink()
        except OSError as temizleme_hatasi:
            print(
                "UYARI: Başarısız DB kaydına ait fotoğraf silinemedi: "
                f"{dosya_yolu} ({temizleme_hatasi})",
                file=sys.stderr,
            )

        raise

    print(f"İhlal kaydedildi: {fotograf_db_yolu}")
    return fotograf_db_yolu
