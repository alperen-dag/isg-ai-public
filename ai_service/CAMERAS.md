# Çoklu kamera çalıştırma

`python ai_service/main.py` artık dinamik çoklu kamera servisini başlatır. İlk geçişte
bir kez servisi yeniden başlatın. Eski `servisi_calistir()` webcam/GUI fonksiyonu
geriye uyumlu testler ve eski entegrasyonlar için korunmuştur; yeni giriş noktası
`multi_camera_service.run()` olur. Yeni servis headless çalışır; Ctrl+C/SIGTERM ile kapanır.

## Kaynaklar ve migration

`prepare_database()` mevcut veriyi silmeden eksik kolonları ekler. USB varsayılanı
`source_type=usb, kamera_index=0` korunur. Tek aktif kamera indeksi kaldırılır;
USB index benzersizliği yalnız USB kaynakları için uygulanır. RTSP/video kayıtları
USB index alanında 0 kullanabilir. İhlaller mevcut `kamera_id` alanını kullanır.
Migration idempotenttir; testler yalnız geçici DB üzerinde çalışır. Bu turda
canlı veritabanına migration uygulanmadı; normal servis/panel başlangıcı uygular.

Panelde kaynak tipi, URI, AI analiz durumu, hedef FPS ve worker grubu düzenlenir.
Yeni kamera mevcut kameraları pasifleştirmez. Her kamera bağımsız kapatılabilir.

* USB: `source_type=usb`, `kamera_index=0`.
* Video: `source_type=video`, `source_uri=C:/videos/test.avi`. Dosya bittiğinde
  reconnect aralığından sonra yeniden açılır; kayıtlı FPS'e göre okunur.
* RTSP: `source_type=rtsp`, `source_uri=rtsp://192.168.1.100:554/STREAM`.
  Kimlik doğrulaması gerekiyorsa `credential_env=ISG_CAMERA_PRESS_1` girin.
  Yalnız AI servisinin ortamında bu değişkeni tam URL olarak tanımlayın:
  `rtsp://USER:PASSWORD@192.168.1.100:554/STREAM` (örnek yer tutucular).
  Kullanıcı/şifrede URL özel karakterleri percent-encode edilmelidir.

Panel şifre almaz, saklamaz veya geri göstermez. DB yalnız şifresiz URI ile ortam
değişkeni adını tutar. Ortam URL'sinin protokol/host/port/path'i DB adresiyle aynı
olmalıdır. Query/fragment içeren RTSP adresleri bu sürümde kabul edilmez; bunlar
token içerebildiğinden güvenli kaynak ayrıştırması yapılmadan desteklenmeyecektir.
URI ve secret referansı audit payload'una dahil edilmez. Native FFmpeg stderr'i
yalnız capture alt sürecinde kapatılır; worker loglarında yalnız ID ve hata kodu vardır.
Kimlik doğrulama hatası ham metin incelenmeden genel bağlantı hatası olarak raporlanır.

## Mimari

Her gerçek kamera için sonlandırılabilir OpenCV capture süreci ve bir yönetici
thread'i vardır. Alt süreç model yüklemez. IPC kuyruğu bir kare, scheduler tamponu
bir karedir; yavaş analizde eski kareler atılır. Geçici encode/IPC/inference
kopyaları da vardır; RAM sınırı kare boyutu ve kamera sayısıyla orantılıdır.

Tek scheduler pose modelini ve PPE modelini birer kez yükler; model çağrıları
seri yapılır. Kamera başına hedef FPS, bağımsız tracker, zone geçmişi, PPE rule
cache ve event gate kullanılır. Round-robin sıra açlığı önler. `CameraConfig.priority`
1–10 ağırlığı programatik genişleme noktasıdır; bu turda DB/UI alanı değildir.
Hedef FPS kapasite garantisi değildir. GPU yetişmezse gerçekleşen analiz FPS'i düşer.

Health bellek içinde her karede güncellenir; tüm kameralar tek SQLite yazıcısını
paylaşır. Kamera başına yalnız son bekleyen health örneği tutulur; normal yazım
2 saniyede bir, durum değişiminde hemen kuyruğa alınır. Paneldeki FPS capture FPS'idir.
Offline reconnect aralığı 1 saniyeden başlayıp 2, 4, 8, 10 saniyeye yükselir.
Başarılı kare geri gelince backoff sıfırlanır. Native takılma timeout sonrasında
alt süreç sonlandırılarak temizlenir. Aynı anda kapanışta önce tüm worker'lara stop verilir.

