/*
  ESP32 + 2.8" ILI9341 touch display: YouTube Music remote (Wi-Fi)

  Shows album art, title, artist, progress, and live synchronized lyrics
  from the music playing on your PC, and controls it from the touch screen.

  Needs pc_music_bridge.py running on the PC (same Wi-Fi network).
  The ESP32 only needs USB power after uploading.

  Controls:
    Tap buttons:  VOL-  PREV  PLAY/PAUSE  NEXT  VOL+
    Hold VOL- / VOL+ to repeat
    Tap progress bar to seek
    Swipe left in upper area  = next track
    Swipe right in upper area = previous track
    Tap album art = toggle between Album Art & Fullscreen Lyrics

  Libraries (Library Manager):
    - Adafruit ILI9341
    - Adafruit GFX Library
    - XPT2046_Touchscreen (Paul Stoffregen)
    - TJpg_Decoder (Bodmer)

  Wiring:
    Display: CS->D15, DC->D2, RESET->D4, SDI(MOSI)->D23, SCK->D18,
             VCC->3V3, GND->GND, LED->3V3
    Touch:   T_CS->D5, T_CLK->D18, T_DIN->D23, T_OUT->D19
*/

#include <SPI.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ILI9341.h>
#include <XPT2046_Touchscreen.h>
#include <TJpg_Decoder.h>
#include "soc/soc.h"
#include "soc/rtc_cntl_reg.h"

// Defined up here so the Arduino IDE's auto-generated function declarations can see it
struct Scroller { int off = 0; unsigned long hold = 0; };

// ================= YOUR SETTINGS =================
// You can create a "config.h" file in this directory to keep your private
// credentials out of version control (see config.example.h), or edit below:
#if __has_include("config.h")
  #include "config.h"
#else
  const char *WIFI_SSID     = "YOUR_WIFI_SSID";     // 2.4 GHz Wi-Fi / hotspot
  const char *WIFI_SSID_ALT = "YOUR_WIFI_SSID";     // Fallback SSID or same as above
  const char *WIFI_PASS     = "YOUR_WIFI_PASSWORD";
  const char *PC_HOST       = "192.168.1.100";      // replace with the PC IP printed by Python
  const int   PC_PORT       = 8765;
#endif
// =================================================

#define TFT_CS   15
#define TFT_DC   2
#define TFT_RST  4
#define TOUCH_CS 5

const bool DEBUG_TOUCH = false;

Adafruit_ILI9341 tft = Adafruit_ILI9341(TFT_CS, TFT_DC, TFT_RST);
XPT2046_Touchscreen ts(TOUCH_CS);

// Off-screen canvases for flicker-free scrolling text.
GFXcanvas16 *titleCv = nullptr;
GFXcanvas16 *artistCv = nullptr;

// ---------- Modern Dark Color Palette ----------
const uint16_t C_BG      = 0x0841;   // Very dark gray background (gives depth vs pure black)
const uint16_t C_BLACK   = 0x0000;   // Pure black (for canvas fills, contrast)
const uint16_t C_WHITE   = 0xFFFF;   // Bright white (current lyrics, important text)
const uint16_t C_ACCENT  = 0x2589;   // Spotify-style green ~RGB(30,178,72)
const uint16_t C_ACCDIM  = 0x1344;   // Dimmer accent for subtle indicators
const uint16_t C_LYGREY  = 0x8410;   // Medium gray (next/prev lyrics)
const uint16_t C_BTNGREY = 0x4208;   // Button outlines, subtle UI elements
const uint16_t C_TIMEGR  = 0x6B4D;   // Time text gray
const uint16_t C_SEPLINE = 0x2104;   // Very subtle separator lines
const uint16_t C_DIMTXT  = 0x3186;   // Very dim text (hints)
const uint16_t C_VINYL   = 0x1082;   // Vinyl record dark fill

// Legacy aliases for readability in unchanged code paths
const uint16_t C_RED     = 0xF800;

// ---------- Touch calibration (Landscape rotation 1) ----------
bool calSwap = false;
long calX1 = 3800, calX2 = 300;   // raw values at screen x = 30 and x = 290
long calY1 = 3800, calY2 = 300;   // raw values at screen y = 30 and y = 210

// Soft touch threshold: 220 provides a soft, responsive touch without noise glitches (original library was 400)
const int Z_MIN = 220;

// ---------- Layout Constants ----------
// Album art
const int ART_X = 10, ART_Y = 18, ART_W = 100, ART_H = 100;
// Text area (right of art)
const int TXT_X = 118, TXT_Y = 20;
const int TXT_W = 196;
// Lyrics in normal view (right of art, below title/artist)
const int LYR_X = 118, LYR_Y = 72, LYR_W = 196, LYR_H = 50;
// Progress bar
const int BAR_X = 16, BAR_Y = 148, BAR_W = 288, BAR_H = 3;
// Time labels
const int TIME_Y = 157;
// Button row
const int BTN_Y = 210;
const int BTN_X[5] = {32, 96, 160, 224, 288};

// =====================================================
//  Shared state between the network task (core 0) and the UI (core 1)
// =====================================================
SemaphoreHandle_t mtx;
QueueHandle_t cmdQ;
struct Cmd { char s[24]; };

String gTitle = "Waiting for PC...";
String gArtist = "Starting...";
String gCurLyric = "";
String gNextLyric = "";
String gPrevLyric = "";
int  gPos = 0, gDur = 0;
bool gPlaying = false;
unsigned long gRx = 0;
bool gUpdated = false;

