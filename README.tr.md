# dlna-cast

[English](README.md) | **Türkçe**

Linux masaüstünüzü DLNA üzerinden akıllı TV'ye sesiyle birlikte yansıtır, isterseniz TV'yi ikinci ekran olarak kullanır. Miracast, Chromecast ya da TV'ye uygulama gerekmez; TV'nin kendi DLNA oynatıcısı kullanılır.

PyGObject ve GStreamer dışında bağımlılığı olmayan küçük bir Python paketi. Ekranı Wayland ekran paylaşımı portalıyla yakalar, GStreamer ile sıkıştırır ve TV'de canlı MPEG-TS akışı olarak oynatır.

- **Ayna modu:** TV ekranlarınızdan birini gösterir.
- **Genişletme modu** (`--extend`): TV ayrı bir sanal monitör olur, pencereleri oraya sürüklersiniz.
- TV'yi ağda kendisi bulur; TV akışı durdurursa ya da bilgisayarın IP'si değişirse kendini toparlar.
- Sabit 60 kare/sn çıkış; NVIDIA (NVENC) veya Intel/AMD (VA-API) donanım kodlama, yedek olarak x264.

**Gecikme yaklaşık 3 saniyedir** (DLNA'da TV tarafı tamponlama). Video, film ve sunum için uygun; TV'de fareyle çalışmak için değil.

## Gereksinimler

- **Wayland** oturumu ve `xdg-desktop-portal` olan bir Linux (KDE Plasma ya da GNOME).
- PipeWire (güncel dağıtımlarda varsayılan).
- Aynı ağda **DLNA/UPnP MediaRenderer** olan bir TV. Samsung UE49KS8500 (Tizen, 2016) ile test edildi. Çoğu Samsung, LG, Sony ve Philips akıllı TV'de bu özellik var; başka modellerle ilgili bildirimler memnuniyetle karşılanır.
- PyGObject ile Python 3.9+, aşağıdaki eklentilerle GStreamer 1.20+.

| | Test edilen |
|---|---|
| Masaüstü | KDE Plasma 6 (Wayland), Fedora |
| Kodlayıcı | NVENC (RTX 2060), x264 |
| TV | Samsung UE49KS8500 |

GNOME, VA-API ve diğer TV'lerin çalışması beklenir ama henüz denenmedi. Genişletme modu sanal monitör destekleyen bir portal ister (KDE Plasma 6'da var, GNOME'da yok).

## Kurulum

Bağımlılıkları kurun, sonra betiği indirin:

**Fedora** (x264 ve AAC için [RPM Fusion](https://rpmfusion.org/Configuration) etkin olmalı):

    sudo dnf install python3-gobject gstreamer1-plugins-base gstreamer1-plugins-good \
        gstreamer1-plugins-bad-free gstreamer1-plugins-ugly gstreamer1-plugin-libav \
        pipewire-gstreamer pulseaudio-utils

**Ubuntu / Debian:**

    sudo apt install python3-gi gir1.2-gstreamer-1.0 gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
        gstreamer1.0-libav gstreamer1.0-pipewire pulseaudio-utils

**Arch:**

    sudo pacman -S python-gobject gst-plugins-base gst-plugins-good gst-plugins-bad \
        gst-plugins-ugly gst-libav gst-plugin-pipewire libpulse

Ardından:

    git clone https://github.com/tamerardall/dlna-cast.git
    cd dlna-cast
    ./dlna-cast.py --list      # TV'niz listede görünmeli

`./dlna-cast.py` doğrudan klasörden çalışır. Bunun yerine bir `dlna-cast` komutu isterseniz, sistemdeki PyGObject'i görecek şekilde kurun:

    pipx install --system-site-packages .

NVIDIA donanım kodlaması için tescilli sürücü gerekir; `nvcodec` eklentisi plugins-bad içindedir. Intel/AMD için `va` eklentisi (plugins-bad) ve ekran kartınızın VA-API sürücüsü gerekir.

## Kullanım

TV'yi açın, sonra:

    ./dlna-cast.py             # ayna
    ./dlna-cast.py --extend    # TV ikinci ekran

İlk çalıştırmada masaüstünüz hangi ekranın paylaşılacağını sorar. Seçim `~/.config/dlna-cast/` içinde saklanır; yeniden seçmek için bu klasörü silin. Ctrl+C ile durdurun (TV'ye de durdur komutu gider).

Birden fazla TV varsa ya da otomatik bulma başarısızsa TV'nin IP'sini verin: `./dlna-cast.py 192.168.1.50`. Neyin bulunduğunu `./dlna-cast.py --list` gösterir.

### Seçenekler

| Seçenek | Varsayılan | Not |
|---|---|---|
| `--extend` | kapalı | TV'yi sanal monitör olarak ekler. KDE'de ekranınızın sağına yerleştirilir. Eski adı `--genislet` de çalışır. |
| `--encoder` | `auto` | `nvenc` (HEVC), `vaapi` (H.264), `x264` (H.264, işlemci). `auto` bu sırayla ilk bulunanı seçer. |
| `--bitrate` | `8000` | kbit/s. Hızlı harekette bloklaşma olursa 10000–12000; TV'nin Wi-Fi'ı zayıfsa düşürün. |
| `--fps` | `60` | TV'ler en fazla 60'ı çözer. |
| `--port` | `8765` | TV'nin akışı okuduğu HTTP portu. |

Çıkış her zaman 1920×1080'dir; farklı boyuttaki ekranlar siyah kenarlarla ölçeklenir.

### Durum satırı

5 saniyede bir:

    fps: source 50/s, output 60/s, longest gap 25 ms, dropped chunks total 0

- **source**: ekrandan gelen yeni kare. Masaüstü yalnızca ekran değişince kare gönderir, durağan ekranda düşük olması normaldir.
- **output**: TV'ye giden kare. Her zaman ~60 olmalı.
- **longest gap**: TV'ye giden veride en uzun duraklama. ~50 ms'yi aşarsa takılma görürsünüz.
- **dropped chunks**: 0 değilse TV (ya da ağ) yetişemiyor demektir.

## Nasıl çalışır

1. Ekran, `xdg-desktop-portal` ile (Wayland'de izinli tek yol) PipeWire akışı olarak yakalanır.
2. GStreamer görüntüyü (HEVC/H.264) ve masaüstü sesini (AAC) MPEG-TS içine kodlar. Canlı, siyah 60 kare/sn bir arka plan `compositor`'ı sürer; böylece ekran durağanken de çıkış sabit hızda akar.
3. 8765 portundaki küçük bir HTTP sunucusu akışı verir.
4. TV'ye UPnP `SetAVTransportURI` + `Play` ile bu adresi oynatması söylenir. Kontrol adresi, SSDP ile bulunan TV'nin aygıt tanımından okunur.

## Güvenlik

HTTP sunucusu `0.0.0.0:8765` üzerinde **kimlik doğrulaması olmadan** dinler. Çalıştığı sürece aynı ağdaki (Tailscale gibi VPN'ler dahil) herkes `http://<ip-adresiniz>:8765/screen.ts` adresinden ekranınızı izleyebilir. Kullanmadığınızda durdurun, güvenilmeyen ağlarda çalıştırmayın.

## Sorun giderme

- **TV bulunamıyor:** TV açık ve aynı ağda/alt ağda mı? Bazı modemler Wi-Fi ile kablo arasında çok noktaya yayını (SSDP) engeller; IP'yi elle verin. `./dlna-cast.py --list` yanıt verenleri gösterir.
- **TV'de görüntü yok:** günlükte `SetAVTransportURI -> 200` olmalı. Başka bir kod TV'nin akışı reddettiği anlamına gelir; günlükle birlikte issue açın. Samsung'da `Play -> 500` zararsızdır (TV kendiliğinden oynamaya başlar).
- **Takılma:** TV'ye `ping` atıp paket kaybına bakın. Bilgisayarda 5 GHz Wi-Fi kullanın, TV'yi modeme yaklaştırın ya da `--bitrate`'i düşürün.
- **Harekette kare hızı düşük:** günlükte hangi kodlayıcının seçildiğine bakın. NVIDIA'da `nvautogpuh265enc` kullanılmalı (`nvenc` bunu seçer); düz `nvh265enc` testlerde canlı kaynağı ~20 kare/sn'ye düşürdü.
- **Genişletme modunda laptop ekranı kapanıyor (KDE):** KWin sanal monitör eklenince bazen iç ekranı kapatan kayıtlı bir düzen uyguluyor. Betik `kscreen-doctor` ile ekranı yeniden açar ve sanal monitörü sağa taşır.
- **Ekran paylaşımı penceresi çıkmıyor:** portal arka ucu (`xdg-desktop-portal-kde` ya da `-gnome`) çalışıyor mu bakın: `busctl --user list | grep portal`.
- **Ses yok:** neyin eksik olduğunu günlük söyler (`pactl`, `pulsesrc` ya da AAC kodlayıcı).

## Bilinen sınırlamalar

- ~3 sn gecikme; DLNA oynatmanın doğası.
- Bu TV ile Miracast denendi ve başarısız oldu (`P2P-GO-NEG-FAILURE`); DLNA daha güvenilir.
- Bazı NVIDIA sürücülerinde NVENC yalnızca HEVC kodluyor (H.264 için `Codec not supported`); bu yüzden `nvenc` HEVC kullanır. Yaklaşık 2016'dan eski TV'ler HEVC oynatamayabilir; o durumda `--encoder x264` kullanın.
- Betik çalışırken TV'nin kendi IP'si değişirse betiği yeniden başlatın.

## Katkı

Hata bildirimleri ve "şu TV'de çalıştı/çalışmadı" bildirimleri çok değerli, bkz. [CONTRIBUTING.md](CONTRIBUTING.md).

## Lisans

[MIT](LICENSE)