## Dinamik ayarlar

Varsayılan kamera refresh 2 saniye. Ad/FPS/analiz/zone değişikliği stream'i yeniden
açmaz. Kaynak tipi/URI/USB index/secret referansı/timeout değişirse yalnız ilgili
worker değiştirilir; kaynak değişiminde eski tracker geçmişi silinir. Kamera silme,
pasifleştirme ve grup dışına taşıma worker'ı kaldırır. DB erişilemiyorsa son kamera
konfigürasyonu korunur; PPE kuralları mevcut 2 saniye TTL / 30 saniye hata toleransı
politikasını kullanmaya devam eder. Geçersiz bir kaynak satırı diğerlerini engellemez.

| Ayar | Varsayılan |
|---|---:|
| CAMERA_CONFIG_REFRESH_SECONDS | 2 |
| CAMERA_STALE_SECONDS | 10 |
| CAMERA_FREEZE_SECONDS | 10 (0 kapatır) |
| CAMERA_RECONNECT_MAX_SECONDS | 10 |
| CAMERA_HEALTH_DB_FLUSH_SECONDS | 2 |
| MAX_INFERENCE_CONCURRENCY | 1 (başka değer reddedilir) |
| CAMERA_WORKER_GROUP | belirtilmezse tüm gruplar |
| DEFAULT_CAMERA_ANALYSIS_FPS | API form alanı yoksa 5 |

Kamera başına DB'deki `reconnect_interval` minimum backoff, `connection_timeout`
bağlantı/okuma timeout'udur. Okuma timeout'u stale süresini aşamaz. Ortam ayarları,
model ayarları ve servis ortamındaki şifre değişimi servis restart gerektirir.
Panelde FPS alanı varsayılan 5 gönderir; DB migration varsayılanı da 5'tir.

## Doğrulama ve kapasite sınırları

`python -m unittest discover -s tests -q`

`python scripts/benchmark_cameras.py` 10/25/50/100 üretilen kaynakla scheduler,
backpressure, thread sayısı, Python allocation trendi ve health yazım isteklerini
ölçer. Çıktı `reports/multi_camera_scale.json` dosyasındadır. Bu benchmark capture
processlerini, gerçek SQLite disk yazımını, RTSP decoder'ını veya GPU'yu ölçmez.
Her ölçek 4 saniye çalışır; uzun süreli memory leak yokluğu kanıtı değildir.
Yerel video testi gerçek OpenCV capture alt sürecini ve kapanışını doğrular.

Üç IP kamera için kontrollü gerçek RTSP testine geçilebilir; gerçek cihaz, şifre,
codec, ağ kopması, servis SIGTERM ve uzun süreli VRAM/RSS testi sahada yapılmalıdır.
Stale tespiti başarılı yeni frame tesliminin kesilmesini izler. Ayrıca capture
süreci saniyede bir görüntünün CRC32 özetini karşılaştırır; 10 saniye aynı özet
kalırsa kapanır ve worker yeniden bağlanır. Bu sezgisel freeze tespiti tamamen
statik görüntü ile donmuş encoder'ı ayıramaz; statik test kaynaklarında
`CAMERA_FREEZE_SECONDS=0` ile kapatılabilir. Özet çakışmaları ve hareketin örnekleme
arasında kaçması mümkündür. Decoder PTS tabanlı doğrulama sonraki iştir.

100 kamera için grupları farklı GPU node'larına bölün. Grup filtresi hazırdır,
fakat dağıtık sahiplik/lease/failover yoktur; aynı grubu iki servise atamayın.
SQLite dosyasını node'lar arasında ağ diski üzerinden paylaşmayın. Çok node için
merkezi DB/API ve health/event aktarımı gerekir. Bu tur bir cluster kurmaz.
Capture başına process maliyeti, IPC kopyalama ve yüksek çözünürlükte decoder CPU/RAM
maliyeti gerçek 100 stream testiyle ölçülmelidir. Kaynak yeniden açılırken scheduler
worker kapanışını sınırlı süre bekleyebilir. In-flight GPU çağrısı zorla kesilmez.

RTX 5070 Laptop üzerinde gerçek kamera kapasitesi bu tur ölçülmedi. Ölçülen toplam
pose+PPE analiz kapasitesi R kare/sn ise 5 FPS hedefinde teorik üst sınır R/5 kameradır;
decoder yükü ve operasyon payı bu sınırı düşürür. 100×5=500 analiz/sn hedefi için
birden fazla node planlayın; sayısal GPU kapasite garantisi verilmez.