uint8_t *gArtBuf = nullptr;
size_t   gArtLen = 0;
bool     gArtNew = false;

volatile bool wifiOk = false;

// =====================================================
//  UI-owned state
// =====================================================
String title  = "Waiting for PC...";
String artist = "Starting...";
String curLyric = "";
String nextLyric = "";
String prevLyric = "";
int  posSec = 0, durSec = 0;
bool playing = false;
bool connected = false;
bool lyricsView = false;
unsigned long lastRx = 0;
unsigned long posBaseMs = 0;

Scroller titleSc, artistSc;

// Touch tracking & debouncing
int rawX = 0, rawY = 0, rawZ = 0;
bool touching = false;
int consecTouch = 0;
int startX = 0, startY = 0, lastX = 0, lastY = 0;
unsigned long lastTouchMs = 0, touchStartMs = 0, lastRepeat = 0, lastDbg = 0;
int initialBtn = -1;
int holdBtn = -1;
bool repeating = false;
int pressedBtn = -1;
unsigned long pressedUntil = 0;

unsigned long lastScroll = 0, lastProgressCheck = 0;
int lastDrawnEst = -1;
int lastLabelCode = -1;

// Track what was last drawn for dirty-rect approach
String lastDrawnCurLyric = "";
String lastDrawnNextLyric = "";
String lastDrawnPrevLyric = "";

// =====================================================
//  Drawing helpers
// =====================================================
void textAt(const char *s, int x, int y, uint8_t size, uint16_t fg, uint16_t bg) {
  tft.setTextSize(size);
  tft.setTextColor(fg, bg);
  tft.setCursor(x, y);
  tft.print(s);
}

void formatTime(int s, char *out, size_t n) {
  if (s < 0) s = 0;
  snprintf(out, n, "%d:%02d", s / 60, s % 60);
}

// Clean word wrapping helper so words are never cut in half
void drawWrappedText(const String &text, int x, int y, int maxW, int maxLines, uint8_t size, uint16_t color, uint16_t bg) {
  tft.setTextSize(size);
  tft.setTextColor(color, bg);
  int charW = 6 * size;
  int lineH = 10 * size;
  int maxCharsPerLine = maxW / charW;
  if (maxCharsPerLine <= 0) return;

  int curLine = 0;
  int startIdx = 0;
  int len = text.length();

  while (startIdx < len && curLine < maxLines) {
    if (len - startIdx <= maxCharsPerLine) {
      tft.setCursor(x, y + curLine * lineH);
      String sub = text.substring(startIdx);
      tft.print(sub);
      // Pad remaining line with spaces to overwrite old text
      int pad = maxCharsPerLine - (len - startIdx);
      for (int p = 0; p < pad; p++) tft.print(' ');
      break;
    }
    int endIdx = startIdx + maxCharsPerLine;
    int spaceIdx = text.lastIndexOf(' ', endIdx);
    if (spaceIdx <= startIdx) {
      spaceIdx = endIdx;
    }
    tft.setCursor(x, y + curLine * lineH);
    tft.print(text.substring(startIdx, spaceIdx));
    // Pad to end of line
    int charsDrawn = spaceIdx - startIdx;
    for (int p = charsDrawn; p < maxCharsPerLine; p++) tft.print(' ');
    curLine++;
    startIdx = (spaceIdx < len && text.charAt(spaceIdx) == ' ') ? spaceIdx + 1 : spaceIdx;
  }
}

// Rounded rectangle (pill) button helper
void drawPill(int cx, int cy, int w, int h, uint16_t fillColor, uint16_t borderColor, bool filled) {
  int r = h / 2;
  if (filled) {
    tft.fillRoundRect(cx - w / 2, cy - h / 2, w, h, r, fillColor);
  } else {
    tft.fillRoundRect(cx - w / 2, cy - h / 2, w, h, r, C_BG);
    tft.drawRoundRect(cx - w / 2, cy - h / 2, w, h, r, borderColor);
  }
}

// WiFi signal indicator: small dots in top-right
void drawWifiIndicator() {
  int baseX = 300, baseY = 6;
  // Clear the area first
  tft.fillRect(baseX - 2, baseY - 2, 22, 10, C_BG);
  if (wifiOk) {
    // 3 green dots = connected
    tft.fillCircle(baseX, baseY + 3, 2, C_ACCENT);
    tft.fillCircle(baseX + 7, baseY + 3, 2, C_ACCENT);
    tft.fillCircle(baseX + 14, baseY + 3, 2, C_ACCENT);
  } else {
    // 3 dim dots = disconnected
    tft.fillCircle(baseX, baseY + 3, 2, C_BTNGREY);
    tft.fillCircle(baseX + 7, baseY + 3, 2, C_BTNGREY);
    tft.fillCircle(baseX + 14, baseY + 3, 2, C_DIMTXT);
  }
}

// Modern vinyl record placeholder (minimal design)
void drawArt() {
  // Dark rounded rect background
  tft.fillRoundRect(ART_X, ART_Y, ART_W, ART_H, 4, C_BG);
  tft.drawRoundRect(ART_X - 1, ART_Y - 1, ART_W + 2, ART_H + 2, 4, C_BTNGREY);

  int cx = ART_X + ART_W / 2;
  int cy = ART_Y + ART_H / 2;

  // Vinyl record: subtle concentric rings
  tft.fillCircle(cx, cy, 42, C_VINYL);
  tft.drawCircle(cx, cy, 36, C_BTNGREY);
  tft.drawCircle(cx, cy, 26, C_BTNGREY);
  tft.drawCircle(cx, cy, 16, C_BTNGREY);
  // Center dot in accent color
  tft.fillCircle(cx, cy, 8, C_ACCENT);
  tft.fillCircle(cx, cy, 2, C_BG);
}

