# PPE / KKD backend sözleşmesi

Gerçek YOLOv8n PPE adapter'ı eklendi. Servis `config/ppe.json` üzerinden ayrı modeli
yükler; yükleme başarısızsa `UnavailablePPEDetector` `available=False` döndürür.
Kamera kuralları ayrıca etkin olmalıdır; yeni DB'de varsayılan kapalıdır. Pose modeli
yalnızca kişi/iskelet üretir. Boş detection listesi ekipman yokluğu anlamına gelmez.

## Model bağlama

`PPEDetector.detect(frame)` `YOLOPPEDetector` tarafından uygulanır; istenirse
`servisi_calistir(ppe_detector=adapter)` ile enjekte edilir. Detector temiz BGR kareyi
alır; aynı karenin pose çıktısı association için kullanılır. Sonuç `PPEBatch`:

- `available`: bu karede model değerlendirmesi başarılı mı?
- `supported_classes`: modelin gerçekten desteklediği normalize sınıflar.
- `detections`: `class_name`, 0–1 `confidence`, piksel `xyxy bbox`, opsiyonel
  kare içi `person_id` içeren `Detection` nesneleri.

Sınıflar: `helmet`, `vest`, `ear_protection`, `safety_shoes` ve bunların açık
eksiklik sınıfları `no_helmet`, `no_vest`, `no_ear_protection`, `no_safety_shoes`.
Yalnızca pozitif ekipman sınıfları üreten model bu sürümde eksiklik ihlali üretmez.
Görünürlük/örtülme doğrulaması olmayan bir modele boş kutudan eksiklik türettirilmemelidir.
Gerçek model yükleme/sınıf haritası doğrulandı; saha confidence kalibrasyonu gereklidir.

### Doğrulanmış model

- Kaynak: https://huggingface.co/Hansung-Cho/yolov8-ppe-detection
- Model kartı: YOLOv8n; dataset açıklaması public PPE/construction datasets (Kaggle vb.).
  Tam dataset sürümü/split bilgisi yayımlanmamış; bu turda eğitim yapılmadı veya
  dataset doğruluğu yeniden ölçülmedi. Model kartındaki başarı oranları saha sonucu değildir.
- Revision: `ac0027bd38bc619d5ce4f52b4cc01beb87d8b958`
- SHA-256: `2419700bbe3b8d38f9000655d9cf952a4bc93ef6c143baf8b49a0abde5d0760f`
- Model ağırlığı: `models/ppe_yolov8n.pt`, 6.250.090 byte; Git'e dahil edilmez.
- Model kartı lisans etiketi MIT; dataset kullanım şartları kaynak sağlayıcıya aittir.

Checkpoint metadata'sındaki gerçek 10 sınıf: `Hardhat`, `Mask`, `NO-Hardhat`,
`NO-Mask`, `NO-Safety Vest`, `Person`, `Safety Cone`, `Safety Vest`, `machinery`,
`vehicle`. Yalnızca şu dört sınıf uygulamaya aktarılır:

| Model etiketi | Standart çıktı | Anlam |
| --- | --- | --- |
| Hardhat | helmet | Baret var |
| NO-Hardhat | no_helmet | Açık baret eksikliği bulgusu |
| Safety Vest | vest | Reflektif yelek var |
| NO-Safety Vest | no_vest | Açık yelek eksikliği bulgusu |

Merkezi `ppe_labels.py`, `safety_vest/no_safety_vest` yazımını da mevcut
`vest/no_vest` DB/kural sözleşmesine eşler. Kulak koruyucu ve iş güvenliği
ayakkabısının pozitif/negatif sınıfları **bu modelde desteklenmiyor**. Bu hedefler
için etiketli özel dataset veya ayrı doğrulanmış model gerekir. Genel `boots`,
`shoes`, `head` gibi sınıflardan güvenlik ekipmanı/eksikliği türetilmez.

### Kurulum ve ayarlar

