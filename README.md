# 🎵 Spotify Floating Lyrics

Aplikasi desktop ringan untuk menampilkan lirik lagu yang sedang diputar di Spotify secara *real-time* dalam jendela mengambang (*floating window*) yang selalu berada di atas layar (*always-on-top*). Dibangun menggunakan **Python** dan **PySide6** dengan integrasi **Windows SMTC** (*System Media Transport Controls*) serta **Spotify API**.

## ✨ Fitur Utama

- **Jendela Mengambang (*Floating Overlay*)**: Menampilkan lirik secara transparan tanpa mengganggu aktivitas utama di layar.
- **Sinkronisasi Lirik *Real-Time***: Lirik berjalan otomatis mengikuti durasi dan posisi lagu yang sedang diputar.
- **Deteksi Media Otomatis**: Mendukung pembacaan status pemutaran media melalui Windows SMTC (`smtc_client.py`) dan Spotify API (`spotify_client.py`).
- **Integrasi *System Tray***: Akses cepat untuk mengatur tampilan, menyembunyikan jendela, atau menutup aplikasi langsung dari ikon di *taskbar*.
- **Pengaturan Kustom**: Mendukung konfigurasi tampilan dan perilaku jendela melalui `settings.py`.

## 🛠️ Teknologi yang Digunakan

- **Bahasa**: Python 3.12+
- **Antarmuka GUI**: PySide6 (Qt for Python)
- **Integrasi Media & API**: Spotify Web API, Windows SMTC, penyedia lirik eksternal (`lyrics_provider.py`)

## 📂 Struktur Proyek

```text
spotify-floating-lyrics/
├── main.py               # Entry point utama aplikasi
├── floating_window.py    # Logika dan tampilan jendela mengambang
├── lyrics_widget.py      # Komponen UI untuk merender teks lirik
├── lyrics_provider.py    # Pengambil data lirik lagu
├── lyrics_sync.py        # Mesin sinkronisasi waktu lirik
├── lyric_queue.py        # Manajemen antrean lirik
├── spotify_client.py     # Koneksi ke Spotify API
├── smtc_client.py        # Pembaca status pemutaran Windows SMTC
├── system_tray.py        # Kontrol ikon dan menu di System Tray
├── settings.py           # Konfigurasi dan penyimpanan pengaturan pengguna
├── workers.py            # Background thread agar UI tetap responsif
└── requirements.txt      # Daftar dependensi Python
```

## 🚀 Cara Instalasi & Menjalankan

### 1. Clone Repositori
```bash
git clone https://github.com
cd Spotify-FloatLyrics
```

### 2. Buat dan Aktifkan *Virtual Environment*
```bash
python -m venv .venv
.venv\Scripts\activate
```

### 3. Instal Dependensi
Pastikan file `requirements.txt` tersedia, lalu jalankan:
```bash
pip install -r requirements.txt
```
*(Jika belum ada `requirements.txt`, instal library utama secara manual seperti `pip install PySide6 spotipy python-dotenv winsdk` sesuai kebutuhan proyek).*

### 4. Konfigurasi Environment (`.env`)
Buat file `.env` di folder utama proyek dan masukkan kredensial Spotify Developer Anda (jika menggunakan Spotify API):
```env
SPOTIPY_CLIENT_ID=your_client_id_here
SPOTIPY_CLIENT_SECRET=your_client_secret_here
SPOTIPY_REDIRECT_URI=http://localhost:8888/callback
```

### 5. Jalankan Aplikasi
```bash
python main.py
```

## 📦 Build ke File `.exe` (Opsional)

Untuk mengemas aplikasi menjadi file executable mandiri menggunakan **PyInstaller**:
```bash
pyinstaller --noconsole --onefile main.py
```
Hasil build akan tersedia di dalam folder `dist/main.exe`.

## 📝 Lisensi

Proyek ini dibuat untuk keperluan pembelajaran dan penggunaan pribadi. Silakan kembangkan atau sesuaikan lebih lanjut sesuai kebutuhan Anda.