// JPEG decoder callback: draws each block of pixels on the display
bool tftOutput(int16_t x, int16_t y, uint16_t w, uint16_t h, uint16_t *bitmap) {
  if (y >= tft.height()) return false;
  tft.drawRGBBitmap(x, y, bitmap, w, h);
  return true;
}

bool drawArtJpeg(uint8_t *buf, size_t len) {
  // Draw JPEG directly without clearing first (avoids flash)
  TJpgDec.setJpgScale(1);
  TJpgDec.setSwapBytes(false);
  TJpgDec.setCallback(tftOutput);
  uint16_t r = TJpgDec.drawJpg(ART_X, ART_Y, buf, len);
  if (r == 0) {
    // Draw 1px border framing the art
    tft.drawRect(ART_X - 1, ART_Y - 1, ART_W + 2, ART_H + 2, C_BTNGREY);
    return true;
  }
  return false;
}

int labelCode() {
  if (!wifiOk)     return 0;
  if (!connected)  return 1;
  return playing ? 2 : 3;
}

void drawLabel() {
  int code = labelCode();
  lastLabelCode = code;
  if (lyricsView) return;
  const char *s = code == 0 ? "CONNECTING WIFI..."
                : code == 1 ? "NO PC CONNECTED"
                : code == 2 ? "NOW PLAYING" : "PAUSED";
  uint16_t col = (code == 0 || code == 1) ? C_BTNGREY : C_ACCENT;
  // Status bar at very top
  tft.fillRect(0, 0, 295, 14, C_BG);
  textAt(s, 10, 3, 1, col, C_BG);
  drawWifiIndicator();
}

void drawFullscreenLyrics() {
  tft.fillScreen(C_BG);

  // Top header (y: 0..30)
  // Back button pill: center at cx=38, cy=16, w=60, h=22
  drawPill(38, 16, 60, 22, C_BG, C_ACCENT, false);
  textAt("< BACK", 15, 12, 1, C_WHITE, C_BG);

  // Song title and artist next to back button
  String hdr = title;
  if (artist.length() > 0 && artist != "Starting...") hdr += " - " + artist;
  if (hdr.length() > 34) hdr = hdr.substring(0, 31) + "...";
  textAt(hdr.c_str(), 75, 12, 1, C_LYGREY, C_BG);

  drawWifiIndicator();
  tft.drawFastHLine(8, 30, 304, C_SEPLINE);

  // Lyrics Area (y: 35..215)
  if (!connected) {
    textAt("NO PC CONNECTED", 100, 100, 1, C_BTNGREY, C_BG);
    return;
  }

  // Previous lyric
  if (prevLyric.length() > 0) {
    drawWrappedText(prevLyric, 16, 40, 288, 2, 1, C_DIMTXT, C_BG);
  }

  // Current lyric (bright white with accent bar)
  if (curLyric.length() > 0) {
    tft.fillRect(10, 80, 3, 24, C_ACCENT);
    drawWrappedText(curLyric, 20, 80, 284, 3, 1, C_WHITE, C_BG);
  } else if (playing) {
    textAt("~ ... ~", 140, 80, 1, C_DIMTXT, C_BG);
  }

  // Next lyric
  if (nextLyric.length() > 0) {
    drawWrappedText(nextLyric, 16, 135, 288, 2, 1, C_LYGREY, C_BG);
  }

  // Bottom mini progress track at y: 220
  int est = estimatedPos();
  int fillW = 0;
  if (durSec > 0) fillW = (int)((long)288 * est / durSec);
  fillW = constrain(fillW, 0, 288);
  tft.fillRect(16, 220, 288, 2, C_BTNGREY);
  if (fillW > 0) tft.fillRect(16, 220, fillW, 2, C_ACCENT);

  char cur[12], tot[12];
  formatTime(est, cur, sizeof(cur));
  if (durSec > 0) formatTime(durSec, tot, sizeof(tot));
  else snprintf(tot, sizeof(tot), "--:--");
  textAt(cur, 16, 226, 1, C_DIMTXT, C_BG);
  textAt(tot, 304 - 6 * (int)strlen(tot), 226, 1, C_DIMTXT, C_BG);
}

void drawLyrics() {
  if (lyricsView) {
    drawFullscreenLyrics();
  } else {
    // ============ Normal view: lyrics right of album art ============
    // Clear lyrics area
    tft.fillRect(LYR_X, LYR_Y, LYR_W, LYR_H, C_BG);
    // Subtle separator above lyrics
    tft.drawFastHLine(LYR_X, LYR_Y, LYR_W - 4, C_SEPLINE);

    if (!connected) {
      textAt("NO PC CONNECTED", LYR_X + 2, LYR_Y + 16, 1, C_BTNGREY, C_BG);
      lastDrawnCurLyric = "";
      lastDrawnNextLyric = "";
      return;
    }

    if (curLyric.length() == 0 && nextLyric.length() == 0) {
      if (playing) {
        textAt("~ ... ~", LYR_X + 40, LYR_Y + 16, 1, C_DIMTXT, C_BG);
      }
      lastDrawnCurLyric = "";
      lastDrawnNextLyric = "";
      return;
    }

    int yOffset = LYR_Y + 4;
    if (curLyric.length() > 0) {
      drawWrappedText(curLyric, LYR_X + 2, yOffset, LYR_W - 4, 2, 1, C_WHITE, C_BG);
      yOffset += (curLyric.length() <= 30) ? 13 : 22;
    }

    if (nextLyric.length() > 0 && yOffset <= LYR_Y + 38) {
      int maxLines = (yOffset <= LYR_Y + 18) ? 2 : 1;
      drawWrappedText(nextLyric, LYR_X + 2, yOffset, LYR_W - 4, maxLines, 1, C_LYGREY, C_BG);
    }

    lastDrawnCurLyric = curLyric;
    lastDrawnNextLyric = nextLyric;
  }
}