`python scripts/download_ppe_model.py` sabit revision'dan indirir, hash doğrular,
mevcut dosyayı ezmez. Servis başlangıcı model indirmez. `ISG_PPE_CONFIG` başka
manifest seçer; `ISG_PPE_MODEL_PATH` ağırlık yolunu, `ISG_PPE_CONFIDENCE` inference
eşiğini, `ISG_PPE_DEVICE` cihazı değiştirir. Göreli yollar repo köküne göre çözülür.
Manifestte `image_size` (640), `confidence` (0.25), `device` (cpu), `sha256` ve
`expected_names` bulunur. Farklı model için kendi doğrulanmış manifestini hazırlayın;
eski hash/sınıf listesiyle farklı ağırlık kabul edilmez. Bilinmeyen sınıflar atılır.

Kamera rule eşiği inference eşiğinden bağımsızdır (başlangıç önerisi 0.5);
etkin eşik iki değerin maksimumudur. Boş detection, yükleme/inference hatası,
desteklenmeyen eksiklik veya pozitif-only model eksiklik kaydı oluşturmaz.

### Performans ölçümü

`python scripts/benchmark_ppe.py --frames 180 --warmup 10 --display --output reports/ppe_benchmark.json`
gerçek aktif kamerayı okur; DB'yi ve kuralları değiştirmeden baret/yelek kurallarını
0.5 eşiğinde değerlendirir. İlk 10 kare ölçüm dışıdır. Model-native inference ile
pre/postprocess dahil predict duvar süreleri ayrı raporlanır. Toplam FPS kamera
okuma, association ve isteğe bağlı görüntülemeyi içerir; disk/DB kaydı içermez.
Kare bazlı olay sayıları doğruluk oranı veya benzersiz kişi/ihlal sayısı değildir.

2026-09-08 yerel ölçüm (`reports/ppe_benchmark_people.json`): CPU, 640x480 kamera,
imgsz=640, 10 ısınma + 180 ölçüm karesi. Pose inference 11.92 ms ortalama / 19.89 ms
p95, PPE inference 27.18 / 30.04 ms, toplam 16.65 FPS. Yalnızca 1 karede kabul
edilen kişi bulundu; bu sonuç kalabalık saha performansı olarak yorumlanmamalıdır.
İlk ölçümde kişi yoktu (`reports/ppe_benchmark.json`, 16.67 FPS).

Normal servis ayrıca gerçek kamerayla 40 saniye çalıştı ve `SERVICE_EXIT 0` ile
kapandı. Kamera 1'de `helmet` ve `vest` kuralları 0.5 eşiğinde etkinleştirildi;
öncesinde `backups/isg_pre_ppe_model_20260908_120752_60e547a7.db` alındı. Eski
kural satırları üzerine yazılmadı. Bu koşuda gerçek DB'ye 1 yelek eksikliği
(ID 12, %54.2), 1 baret eksikliği (ID 20, %60.49) ve 8 yasak bölge kaydı eklendi.
Görüntülerde bir kişinin ekipmansız baş/gövdesi görüldü; bu iki örnek dataset
doğruluğu ölçümü değildir. Ayrı karelerin `person_id=0` değeri kalıcı kimlik değildir.

Mevcut `bus.jpg` çoklu kişi fotoğrafında 3 pose kişisi ve .804 güvenli tek
`no_helmet` bulgusu yalnızca kişi 1'e eşleşti. Düşük güvenli yelek bulguları atıldı.
İki eksikliğin aynı kişiye atanması ve kişiler arası karışmama ayrıca deterministik
adapter/association testlerinde doğrulandı; iki fiziksel kişiyle canlı kontrollü
deney yapılmadı. Sonraki turda örtüşme ve kalıcı takip deneyleri gereklidir.

Canlı koşuda eski zone pencere mantığı bazı kayıtlara 0 güven yazdı (ID 16, 21):
karar geçmiş pozitif karelerden oluşurken kayıt anındaki güven 0 olabiliyor.
PPE entegrasyonu bu mevcut davranışı değiştirmedi; ayrı zone regresyon turunda
pencere içi güvenin saklanması değerlendirilmeli, eski kayıtlar değiştirilmemeli.

## Eşleştirme ve kurallar

