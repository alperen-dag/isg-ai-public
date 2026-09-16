import sqlite3
import os
import json
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent

_configured_database_path = os.environ.get("ISG_DATABASE_PATH")

if _configured_database_path:
    DB_PATH = Path(_configured_database_path).expanduser()

    if not DB_PATH.is_absolute():
        raise RuntimeError("ISG_DATABASE_PATH mutlak bir dosya yolu olmalıdır.")
else:
    DB_PATH = PROJECT_ROOT / "isg.db"


def connect_database():
    connection = sqlite3.connect(DB_PATH)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _existing_columns(connection, table_name):
    return {
        row[1]
        for row in connection.execute(f"PRAGMA table_info({table_name})")
    }


def _add_missing_columns(connection, table_name, columns):
    existing_columns = _existing_columns(connection, table_name)

    for column_name, column_definition in columns.items():
        if column_name not in existing_columns:
            connection.execute(
                f"ALTER TABLE {table_name} "
                f"ADD COLUMN {column_name} {column_definition}"
            )


def _audit_degeri_json(deger):
    if deger is None:
        return None

    return json.dumps(deger, ensure_ascii=False, sort_keys=True)


def audit_kaydi_ekle(
    connection,
    kullanici_id,
    kullanici_adi,
    islem_turu,
    hedef_turu,
    hedef_id,
    eski_deger=None,
    yeni_deger=None,
    ip_adresi=None,
):
    connection.execute(
        """
        INSERT INTO audit_log (
            kullanici_id,
            kullanici_adi,
            islem_turu,
            hedef_turu,
            hedef_id,
            eski_deger,
            yeni_deger,
            ip_adresi,
            tarih
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            kullanici_id,
            kullanici_adi,
            islem_turu,
            hedef_turu,
            hedef_id,
            _audit_degeri_json(eski_deger),
            _audit_degeri_json(yeni_deger),
            ip_adresi,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )


def _kullanici_yetkileri_foreign_key_is_valid(connection):
    foreign_keys = connection.execute(
        "PRAGMA foreign_key_list(kullanici_yetkileri)"
    ).fetchall()

    return any(
        foreign_key[2] == "kullanicilar"
        and foreign_key[3] == "kullanici_id"
        and foreign_key[4] == "id"
        and foreign_key[6].upper() == "CASCADE"
        for foreign_key in foreign_keys
    )


def _migrate_kullanici_yetkileri_foreign_key(connection):
    if _kullanici_yetkileri_foreign_key_is_valid(connection):
        return

    foreign_keys_enabled = connection.execute(
        "PRAGMA foreign_keys"
    ).fetchone()[0]

    if foreign_keys_enabled != 1:
        raise RuntimeError("SQLite foreign key denetimi etkinleştirilemedi.")

    try:
        connection.execute("BEGIN IMMEDIATE")

        orphan_records = connection.execute(
            """
            SELECT ky.id, ky.kullanici_id
            FROM kullanici_yetkileri AS ky
            LEFT JOIN kullanicilar AS k ON k.id = ky.kullanici_id
            WHERE k.id IS NULL
            ORDER BY ky.id
            """
        ).fetchall()

        if orphan_records:
            raise RuntimeError(
                "kullanici_yetkileri migration işlemi durduruldu; "
                f"{len(orphan_records)} orphan kayıt bulundu: "
                f"{orphan_records}"
            )

        original_count = connection.execute(
            "SELECT COUNT(*) FROM kullanici_yetkileri"
        ).fetchone()[0]

        connection.execute(
            """
            CREATE TABLE kullanici_yetkileri_migration (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kullanici_id INTEGER NOT NULL,
                modul TEXT NOT NULL,
                UNIQUE(kullanici_id, modul),
                FOREIGN KEY (kullanici_id)
                    REFERENCES kullanicilar(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            INSERT INTO kullanici_yetkileri_migration
                (id, kullanici_id, modul)
            SELECT id, kullanici_id, modul
            FROM kullanici_yetkileri
            """
        )

        migrated_count = connection.execute(
            "SELECT COUNT(*) FROM kullanici_yetkileri_migration"
        ).fetchone()[0]

        if migrated_count != original_count:
            raise RuntimeError(
                "Yetki kayıt sayısı migration sırasında değişti: "
                f"önce={original_count}, sonra={migrated_count}."
            )

        connection.execute("DROP TABLE kullanici_yetkileri")
        connection.execute(
            "ALTER TABLE kullanici_yetkileri_migration "
            "RENAME TO kullanici_yetkileri"
        )

        final_count = connection.execute(
            "SELECT COUNT(*) FROM kullanici_yetkileri"
        ).fetchone()[0]
        foreign_key_errors = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()

        if final_count != original_count:
            raise RuntimeError(
                "Yetki kayıt sayısı migration sonrasında değişti: "
                f"önce={original_count}, sonra={final_count}."
            )

        if foreign_key_errors:
            raise RuntimeError(
                "Foreign key doğrulaması başarısız oldu: "
                f"{foreign_key_errors}"
            )

        if not _kullanici_yetkileri_foreign_key_is_valid(connection):
            raise RuntimeError("Gerekli foreign key oluşturulamadı.")

        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _index_columns(connection, index_name):
    return [
        row[2]
        for row in connection.execute(
            f"PRAGMA index_info({index_name})"
        ).fetchall()
    ]


def _kamera_index_kisitlari_gecerli(connection):
    kamera_index_unique = False
    tek_aktif_unique = False

    for index in connection.execute("PRAGMA index_list(kameralar)").fetchall():
        index_name = index[1]
        unique = index[2] == 1
        partial = index[4] == 1
        columns = _index_columns(connection, index_name)

        if unique and not partial and columns == ["kamera_index"]:
            kamera_index_unique = True

        if unique and partial and columns == ["aktif"]:
            index_sql_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = ?",
                (index_name,),
            ).fetchone()
            index_sql = "" if index_sql_row is None else index_sql_row[0]
            normalized_sql = "".join(index_sql.lower().split())

            if "whereaktif=1" in normalized_sql:
                tek_aktif_unique = True

    return kamera_index_unique, tek_aktif_unique


def _kamera_kisitlarini_hazirla(connection):
    kamera_index_unique, tek_aktif_unique = (
        _kamera_index_kisitlari_gecerli(connection)
    )

    if kamera_index_unique and tek_aktif_unique:
        return

    try:
        connection.execute("BEGIN IMMEDIATE")
        duplicate_indexes = connection.execute(
            """
            SELECT kamera_index, COUNT(*)
            FROM kameralar
            GROUP BY kamera_index
            HAVING COUNT(*) > 1
            ORDER BY kamera_index
            """
        ).fetchall()

        if duplicate_indexes:
            raise RuntimeError(
                "Kamera constraint migration işlemi durduruldu; "
                "duplicate kamera indexleri bulundu: "
                f"{duplicate_indexes}"
            )

        aktif_kameralar = connection.execute(
            "SELECT id FROM kameralar WHERE aktif = 1 ORDER BY id"
        ).fetchall()

        if len(aktif_kameralar) > 1:
            raise RuntimeError(
                "Kamera constraint migration işlemi durduruldu; "
                "birden fazla aktif kamera bulundu: "
                f"{[row[0] for row in aktif_kameralar]}"
            )

        original_count = connection.execute(
            "SELECT COUNT(*) FROM kameralar"
        ).fetchone()[0]
        existing_index_names = {
            row[1]
            for row in connection.execute(
                "PRAGMA index_list(kameralar)"
            ).fetchall()
        }

        if not kamera_index_unique:
            index_name = "ux_kameralar_kamera_index"

            if index_name in existing_index_names:
                raise RuntimeError(
                    f"{index_name} adlı mevcut index beklenen yapıda değil."
                )

            connection.execute(
                "CREATE UNIQUE INDEX ux_kameralar_kamera_index "
                "ON kameralar(kamera_index)"
            )

        if not tek_aktif_unique:
            index_name = "ux_kameralar_tek_aktif"

            if index_name in existing_index_names:
                raise RuntimeError(
                    f"{index_name} adlı mevcut index beklenen yapıda değil."
                )

            connection.execute(
                "CREATE UNIQUE INDEX ux_kameralar_tek_aktif "
                "ON kameralar(aktif) WHERE aktif = 1"
            )

        final_count = connection.execute(
            "SELECT COUNT(*) FROM kameralar"
        ).fetchone()[0]
        final_constraints = _kamera_index_kisitlari_gecerli(connection)

        if final_count != original_count:
            raise RuntimeError(
                "Kamera kayıt sayısı migration sırasında değişti: "
                f"önce={original_count}, sonra={final_count}."
            )

        if final_constraints != (True, True):
            raise RuntimeError("Kamera unique constraintleri oluşturulamadı.")

        connection.commit()
    except Exception:
        connection.rollback()
        raise


def prepare_database():
    connection = connect_database()

    try:
        with connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ihlaller (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tarih TEXT NOT NULL,
                    ihlal_turu TEXT NOT NULL,
                    guven_skoru REAL,
                    fotograf_yolu TEXT NOT NULL,
                    durum TEXT DEFAULT 'Yeni',
                    aciklama TEXT DEFAULT ''
                )
                """
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS kullanicilar (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ad_soyad TEXT NOT NULL,
                    kullanici_adi TEXT UNIQUE NOT NULL,
                    sifre TEXT NOT NULL,
                    rol TEXT NOT NULL DEFAULT 'Görüntüleyici',
                    aktif INTEGER NOT NULL DEFAULT 1
                )
                """
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS kullanici_yetkileri (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kullanici_id INTEGER NOT NULL,
                    modul TEXT NOT NULL,
                    UNIQUE(kullanici_id, modul),
                    FOREIGN KEY (kullanici_id)
                        REFERENCES kullanicilar(id) ON DELETE CASCADE
                )
                """
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS giris_denemeleri (
                    anahtar TEXT PRIMARY KEY,
                    basarisiz_sayisi INTEGER NOT NULL DEFAULT 0,
                    pencere_baslangici INTEGER NOT NULL,
                    engel_bitis INTEGER NOT NULL DEFAULT 0
                )
                """
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kullanici_id INTEGER,
                    kullanici_adi TEXT NOT NULL,
                    islem_turu TEXT NOT NULL,
                    hedef_turu TEXT NOT NULL,
                    hedef_id INTEGER,
                    eski_deger TEXT,
                    yeni_deger TEXT,
                    ip_adresi TEXT,
                    tarih TEXT NOT NULL,
                    FOREIGN KEY (kullanici_id)
                        REFERENCES kullanicilar(id) ON DELETE SET NULL
                )
                """
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS kameralar (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kamera_adi TEXT NOT NULL
                        CHECK(length(trim(kamera_adi)) > 0),
                    kamera_index INTEGER NOT NULL
                        CHECK(kamera_index >= 0),
                    aktif INTEGER NOT NULL DEFAULT 1
                        CHECK(aktif IN (0, 1)),
                    yasak_bolge_orani REAL NOT NULL DEFAULT 0.35
                        CHECK(
                            yasak_bolge_orani >= 0
                            AND yasak_bolge_orani <= 1
                        ),
                    olusturma_tarihi TEXT NOT NULL,
                    guncelleme_tarihi TEXT NOT NULL
                )
                """
            )

            connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_ihlaller_tarih "
                "ON ihlaller(tarih)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_ihlaller_durum "
                "ON ihlaller(durum)"
            )

            kamera_sayisi = connection.execute(
                "SELECT COUNT(*) FROM kameralar"
            ).fetchone()[0]

            if kamera_sayisi == 0:
                simdi = datetime.now().isoformat(timespec="seconds")
                connection.execute(
                    """
                    INSERT INTO kameralar (
                        kamera_adi,
                        kamera_index,
                        aktif,
                        yasak_bolge_orani,
                        olusturma_tarihi,
                        guncelleme_tarihi
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    ("Ana Kamera", 0, 1, 0.35, simdi, simdi),
                )

            _add_missing_columns(
                connection,
                "ihlaller",
                {
                    "tarih": "TEXT NOT NULL DEFAULT ''",
                    "ihlal_turu": "TEXT NOT NULL DEFAULT ''",
                    "guven_skoru": "REAL",
                    "fotograf_yolu": "TEXT NOT NULL DEFAULT ''",
                    "durum": "TEXT DEFAULT 'Yeni'",
                    "aciklama": "TEXT DEFAULT ''",
                    "kamera_id": "INTEGER REFERENCES kameralar(id) ON DELETE SET NULL",
                    "person_id": "TEXT",
                    "cozum_notu": "TEXT",
                    "cozen_kullanici_id": "INTEGER REFERENCES kullanicilar(id) ON DELETE SET NULL",
                    "cozen_kullanici_adi": "TEXT",
                    "cozum_tarihi": "TEXT",
                },
            )

            _add_missing_columns(
                connection,
                "kullanicilar",
                {
                    "ad_soyad": "TEXT NOT NULL DEFAULT ''",
                    "eposta": "TEXT",
                    "kullanici_adi": "TEXT NOT NULL DEFAULT ''",
                    "sifre": "TEXT NOT NULL DEFAULT ''",
                    "rol": "TEXT NOT NULL DEFAULT 'Görüntüleyici'",
                    "aktif": "INTEGER NOT NULL DEFAULT 1",
                },
            )

            _add_missing_columns(
                connection,
                "kullanici_yetkileri",
                {
                    "kullanici_id": "INTEGER",
                    "modul": "TEXT NOT NULL DEFAULT ''",
                },
            )

            connection.execute("""
                CREATE TABLE IF NOT EXISTS camera_health (
                    kamera_id INTEGER PRIMARY KEY REFERENCES kameralar(id) ON DELETE CASCADE,
                    session_id TEXT NOT NULL, run_started_at REAL NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('online','offline','unknown')),
                    last_frame_at REAL, last_error_at REAL, error_code TEXT,
                    fps REAL, updated_at REAL NOT NULL
                )
            """)

            connection.execute("""
                CREATE TABLE IF NOT EXISTS kamera_ppe_kurallari (
                    kamera_id INTEGER NOT NULL REFERENCES kameralar(id) ON DELETE CASCADE,
                    equipment TEXT NOT NULL CHECK(equipment IN
                        ('helmet', 'vest', 'ear_protection', 'safety_shoes')),
                    aktif INTEGER NOT NULL DEFAULT 0 CHECK(aktif IN (0, 1)),
                    confidence_threshold REAL NOT NULL DEFAULT 0.5
                        CHECK(confidence_threshold >= 0 AND confidence_threshold <= 1),
                    PRIMARY KEY (kamera_id, equipment)
                )
            """)

        _migrate_kullanici_yetkileri_foreign_key(connection)
        with connection:
            _add_missing_columns(connection, 'kameralar', {
                'source_type': "TEXT NOT NULL DEFAULT 'usb'",
                'source_uri': "TEXT NOT NULL DEFAULT ''",
                'credential_env': "TEXT NOT NULL DEFAULT ''",
                'analysis_enabled': 'INTEGER NOT NULL DEFAULT 1',
                'analysis_fps': 'REAL NOT NULL DEFAULT 5',
                'worker_group': "TEXT NOT NULL DEFAULT 'default'",
                'connection_timeout': 'REAL NOT NULL DEFAULT 5',
                'reconnect_interval': 'REAL NOT NULL DEFAULT 1',
            })
            connection.execute('DROP INDEX IF EXISTS ux_kameralar_tek_aktif')
            connection.execute('DROP INDEX IF EXISTS ux_kameralar_kamera_index')
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_kameralar_usb_index ON kameralar(kamera_index) WHERE source_type='usb'")
    finally:
        connection.close()