void drawButton(int i, bool pressed) {
  int cx = BTN_X[i], cy = BTN_Y;

  if (i == 2) {
    // ---- Play/Pause: large filled accent pill ----
    int w = 52, h = 28;
    uint16_t fill = pressed ? C_ACCDIM : C_ACCENT;
    drawPill(cx, cy, w, h, fill, fill, true);
    if (playing) {
      // Pause bars (dark on accent)
      tft.fillRect(cx - 6, cy - 8, 4, 16, C_BG);
      tft.fillRect(cx + 3, cy - 8, 4, 16, C_BG);
    } else {
      // Play triangle (dark on accent)
      tft.fillTriangle(cx - 5, cy - 9, cx - 5, cy + 9, cx + 9, cy, C_BG);
    }
    return;
  }

  if (i == 0 || i == 4) {
    // ---- Vol-/Vol+: small outlined pills ----
    int w = 36, h = 22;
    tft.fillRoundRect(cx - w / 2 - 1, cy - h / 2 - 1, w + 2, h + 2, h / 2 + 1, C_BG);
    if (pressed) {
      drawPill(cx, cy, w, h, C_BTNGREY, C_BTNGREY, true);
    } else {
      drawPill(cx, cy, w, h, C_BG, C_BTNGREY, false);
    }
    const char *label = (i == 0) ? "-" : "+";
    uint16_t txtCol = pressed ? C_WHITE : C_LYGREY;
    textAt(label, cx - 5, cy - 7, 2, txtCol, pressed ? C_BTNGREY : C_BG);
    return;
  }

  // ---- Prev/Next: medium outlined pills ----
  {
    int w = 44, h = 24;
    tft.fillRoundRect(cx - w / 2 - 1, cy - h / 2 - 1, w + 2, h + 2, h / 2 + 1, C_BG);
    if (pressed) {
      drawPill(cx, cy, w, h, C_BTNGREY, C_BTNGREY, true);
    } else {
      drawPill(cx, cy, w, h, C_BG, C_BTNGREY, false);
    }
    uint16_t triCol = pressed ? C_WHITE : C_LYGREY;
    if (i == 1) {
      // Prev: <<| (two small triangles + bar)
      tft.fillTriangle(cx + 4, cy - 6, cx + 4, cy + 6, cx - 4, cy, triCol);
      tft.fillRect(cx - 7, cy - 6, 2, 13, triCol);
    } else {
      // Next: |>> (bar + two small triangles)
      tft.fillTriangle(cx - 4, cy - 6, cx - 4, cy + 6, cx + 4, cy, triCol);
      tft.fillRect(cx + 5, cy - 6, 2, 13, triCol);
    }
  }
}

void drawAllButtons() {
  for (int i = 0; i < 5; i++) drawButton(i, false);
}

int estimatedPos() {
  int est = posSec;
  if (playing && connected) est += (millis() - posBaseMs) / 1000;
  if (durSec > 0 && est > durSec) est = durSec;
  return est;
}

void drawProgress() {
  int est = estimatedPos();
  lastDrawnEst = est;

  int fillW = 0;
  if (durSec > 0) fillW = (int)((long)BAR_W * est / durSec);
  fillW = constrain(fillW, 0, BAR_W);

  // Clear bar area (only the 9px height needed for track + thumb)
  tft.fillRect(BAR_X - 5, BAR_Y - 4, BAR_W + 10, 9, C_BG);

  // Track (thin 3px rounded bar)
  tft.fillRoundRect(BAR_X, BAR_Y, BAR_W, BAR_H, 1, C_BTNGREY);
  // Filled portion in accent
  if (fillW > 0) tft.fillRoundRect(BAR_X, BAR_Y, fillW, BAR_H, 1, C_ACCENT);
  // Small circle indicator at current position
  tft.fillCircle(BAR_X + fillW, BAR_Y + 1, 4, C_WHITE);

  // Time labels below progress bar
  char cur[12], tot[12];
  formatTime(est, cur, sizeof(cur));
  if (durSec > 0) formatTime(durSec, tot, sizeof(tot));
  else            snprintf(tot, sizeof(tot), "--:--");

  // Left-aligned current time
  // Use fixed width format to prevent flicker: pad with spaces
  char curPad[12];
  snprintf(curPad, sizeof(curPad), "%-5s", cur);
  textAt(curPad, BAR_X, TIME_Y, 1, C_TIMEGR, C_BG);

  // Right-aligned total time
  char totPad[12];
  snprintf(totPad, sizeof(totPad), "%5s", tot);
  textAt(totPad, BAR_X + BAR_W - 30, TIME_Y, 1, C_TIMEGR, C_BG);
}