Mevcut boyut/omuz/kalça filtresinden geçen kişiler kullanılır. COCO baş keypoint'leri
baret/kulak koruyucuya, omuz-kalça gövde bölgesi yeleğe, ayak bilekleri ayakkabıya
referans olur. Güvenilir keypoint yoksa kişi kutusunun baş %0–30, gövde %20–75,
ayak %75–100 bölümleri yalnızca mekânsal eşleştirme için kullanılır; bunlar PPE
tespiti veya doğruluk iddiası değildir.

PPE kutu merkezi tam bir kişinin ilgili bölgesindeyse eşleşir. Birden fazla aday,
yanlış verilmiş kişi ID veya düşük güven bulgusu atılır. Aynı kişide güvenilir
pozitif ve negatif ekipman bulgusu çelişirse ihlal üretilmez. Her eksik ekipman
ayrı `Violation` olur; güven değeri açık eksiklik detection skorudur.

Kabul edilen pose kişileri hafif IoU tracker ile eşleştirilir. PPE DB olayları
kamera + oturum kapsamlı kişi ID + ihlal türü başına 30 saniye sınırlanır.
Kayıt öncesinde varsayılan 5 karede 3 negatif gözlem gerekir. Tek karelik
`PPEPipeline.evaluate` çıktıları kanıt adaylarıdır; servis `PPEEventGate` üzerinden
doğrular. Yasak bölgenin mevcut kamera bazlı 5/10 kare mantığı korunur.

## Veritabanı ve etkinleştirme

`prepare_database()` mevcut açılış akışında idempotent, eklemeli migration uygular:
`ihlaller.kamera_id` (nullable FK, kamera silinirse NULL), `person_id` (nullable),
`kamera_ppe_kurallari` tablosu. Eski ihlallerin kamera bilgisi tahminle doldurulmaz.
Mevcut tarih, tür, yüzde confidence, fotoğraf, durum ve açıklama alanları korunur.
UUID dosya eki aynı saniyedeki ayrı ihlallerin fotoğraflarını korur.

Kamera kural satırı `(kamera_id, equipment, aktif, confidence_threshold)` içerir;
anahtar kamera/ekipmandır. Satır yoksa kapalıdır. Backend `CameraRuleCache` ile
başlangıçta ve monoton saate göre 2 saniyelik TTL sonrasında okur; helmet/vest
değişiklikleri yeniden başlatma gerektirmez. Kameralar sayfasında bu iki kural
yetkili yönetici tarafından CSRF korumalı olarak güncellenir. Unsupported sınıflar
formda reddedilir ve runtime cache tarafından da süzülür.

`config/runtime.json`: health yazma 2 s, stale 10 s, PPE refresh 2 s,
son başarılı kuralın maksimum yaşı 30 s, runtime SQLite bekleme süresi 0.1 s.
Kural okumaları ayrı bağlantı kullanır; ihlal yazma bağlantısının timeout'u korunur.
DB kesintisinde son başarılı kurallar 30 saniyeye kadar kullanılır; sonra PPE
değerlendirmesi boş kurallarla devam eder, DB düzelince otomatik toparlanır.
Kural/eşik değişimi eski doğrulama karelerini temizler; cooldown korunur.

`camera_health` kamera başına son AI oturumu, frame/hata zamanları, güvenli hata
kodu ve işlenen FPS tutar. Tek elemanlı kuyruk ve ayrı worker SQLite yazmasını
inference yolundan ayırır. Yeni oturum ilk frame'e kadar unknown; başarılı frame
online; hata/normal kapanış offline. 10 saniyeden eski frame/heartbeat offline
gösterilir. Hiç çalışmamış kameranın ayarı aktif olsa da health unknown kalır.
Eski başarılı frame ve hata zamanları geçmiş bilgi olarak korunur. Panel health'i
sayfa yenilemesinde okur; ham hata metni veya kamera bağlantı sırrı göstermez.
Kamera index/zone ve runtime dosyasındaki süre değişiklikleri servis başlangıcında
okunur; bunlar için yeniden başlatma hâlâ gereklidir.

Canlı doğrulama: `python scripts/verify_runtime_camera.py`, mevcut aktif kamera 0
ve helmet satırı gerektirir. Gerçek AI servisinde 40 saniye çalışır; helmet ayarını
geçici değiştirip finally içinde geri yükler. Gerçek oluşan ihlalleri saklar.
Sonuç: `reports/runtime_camera.json`. Ani süreç sonlandırmasında finally çalışmaz;
rapordaki başlangıç değerleriyle ayarların kontrol edilmesi gerekir.

