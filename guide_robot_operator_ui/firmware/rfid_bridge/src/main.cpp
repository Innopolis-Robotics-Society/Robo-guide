// rfid_bridge -- ESP32-S3 + RC522 challenge/response мост для operator_ui
// (Task E4/E5). Прошивка сама по себе -- единственная граница доверия
// этой схемы: секрет и ключ сектора живут ТОЛЬКО здесь.
//
// Протокол -- построчный JSON поверх USB CDC (design E4):
//   -> {"cmd":"challenge","nonce":"<64 hex>"}
//   <- {"ok":true,"resp":"<hmac hex>","card":"op_mook"}
//   <- {"ok":false,"err":"no_card"}
//
// `resp` = HMAC-SHA256(RFID_BRIDGE_SHARED_SECRET, nonce) -- доказывает
// узлу, что ответ от легитимной прошивки, не инжектирован в serial.
// `card` -- логическое имя оператора, прочитанное из защищённого сектора
// карты, НЕ UID. Ни UID, ни содержимое сектора не покидают эту прошивку
// ни в одном поле ответа (design E4, критерий 17) -- reply_ok()/reply_err()
// ниже единственное место, которое пишет в Serial, и они принимают
// только (hex-подпись, имя) или код ошибки, физически не могут переслать
// что-то ещё.

#include <Arduino.h>
#include <MFRC522.h>
#include <SPI.h>
#include <USB.h>
#include <esp_wifi.h>
#include <mbedtls/md.h>

#include "rfid_bridge_secrets.h"

// -- распиновка RC522 <-> ESP32-S3 (см. README.md -- поменяй под свою плату) --
constexpr uint8_t PIN_RC522_SS = 10;
constexpr uint8_t PIN_RC522_RST = 9;
// SCK/MOSI/MISO -- аппаратные пины SPI по умолчанию для платы из
// platformio.ini; отдельно не переопределяются.

// Сектор/блок, где карта хранит логическое имя оператора. НЕ сектор 0 --
// там производственный/manufacturer-блок, на многих клонах читаемый без
// ключа вообще; сектор 1 требует KEY_A, которого у клона без прошитого
// сектора не будет (design E4).
constexpr uint8_t RFID_SECTOR_BLOCK = 4;  // первый data-блок сектора 1
constexpr size_t RFID_NAME_MAX = 16;      // MIFARE Classic -- 16 байт на блок

constexpr uint8_t kSectorKeyBytes[6] = RFID_BRIDGE_SECTOR_KEY;

MFRC522 rfid(PIN_RC522_SS, PIN_RC522_RST);
MFRC522::MIFARE_Key sectorKey;

void setup() {
  // Серийник -- ДО USB.begin()/Serial.begin(), иначе поздно (design E5):
  // без него все платы этой модели отвечают одним и тем же VID:PID
  // 303a:1001, и udev-правило по ATTRS{serial} не сможет их различить
  // при переподключении/перезагрузке.
  USB.serialNumber(RFID_BRIDGE_USB_SERIAL);
  Serial.begin(115200);
  while (!Serial) {
    delay(10);
  }

  // Wi-Fi выключен явно (design E5) -- ридеру сеть не нужна вообще, а её
  // всплески тока на общем USB-хабе с микрофонным массивом ищутся потом
  // неделю. Безопасно звать, даже если радио никогда не инициализировалось
  // (ESP_ERR_WIFI_NOT_INIT в этом случае -- не ошибка, а ожидаемый исход).
  esp_err_t wifi_rc = esp_wifi_stop();
  if (wifi_rc != ESP_OK && wifi_rc != ESP_ERR_WIFI_NOT_INIT) {
    Serial.printf("{\"log\":\"esp_wifi_stop rc=%d\"}\n", static_cast<int>(wifi_rc));
  }

  SPI.begin();
  rfid.PCD_Init();

  for (int i = 0; i < 6; i++) {
    sectorKey.keyByte[i] = kSectorKeyBytes[i];
  }
}