void drawScroller(GFXcanvas16 &cv, Scroller &sc, int x, int y,
                  const String &s, uint8_t size, uint16_t color) {
  cv.fillScreen(C_BG);
  cv.setTextWrap(false);
  cv.setTextSize(size);
  cv.setTextColor(color);

  int16_t x1, y1;
  uint16_t w, h;
  cv.getTextBounds(s.c_str(), 0, 0, &x1, &y1, &w, &h);
  int ty = (cv.height() - 8 * size) / 2;

  if ((int)w <= cv.width()) {
    cv.setCursor(0, ty);
    cv.print(s);
  } else {
    const int gap = 40;
    cv.setCursor(-sc.off, ty);
    cv.print(s);
    cv.setCursor(-sc.off + (int)w + gap, ty);
    cv.print(s);
    if (millis() >= sc.hold) {
      sc.off += 2;
      if (sc.off >= (int)w + gap) {
        sc.off = 0;
        sc.hold = millis() + 1200;
      }
    }
  }
  tft.drawRGBBitmap(x, y, cv.getBuffer(), cv.width(), cv.height());
}

void drawScreen() {
  tft.fillScreen(C_BG);
  if (lyricsView) {
    drawFullscreenLyrics();
  } else {
    drawLabel();
    drawArt();
    drawLyrics();
    drawAllButtons();
    drawProgress();
  }
}

// =====================================================
//  Touch reading with noise rejection & 2-sample validation
// =====================================================
bool readTouch(int &x, int &y) {
  if (!ts.touched()) return false;
  TS_Point p1 = ts.getPoint();
  if (p1.z < Z_MIN) return false;
  if (p1.x < 30 || p1.x > 4060 || p1.y < 30 || p1.y > 4060) return false;

  // Validate with a second sample to eliminate SPI bus noise
  delayMicroseconds(80);
  TS_Point p2 = ts.getPoint();
  if (p2.z < Z_MIN) return false;
  if (abs(p1.x - p2.x) > 350 || abs(p1.y - p2.y) > 350) return false;

  rawX = (p1.x + p2.x) / 2;
  rawY = (p1.y + p2.y) / 2;
  rawZ = (p1.z + p2.z) / 2;

  long rx = calSwap ? rawY : rawX;
  long ry = calSwap ? rawX : rawY;

  if (calX2 == calX1 || calY2 == calY1) return false;

  int calcX = 30 + (int)((rx - calX1) * 260 / (calX2 - calX1));
  int calcY = 30 + (int)((ry - calY1) * 180 / (calY2 - calY1));

  x = constrain(calcX, 0, 319);
  y = constrain(calcY, 0, 239);
  return true;
}

// =====================================================
//  Network task (runs on core 0, never touches the display)
// =====================================================
int fetchedArtId = -1;
volatile int gNeedArtId = -1;

void sendCmdHttp(const char *c) {
  if (WiFi.status() != WL_CONNECTED) return;

  static HTTPClient httpCmd;
  static WiFiClient clientCmd;
  static bool cmdInit = false;

  String url = String("http://") + PC_HOST + ":" + PC_PORT + "/cmd?c=" + c;

  if (!cmdInit) {
    httpCmd.setReuse(true);
    httpCmd.setConnectTimeout(1200);
    httpCmd.setTimeout(2000);
    cmdInit = true;
  }

  if (httpCmd.begin(clientCmd, url)) {
    int code = httpCmd.GET();
    if (code < 200 || code >= 300) {
      Serial.printf("[HTTP] Command %s returned %d\n", c, code);
      httpCmd.end();
    }
  } else {
    httpCmd.end();
  }
}

bool fetchArt(int id) {
  if (WiFi.status() != WL_CONNECTED) return false;

  HTTPClient http;
  WiFiClient client;
  String url = String("http://") + PC_HOST + ":" + PC_PORT + "/art.jpg";
  http.setConnectTimeout(1800);
  http.setTimeout(4500);

  uint8_t *buf = nullptr;
  size_t len = 0;

  if (!http.begin(client, url)) {
    Serial.println("[HTTP] Could not start album-art request");
    return false;
  }

  int code = http.GET();
  if (code == 200) {
    int total = http.getSize();
    if (total > 0 && total <= 60000) {
      buf = (uint8_t *)malloc((size_t)total);
      if (buf) {
        WiFiClient *stream = http.getStreamPtr();
        size_t received = 0;
        unsigned long started = millis();
        while (received < (size_t)total && millis() - started < 4500) {
          int availableBytes = stream->available();
          if (availableBytes > 0) {
            size_t wanted = (size_t)total - received;
            if ((size_t)availableBytes < wanted) wanted = (size_t)availableBytes;
            size_t count = stream->readBytes(buf + received, wanted);
            if (count == 0) break;
            received += count;
          } else if (!stream->connected()) {
            break;
          } else {
            vTaskDelay(2 / portTICK_PERIOD_MS);
          }
        }
        if (received == (size_t)total) len = received;
        else { free(buf); buf = nullptr; }
      }
    }
  } else {
    Serial.printf("[HTTP] Album-art request returned %d\n", code);
  }
  http.end();

  // Do not mark the art ID as fetched on failure. The next status poll retries.
  if (!buf || len == 0) return false;

  xSemaphoreTake(mtx, portMAX_DELAY);
  if (gArtBuf) free(gArtBuf);
  gArtBuf = buf;
  gArtLen = len;
  gArtNew = true;
  xSemaphoreGive(mtx);

  fetchedArtId = id;
  Serial.printf("[ART] Received %u bytes\n", (unsigned int)len);
  return true;
}