PPE inference/kural/kayıt hataları loglanır, yasak bölge çalışmaya devam eder.
Migration doğrulama turunda gerçek veritabanına migration uygulandı. Tekrarlanabilir
yedekleme/doğrulama aracı: `python scripts/verify_ppe_migration.py`. Araç SQLite
backup API'siyle `backups/` altında benzersiz yedek alır, eski kayıtları tüm
alanlarıyla karşılaştırır, migration'ı iki defa çalıştırıp şema/veri idempotansını
ve bütünlüğü denetler. Eski tablo yeniden kurma migration'ı gerekiyorsa durur;
otomatik geri yükleme veya veri silme yapmaz. Testler geçici DB kullanır.

## Tracking, confidence ve suppression

Ayarlar `config/tracking.json` üzerinden servis başlangıcında okunur:
`enabled`, `iou_threshold` (0.3), `max_missing_frames` (10), `ppe_window` (5),
`ppe_required` (3), `cooldown_seconds` (30), `state_ttl_seconds` (120).

Tracker kabul edilmiş pose kutularını bire bir, en yüksek IoU önceliğiyle eşler.
Kimlik oturum UUID + artan sayaçtır; yeniden başlatmada veya 10'dan fazla kayıp
kare sonrasında yeni kimlik oluşur. Görünüş modeli/re-identification yoktur.
Örtüşme, hızlı hareket ve başka bir kişinin aynı konumu alması ID değişimine
veya karışmasına neden olabilir. Eski track silinir; olay durumu son negatif
bulgudan 120 saniye sonra temizlenir. Yeni ID geçmiş doğrulamayı devralmaz.

Tracker kapalıysa veya hata verirse her kareye özgü kimlikler döner, detection
ve kamera bazlı zone işleyişi sürer. Varsayılan çok kareli PPE doğrulaması bu
kimliklerle tamamlanamaz; bu durumda PPE DB kaydı beklenmemelidir. Tek kare
moduna geçmek tekrar kayıt ve yanlış alarm riski taşır. Tracker hatası loglanır;
sonraki karelerde tekrar denenir. PPE modeli kapalıyken zone bağımsız çalışır.

`PPEEventGate` bildirim göndermez; DB kaydı başarılı olduktan sonra cooldown
başlar. Farklı kişiler/türler/kameralar bağımsızdır. Başarısız kayıt sonraki
uygun karede yeniden denenir. Süren ihlal 30 saniye sonra yeniden kaydedilebilir;
bu tasarım ihlal başına tek yaşam boyu olay garantisi vermez. Pozitif/eksik/
çelişkili gözlem pencereye negatif kanıt eklemez. Mevcut uygulamada ayrı bir
bildirim rate limiter bulunmuyor; eklenirse bu kapıdan ayrı tutulmalıdır.

Zone geçmişinde 5 negatif kare hâlâ bulunurken 5 güvenli kareyle yeniden
etkinleşme mümkündü: son güvenli karenin başlangıç değeri 0 kaydediliyordu.
Artık aynı doğrulama penceresindeki gerçek person detection skorlarının maksimumu
aktarılır (modelin zone olasılığı değildir). Skor yoksa SQL NULL yazılır;
ana liste ve detay bunu “Mevcut değil” gösterir. 5/10 ve 5 güvenli kare davranışı
aynen korunur; bunun güvenli karede geçmiş olayı yeniden tetikleme özelliği de
korunmuştur. Eski kayıtlar değiştirilmez; yeni şema migration gerekmez.

## Test

`venv/Scripts/python.exe -m unittest discover -s tests -v`

Canlı, üretim DB'sine yazmayan test: `python scripts/verify_tracking_camera.py`.
30 saniye gerçek pose/PPE modeli + tracker + event gate çalıştırır, kayıtları
simüle eder ve `reports/tracking_camera.json` üretir. Görüntü saklamaz.
Fiziksel çıkış/giriş ile detection kaybı sayısal çıktılardan kesin ayrıştırılamaz.
İki fiziksel kişi testi ayrıca gereklidir.