void reply_ok(const String &respHex, const String &card) {
  Serial.printf("{\"ok\":true,\"resp\":\"%s\",\"card\":\"%s\"}\n", respHex.c_str(), card.c_str());
}

void reply_err(const char *err) {
  Serial.printf("{\"ok\":false,\"err\":\"%s\"}\n", err);
}

// Аутентифицировать карту по сектору и прочитать имя оператора из него.
// false -- карты нет ИЛИ ключ сектора не подошёл; клон/чужая карта без
// прошитого сектора не отличается от "карты нет" на этом уровне --
// так и задумано (design E4: клонировать UID недостаточно).
bool readOperatorName(String &outName) {
  if (!rfid.PICC_IsNewCardPresent() || !rfid.PICC_ReadCardSerial()) {
    return false;
  }

  MFRC522::StatusCode status = rfid.PCD_Authenticate(
      MFRC522::PICC_CMD_MF_AUTH_KEY_A, RFID_SECTOR_BLOCK, &sectorKey, &(rfid.uid));
  if (status != MFRC522::STATUS_OK) {
    rfid.PICC_HaltA();
    rfid.PCD_StopCrypto1();
    return false;
  }

  byte buffer[18];
  byte size = sizeof(buffer);
  status = rfid.MIFARE_Read(RFID_SECTOR_BLOCK, buffer, &size);
  rfid.PICC_HaltA();
  rfid.PCD_StopCrypto1();
  if (status != MFRC522::STATUS_OK) {
    return false;
  }

  outName = "";
  for (size_t i = 0; i < RFID_NAME_MAX && buffer[i] != 0; i++) {
    outName += static_cast<char>(buffer[i]);
  }
  return outName.length() > 0;
}

String hmacSha256Hex(const String &nonce) {
  uint8_t digest[32];
  const char *secret = RFID_BRIDGE_SHARED_SECRET;

  mbedtls_md_context_t ctx;
  mbedtls_md_init(&ctx);
  mbedtls_md_setup(&ctx, mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), /*hmac=*/1);
  mbedtls_md_hmac_starts(&ctx, reinterpret_cast<const unsigned char *>(secret), strlen(secret));
  mbedtls_md_hmac_update(&ctx, reinterpret_cast<const unsigned char *>(nonce.c_str()),
                          nonce.length());
  mbedtls_md_hmac_finish(&ctx, digest);
  mbedtls_md_free(&ctx);

  char hex[65];
  for (int i = 0; i < 32; i++) {
    sprintf(hex + i * 2, "%02x", digest[i]);
  }
  hex[64] = '\0';
  return String(hex);
}

// Разбор входящей строки без JSON-библиотеки -- запрос ровно один,
// {"cmd":"challenge","nonce":"..."}, полноценный парсер на недоверенном
// входе -- лишняя поверхность для багов ради формата, который не меняется.
void handleLine(const String &line) {
  int nonceIdx = line.indexOf("\"nonce\"");
  if (line.indexOf("\"cmd\"") < 0 || line.indexOf("\"challenge\"") < 0 || nonceIdx < 0) {
    reply_err("bad_request");
    return;
  }
  int colon = line.indexOf(':', nonceIdx);
  int q1 = colon < 0 ? -1 : line.indexOf('"', colon + 1);
  int q2 = q1 < 0 ? -1 : line.indexOf('"', q1 + 1);
  if (q1 < 0 || q2 < 0) {
    reply_err("bad_request");
    return;
  }
  String nonce = line.substring(q1 + 1, q2);

  String card;
  if (!readOperatorName(card)) {
    reply_err("no_card");
    return;
  }
  reply_ok(hmacSha256Hex(nonce), card);
}

void loop() {
  static String line;
  while (Serial.available()) {
    char c = static_cast<char>(Serial.read());
    if (c == '\n') {
      line.trim();
      if (line.length() > 0) {
        handleLine(line);
      }
      line = "";
    } else if (c != '\r') {
      line += c;
    }
  }
}