// Body format: T|title|artist|pos|dur|playing|artId|curLyric|nextLyric|prevLyric
void parseStatus(const String &l) {
  if (!l.startsWith("T|")) return;
  int p1 = l.indexOf('|');
  int p2 = l.indexOf('|', p1 + 1);
  int p3 = l.indexOf('|', p2 + 1);
  int p4 = l.indexOf('|', p3 + 1);
  int p5 = l.indexOf('|', p4 + 1);
  int p6 = l.indexOf('|', p5 + 1);
  if (p1 < 0 || p2 < 0 || p3 < 0 || p4 < 0 || p5 < 0 || p6 < 0) return;

  String t = l.substring(p1 + 1, p2);
  String a = l.substring(p2 + 1, p3);
  int pos  = l.substring(p3 + 1, p4).toInt();
  int dur  = l.substring(p4 + 1, p5).toInt();
  bool pl  = l.substring(p5 + 1, p6).toInt() == 1;

  int p7 = l.indexOf('|', p6 + 1);
  int artId = 0;
  String curL = "";
  String nxtL = "";
  String prvL = "";

  if (p7 < 0) {
    artId = l.substring(p6 + 1).toInt();
  } else {
    artId = l.substring(p6 + 1, p7).toInt();
    int p8 = l.indexOf('|', p7 + 1);
    if (p8 < 0) {
      curL = l.substring(p7 + 1);
    } else {
      curL = l.substring(p7 + 1, p8);
      int p9 = l.indexOf('|', p8 + 1);
      if (p9 < 0) {
        nxtL = l.substring(p8 + 1);
      } else {
        nxtL = l.substring(p8 + 1, p9);
        prvL = l.substring(p9 + 1);
      }
    }
  }
  curL.trim();
  nxtL.trim();
  prvL.trim();

  if (t.length() == 0) t = "Nothing playing";

  xSemaphoreTake(mtx, portMAX_DELAY);
  gTitle = t;
  gArtist = a;
  gPos = pos;
  gDur = dur;
  gPlaying = pl;
  gCurLyric = curL;
  gNextLyric = nxtL;
  gPrevLyric = prvL;
  gRx = millis();
  gUpdated = true;
  xSemaphoreGive(mtx);

  if (artId != fetchedArtId) {
    if (artId == 0) {
      xSemaphoreTake(mtx, portMAX_DELAY);
      if (gArtBuf) { free(gArtBuf); gArtBuf = nullptr; }
      gArtLen = 0;
      gArtNew = true;
      xSemaphoreGive(mtx);
      fetchedArtId = 0;
      gNeedArtId = -1;
    } else {
      gNeedArtId = artId;
    }
  }
}

void pollStatus() {
  if (WiFi.status() != WL_CONNECTED) return;

  static HTTPClient http;
  static WiFiClient client;
  static bool httpInit = false;

  String url = String("http://") + PC_HOST + ":" + PC_PORT + "/status";

  if (!httpInit) {
    http.setReuse(true);
    http.setConnectTimeout(1800);
    http.setTimeout(2500);
    httpInit = true;
  }

  static unsigned long lastErrorLog = 0;
  if (http.begin(client, url)) {
    int code = http.GET();
    if (code == 200) {
      parseStatus(http.getString());
    } else {
      if (millis() - lastErrorLog > 5000) {
        lastErrorLog = millis();
        Serial.printf("[NET] PC bridge not responding at %s:%d (HTTP %d)\n", PC_HOST, PC_PORT, code);
      }
      http.end(); // close socket on error to reset cleanly
    }
  } else if (millis() - lastErrorLog > 5000) {
    lastErrorLog = millis();
    Serial.printf("[NET] Cannot open PC bridge at %s:%d\n", PC_HOST, PC_PORT);
    http.end();
  }
}

void netTask(void *param) {
  unsigned long lastPoll = 0;
  unsigned long lastReconnectAttempt = 0;
  bool wasWifiConnected = false;

  for (;;) {
    if (WiFi.status() != WL_CONNECTED) {
      wifiOk = false;
      wasWifiConnected = false;

      // Attempt active reconnect if disconnected for more than 10 seconds
      if (millis() - lastReconnectAttempt > 10000) {
        lastReconnectAttempt = millis();
        static bool useAlt = false;
        useAlt = !useAlt;
        const char *target = useAlt ? WIFI_SSID_ALT : WIFI_SSID;
        Serial.printf("[WIFI] Reconnecting to %s...\n", target);
        WiFi.disconnect();
        WiFi.begin(target, WIFI_PASS);
      }

      vTaskDelay(500 / portTICK_PERIOD_MS);
      continue;
    }

    wifiOk = true;
    if (!wasWifiConnected) {
      wasWifiConnected = true;
      Serial.println("[WIFI] Connected");
      Serial.print("[WIFI] ESP32 IP: ");
      Serial.println(WiFi.localIP());
      Serial.printf("[NET] Trying PC bridge at http://%s:%d\n", PC_HOST, PC_PORT);
      lastPoll = 0;  // poll immediately after Wi-Fi reconnects
    }

    Cmd command;
    while (xQueueReceive(cmdQ, &command, 0) == pdTRUE) {
      sendCmdHttp(command.s);
      lastPoll = millis() - 900;
    }

    // Dynamic poll interval:
    // If not connected to PC: poll gently every 2000ms to avoid network/socket flood
    // If connected and playing: poll every 600ms for responsive lyrics
    // If connected and paused: poll every 1000ms
    unsigned long pollInterval = 600;
    if (!connected) pollInterval = 2000;
    else if (!gPlaying) pollInterval = 1000;

    if (millis() - lastPoll >= pollInterval) {
      lastPoll = millis();
      pollStatus();
    }

    if (gNeedArtId > 0) {
      int id = gNeedArtId;
      gNeedArtId = -1;
      fetchArt(id);
    }

    vTaskDelay(20 / portTICK_PERIOD_MS);
  }
}

