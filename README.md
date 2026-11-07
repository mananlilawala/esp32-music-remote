# ESP32 Music Remote & Synced Lyrics Display

[![ESP32](https://img.shields.io/badge/Hardware-ESP32--WROOM--32-orange.svg)](https://www.espressif.com/)
[![Display](https://img.shields.io/badge/Display-2.8%22%20ILI9341%20TFT%20(320x240)-blue.svg)]()
[![Python](https://img.shields.io/badge/Python-3.11%20%7C%203.12%20%7C%203.13-yellow.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

A standalone smart desktop music remote and live synchronized lyrics display powered by an **ESP32** and a **2.8" ILI9341 touch screen**. Connects over Wi-Fi to a lightweight Windows Python bridge that hooks directly into the Windows System Media Transport Controls (GSMTC)—supporting **Spotify**, **YouTube**, **YouTube Music**, **Apple Music**, **Brave**, **Chrome**, and any Windows media player.

---

## ✨ Features

* **Spotify-Inspired Modern Dark Theme:** Sleek dark graphite palette (`#080808` tone), vibrant green accent (`#1DB954`), high-contrast white lyrics, and rounded pill buttons.
* **Live Synchronized Lyrics:** Fetches time-synced lyrics from [LRCLIB](https://lrclib.net/) in real time, automatically advancing line-by-line as your track plays.
* **Full-Screen Lyrics View:** Tap either the album artwork or the lyrics box to expand into a dedicated 320x240 full-screen lyrics view with a `< BACK` button and 4 lines of lyrical context.
* **Real-Time Album Art:** Displays live 100x100 album art decoded via hardware-friendly baseline JPEG decompression (`TJpg_Decoder`). Includes a fallback vinyl record graphic with rotating center badge when art is unavailable.
* **Universal Multi-Script Transliteration:** The ILI9341 display renders ASCII text. The Python bridge features an algorithmic transliteration engine that automatically romanizes non-Latin scripts on the fly:
  * **Korean (Hangul):** Algorithmic decomposition of all 11,172 syllables (e.g. *사랑해* $\rightarrow$ `saranghae`).
  * **Japanese:** Full Hiragana and Katakana romanization with digraphs (*きゃ* $\rightarrow$ `kya`) and sokuon consonant doubling (*がっこう* $\rightarrow$ `gakkou`).
  * **Cyrillic:** Full phonetic transliteration (*Привет* $\rightarrow$ `Privet`).
  * **Thai, Arabic & Devanagari (Hindi):** Consonant and vowel phonetic conversion (*नमस्ते* $\rightarrow$ `namaste`).
* **Zero-Lag Media Controls:** Play/Pause, Next Track, Previous Track, Seek via progress bar tap, and Volume Up/Down with smooth button holding acceleration over persistent HTTP/1.1 keep-alive sockets.
* **Intuitive Touch & Gestures:**
  * Swipe **Left** in upper area $\rightarrow$ Next track.
  * Swipe **Right** in upper area $\rightarrow$ Previous track.
  * Tap on **Progress Bar** $\rightarrow$ Instant seek.
  * Tap on **Album Art / Lyrics** $\rightarrow$ Toggle Full-Screen Lyrics.
* **Dual-Core FreeRTOS Architecture:** 
  * **Core 0:** Background networking task polls media status and downloads JPEG artwork asynchronously.
  * **Core 1:** Dedicated UI rendering loop with double-buffered canvas scrollers for flicker-free 60 FPS text scrolling.

---

## 🛠️ Hardware Requirements

| Component | Description |
| :--- | :--- |
| **Microcontroller** | ESP32-WROOM-32 / ESP32-D0WD-V3 Dev Board |
| **Display** | 2.8" SPI TFT LCD (ILI9341 driver, 320x240 resolution) with XPT2046 Resistive Touch Screen |
| **Power** | Standard 5V USB cable |

### Pinout & Wiring

Both the ILI9341 display and the XPT2046 touch controller share the ESP32's hardware VSPI bus (pins 18, 19, 23):

```
       ESP32 Pin          ILI9341 / XPT2046 Module Pin
       ---------          ----------------------------
         3V3      ------>   VCC, LED (Backlight)
         GND      ------>   GND
         D15      ------>   CS (TFT Chip Select)
         D2       ------>   DC (TFT Data / Command)
         D4       ------>   RESET (TFT Reset)
         D18      ------>   SCK / CLK (SPI Clock - shared)
         D23      ------>   MOSI / SDI / T_DIN (SPI Data In - shared)
         D19      ------>   MISO / SDO / T_DO (SPI Data Out - shared)
         D5       ------>   T_CS (Touch Chip Select)
```

---

## 🚀 Getting Started

### 1. PC Bridge Setup (Windows)

1. Clone this repository:
   ```bash
   git clone https://github.com/mananlilawala/esp32-music-remote.git
   cd esp32-music-remote
   ```

2. Install Python dependencies (requires 64-bit Python 3.11, 3.12, or 3.13):
   ```powershell
   py -m pip install -r requirements.txt
   ```

3. Start the bridge:
   ```powershell
   py pc_music_bridge.py
   ```
   The console will output your PC's LAN IPv4 address (e.g. `192.168.1.100` or `172.20.10.3`). Keep note of this IP.

> **Windows Firewall Note:** When prompted, allow Python through Windows Defender Firewall on Private networks.

---

### 2. ESP32 Firmware Setup

1. Open the Arduino IDE.
2. In **Tools > Board**, select **ESP32 Dev Module** (or `esp32:esp32:esp32`).
3. Install the required libraries via **Sketch > Include Library > Manage Libraries...**:
   * `Adafruit ILI9341`
   * `Adafruit GFX Library`
   * `XPT2046_Touchscreen` (by Paul Stoffregen)
   * `TJpg_Decoder` (by Bodmer)
4. Configure your network credentials:
   * Inside the `ESP32_Music_Remote/` folder, copy `config.example.h` to `config.h`:
     ```bash
     cp ESP32_Music_Remote/config.example.h ESP32_Music_Remote/config.h
     ```
   * Open `config.h` and enter your Wi-Fi SSID, password, and the PC IP from Step 1:
     ```cpp
     const char *WIFI_SSID = "Your_WiFi_SSID";
     const char *WIFI_PASS = "Your_WiFi_Password";
     const char *PC_HOST   = "192.168.1.100"; // IP printed by pc_music_bridge.py
     const int   PC_PORT   = 8765;
     ```
     *(Note: `config.h` is git-ignored so your credentials remain private).*
5. Connect your ESP32 via USB and click **Upload**.

---

## 📱 User Interface & Touch Controls

### Normal View (Album Art + Live Lyrics)

```
┌─────────────────────────────────────────────────────────┐
│ NOW PLAYING                                         ••• │  <- Status & Wi-Fi
│                                                         │
│ ┌───────────────┐  Track Title (Smooth Scroll)          │
│ │               │  Artist Name (Smooth Scroll)          │
│ │   100 x 100   │  ───────────────────────────────────  │
│ │   Album Art   │  Live Current Lyric (Bright White)    │
│ │               │  Next Lyric (Slate Gray)              │
│ └───────────────┘                                       │
│                                                         │
│ ────●────────────────────────────────────────────────── │  <- Seekable Progress Bar
│ 0:42                                               3:15 │
│                                                         │
│   [VOL-]     [ |◀ ]       [ ▶ / ❚❚ ]       [ ▶| ]    [VOL+]  │  <- Rounded Pill Buttons
└─────────────────────────────────────────────────────────┘
```

* **Play / Pause:** Tap center button.
* **Volume:** Tap `VOL-` / `VOL+` to adjust; hold down to repeat.
* **Skip / Previous:** Tap `|◀` / `▶|`, or swipe left/right across the top area.
* **Seek:** Tap anywhere along the progress bar.
* **Expand Lyrics:** Tap either the album artwork or the lyrics text box.

### Full-Screen Lyrics View

* Displays expanded 4-line context with large white typography and a green vertical playback marker.
* Tap **`< BACK`** on the top-left (or swipe right) to return to the player.

---

## 📡 HTTP Protocol Reference

The Python bridge runs an HTTP server on port `8765`:

| Method | Endpoint | Description | Response |
| :--- | :--- | :--- | :--- |
| `GET` | `/health` | Bridge health check | `OK` |
| `GET` | `/status` | Pipe-delimited live track state | `T\|title\|artist\|pos\|dur\|playing\|art_id\|cur_lyric\|next_lyric\|prev_lyric` |
| `GET` | `/art.jpg` | Current track 100x100 JPEG thumbnail | Binary JPEG image |
| `GET` | `/cmd?c=PLAY` | Toggle Play / Pause | `OK` |
| `GET` | `/cmd?c=NEXT` | Skip to next track | `OK` |
| `GET` | `/cmd?c=PREV` | Skip to previous track | `OK` |
| `GET` | `/cmd?c=VOLUP` | Increase Windows volume | `OK` |
| `GET` | `/cmd?c=VOLDN` | Decrease Windows volume | `OK` |
| `GET` | `/cmd?c=SEEK:<s>` | Seek to position in seconds | `OK` |

---

## 📄 License

This project is licensed under the [MIT License](LICENSE) - feel free to modify and build upon it!