void sendCmd(const char *c) {
  if (!cmdQ) return;
  Cmd cmd;
  strncpy(cmd.s, c, sizeof(cmd.s) - 1);
  cmd.s[sizeof(cmd.s) - 1] = 0;
  if (xQueueSend(cmdQ, &cmd, 0) != pdTRUE) {
    Serial.println("[QUEUE] Command queue full; command dropped");
  }
}

// Copies new data from the network task into UI state (called from loop)
void syncFromNet() {
  bool upd = false, artNew = false;
  String t, a, curL, nxtL, prvL;
  int pos = 0, dur = 0;
  bool pl = false;
  unsigned long rx = 0;
  uint8_t *buf = nullptr;
  size_t len = 0;

  if (xSemaphoreTake(mtx, 0) != pdTRUE) return;
  if (gUpdated) {
    upd = true;
    gUpdated = false;
    t = gTitle; a = gArtist; pos = gPos; dur = gDur; pl = gPlaying; rx = gRx;
    curL = gCurLyric; nxtL = gNextLyric; prvL = gPrevLyric;
  }
  if (gArtNew) {
    artNew = true;
    gArtNew = false;
    buf = gArtBuf; len = gArtLen;
    gArtBuf = nullptr; gArtLen = 0;
  }
  xSemaphoreGive(mtx);

  if (upd) {
    bool songChanged = (t != title || a != artist);
    if (songChanged) {
      title = t;
      titleSc.off = 0;
      titleSc.hold = millis() + 1200;
      artist = a;
      artistSc.off = 0;
      artistSc.hold = millis() + 1200;
    }
    bool was = playing;
    playing = pl;
    durSec = dur;
    lastRx = rx;

    // Smooth progress progression: keep continuous estimated position unless seek/jump/pause
    if (pl && was && abs(pos - estimatedPos()) <= 1) {
      posSec = estimatedPos();
      posBaseMs = rx;
    } else {
      posSec = pos;
      posBaseMs = rx;
    }

    if (songChanged || curL != curLyric || nxtL != nextLyric || prvL != prevLyric) {
      curLyric = curL;
      nextLyric = nxtL;
      prevLyric = prvL;
      drawLyrics();
    }
    if (was != playing) {
      drawButton(2, false);
      drawLabel();
    }
  }

  if (artNew) {
    if (!lyricsView) {
      bool ok = (buf && len > 0) ? drawArtJpeg(buf, len) : false;
      if (!ok) drawArt();
    }
    if (buf) free(buf);
  }
}

// =====================================================
//  Actions & hit testing
// =====================================================
int hitTest(int x, int y) {
  if (lyricsView) {
    if (x <= 80 && y <= 38) return 10; // 10 = Back button
    return -1;
  }
  if (y < 175) return -1;
  if (x < 64)  return 0;  // VOL-
  if (x < 128) return 1;  // PREV
  if (x < 192) return 2;  // PLAY/PAUSE
  if (x < 256) return 3;  // NEXT
  return 4;               // VOL+
}

void flashButton(int b) {
  drawButton(b, true);
  pressedBtn = b;
  pressedUntil = millis() + 130;
}

void doButtonAction(int b) {
  flashButton(b);
  switch (b) {
    case 0: sendCmd("VOLDN"); break;
    case 1: sendCmd("PREV");  break;
    case 2:
      sendCmd("PLAY");
      playing = !playing;
      posSec = estimatedPos();
      posBaseMs = millis();
      drawButton(2, false);
      break;
    case 3: sendCmd("NEXT");  break;
    case 4: sendCmd("VOLUP"); break;
  }
}

// =====================================================
//  Setup / loop
// =====================================================
void setup() {
  WRITE_PERI_REG(RTC_CNTL_BROWN_OUT_REG, 0); // Disable hardware brownout reset to prevent voltage dip reboots

  Serial.begin(115200);
  delay(100);
  Serial.println("\n--- ESP32 YouTube Music Remote ---");

  // Explicit hardware reset pulse to ILI9341
  pinMode(TFT_RST, OUTPUT);
  digitalWrite(TFT_RST, HIGH);
  delay(10);
  digitalWrite(TFT_RST, LOW);
  delay(20);
  digitalWrite(TFT_RST, HIGH);
  delay(150);

  pinMode(TFT_CS, OUTPUT);
  digitalWrite(TFT_CS, HIGH);
  pinMode(TOUCH_CS, OUTPUT);
  digitalWrite(TOUCH_CS, HIGH);

  tft.begin(10000000);       // 10 MHz, safe for breadboard wires
  tft.setRotation(1);        // Landscape
  tft.setTextWrap(false);
  tft.fillScreen(C_ACCENT);  // quick accent flash: display controller is alive
  delay(300);
  tft.fillScreen(C_BG);

  // Canvas dimensions updated for new layout (text area right of 100px art)
  titleCv = new GFXcanvas16(TXT_W, 22);
  artistCv = new GFXcanvas16(TXT_W, 14);

  ts.begin();
  ts.setRotation(1);
  ts.setThreshold(220);      // Soft touch sensitivity without SPI noise glitches

  mtx = xSemaphoreCreateMutex();
  cmdQ = xQueueCreate(8, sizeof(Cmd));
  if (!mtx || !cmdQ) {
    Serial.println("[FATAL] Could not allocate network synchronization objects");
    while (true) delay(1000);
  }

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);              // Prevent power-save delays on the small HTTP requests
  WiFi.setTxPower(WIFI_POWER_17dBm); // 17dBm eliminates current draw spikes, stopping power drops
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.println("[WIFI] Starting connection...");
  Serial.printf("[CONFIG] PC bridge: http://%s:%d\n", PC_HOST, PC_PORT);

  BaseType_t taskStarted = xTaskCreatePinnedToCore(
    netTask, "net", 12288, NULL, 1, NULL, 0
  );
  if (taskStarted != pdPASS) {
    Serial.println("[FATAL] Could not start network task");
    while (true) delay(1000);
  }

  drawScreen();
  Serial.println("Ready. Open Serial Monitor at 115200 baud for connection status.");
}

void loop() {
  unsigned long now = millis();

  syncFromNet();

  bool wasConnected = connected;
  connected = wifiOk && (lastRx != 0) && (now - lastRx < 8000);

  if (wasConnected && !connected) {
    // When Python disconnects: stop playback state and update display
    playing = false;
    posSec = estimatedPos();
    posBaseMs = now;
    curLyric = "";
    nextLyric = "";
    prevLyric = "";
    drawButton(2, false);   // Updates to Play icon (stopped)
    drawProgress();
    drawLyrics();
    drawLabel();
  } else if (!wasConnected && connected) {
    drawLabel();
    drawLyrics();
  } else if (labelCode() != lastLabelCode) {
    drawLabel();
  }

  // --- Touch handling with noise filter ---
  int x, y;
  bool raw = readTouch(x, y);

  if (DEBUG_TOUCH && raw && now - lastDbg > 120) {
    lastDbg = now;
    Serial.printf("DBG raw=%d,%d z=%d -> screen=%d,%d\n", rawX, rawY, rawZ, x, y);
  }

  if (raw) {
    consecTouch++;
    lastTouchMs = now;
    if (!touching) {
      if (consecTouch >= 2) {  // Require 2 consecutive cycles to confirm touch
        touching = true;
        startX = x; startY = y;
        touchStartMs = now;
        initialBtn = hitTest(x, y);
        holdBtn = (initialBtn == 0 || initialBtn == 4) ? initialBtn : -1;
        repeating = false;
        if (initialBtn >= 0) flashButton(initialBtn);
      }
    }
    lastX = x; lastY = y;

    // Hold VOL- / VOL+ to repeat after 450 ms
    if (holdBtn != -1 && now - touchStartMs > 450 && now - lastRepeat > 150) {
      lastRepeat = now;
      repeating = true;
      sendCmd(holdBtn == 0 ? "VOLDN" : "VOLUP");
      flashButton(holdBtn);
    }
  } else if (now - lastTouchMs > 70) {      // finger lifted
    consecTouch = 0;
    if (touching) {
      touching = false;
      unsigned long duration = now - touchStartMs;
      int dx = lastX - startX;
      int dy = lastY - startY;

      // Only evaluate if touched for at least 50 ms (rejects transient noise)
      if (duration >= 50) {
        if (lyricsView) {
          // In fullscreen lyrics view:
          // Tap on Back button (or header) OR swipe right returns to normal player view
          if (initialBtn == 10 || startY <= 38 || dx > 50) {
            lyricsView = false;
            drawScreen();
            fetchedArtId = -1; // refresh album art
          }
        } else {
          // In normal player view:
          if (initialBtn >= 0) {
            if (!repeating) doButtonAction(initialBtn);
          } else if (startY >= 140 && startY <= 168 && durSec > 0) {
            // Tap on the progress bar to seek
            int seekSec = (long)(startX - BAR_X) * durSec / BAR_W;
            seekSec = constrain(seekSec, 0, durSec);
            posSec = seekSec;
            posBaseMs = now;
            drawProgress();
            char cmd[24];
            snprintf(cmd, sizeof(cmd), "SEEK:%d", seekSec);
            sendCmd(cmd);
          } else if (startY < 140) {
            // Upper area gestures
            if (abs(dx) > 60 && abs(dx) > abs(dy) * 2 && duration < 800) {
              // Swipe gesture for Next / Previous track
              if (dx < 0) { sendCmd("NEXT"); flashButton(3); }
              else        { sendCmd("PREV"); flashButton(1); }
            } else if ((startX >= ART_X && startX <= ART_X + ART_W && startY >= ART_Y && startY <= ART_Y + ART_H) ||
                       (startX >= LYR_X - 10 && startY >= LYR_Y - 10 && startY <= 140)) {
              // Tap on Album Art OR Lyrics area opens Fullscreen Lyrics!
              lyricsView = true;
              drawFullscreenLyrics();
            }
          }
        }
      }

      repeating = false;
      initialBtn = -1;
      holdBtn = -1;
    }
  }

  // --- Button flash reset ---
  if (pressedBtn >= 0 && now > pressedUntil) {
    drawButton(pressedBtn, false);
    pressedBtn = -1;
  }

  // --- Scrolling text (active in normal album art view) ---
  if (now - lastScroll > 60) {
    lastScroll = now;
    if (!lyricsView && titleCv && artistCv && titleCv->getBuffer() && artistCv->getBuffer()) {
      drawScroller(*titleCv,  titleSc,  TXT_X, TXT_Y,      title,  2, C_WHITE);
      drawScroller(*artistCv, artistSc, TXT_X, TXT_Y + 26,  artist, 1, C_LYGREY);
    }
  }

  // --- Progress bar ---
  if (now - lastProgressCheck > 250) {
    lastProgressCheck = now;
    if (estimatedPos() != lastDrawnEst) drawProgress();
  }

  delay(5);
}
