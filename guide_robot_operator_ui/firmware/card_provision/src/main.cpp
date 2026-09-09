// card_provision -- утилита персонализации карт операторов для rfid_bridge
// (design E4/E5). Прошивается на ту же плату ESP32-S3 с тем же RC522.
// НЕ рабочая прошивка: после выпуска карт вернуть rfid_bridge.
//
// Что делает: пишет в сектор 1 карты MIFARE Classic 1K логическое имя
// оператора (блок 4) и меняет ключ A сектора с заводского FFFFFFFFFFFF на
// RFID_SECTOR_KEY (блок 7, trailer). Ровно то, что потом читает
// rfid_bridge::readOperatorName().
//
// Интерфейс -- построчные команды поверх USB CDC, 115200:
//   help                 -- список команд
//   verify               -- прочитать карту, ничего не меняя
//   write <имя>          -- прошить ЧИСТУЮ карту (заводской ключ)
//   rename <имя>         -- сменить имя на УЖЕ прошитой карте (ключ не трогается)
//
// Порядок записи в write умышленный: сначала имя, потом ключ. Сбой на
// первом шаге оставляет карту с заводским ключом и полностью
// восстановимой. Байты доступа FF 07 80 69 -- заводская конфигурация;
// любое другое значение может необратимо закирпичить сектор, менять их
// нельзя.

#include <Arduino.h>
#include <MFRC522.h>
#include <SPI.h>

#include "card_provision_secrets.h"

constexpr uint8_t PIN_RC522_SS = 10;   // как в rfid_bridge
constexpr uint8_t PIN_RC522_RST = 9;

constexpr uint8_t SECTOR_DATA_BLOCK = 4;     // первый data-блок сектора 1
constexpr uint8_t SECTOR_TRAILER_BLOCK = 7;  // trailer сектора 1
constexpr size_t CARD_NAME_MAX = 16;              // MIFARE Classic -- 16 байт на блок
constexpr uint32_t CARD_WAIT_MS = 20000;

constexpr uint8_t kNewKeyBytes[6] = RFID_SECTOR_KEY;

MFRC522 rfid(PIN_RC522_SS, PIN_RC522_RST);
MFRC522::MIFARE_Key factoryKey, newKey;

void release() {
  rfid.PICC_HaltA();
  rfid.PCD_StopCrypto1();
}

// Ждать карту в поле. PICC_IsNewCardPresent() шлёт REQA, на который карта
// в состоянии HALT не отвечает -- поэтому между операциями карту надо
// физически убирать и подносить заново, и поэтому здесь цикл, а не один
// опрос: на FM17522 попадание в поле вероятностное.
bool waitForCard(const char *prompt) {
  Serial.println(prompt);
  uint32_t deadline = millis() + CARD_WAIT_MS;
  while (millis() < deadline) {
    if (rfid.PICC_IsNewCardPresent() && rfid.PICC_ReadCardSerial()) {
      return true;
    }
    delay(50);
  }
  Serial.println("!! карта не появилась за 20 с");
  return false;
}

bool auth(uint8_t block, MFRC522::MIFARE_Key *key) {
  return rfid.PCD_Authenticate(MFRC522::PICC_CMD_MF_AUTH_KEY_A, block, key, &(rfid.uid))
         == MFRC522::STATUS_OK;
}

bool readName(String &out) {
  byte buf[18];
  byte size = sizeof(buf);
  if (rfid.MIFARE_Read(SECTOR_DATA_BLOCK, buf, &size) != MFRC522::STATUS_OK) {
    return false;
  }
  out = "";
  for (size_t i = 0; i < CARD_NAME_MAX && buf[i] != 0; i++) {
    out += static_cast<char>(buf[i]);
  }
  return true;
}

bool writeName(const String &name) {
  byte data[16] = {0};
  for (size_t i = 0; i < CARD_NAME_MAX && i < name.length(); i++) {
    data[i] = static_cast<byte>(name[i]);
  }
  return rfid.MIFARE_Write(SECTOR_DATA_BLOCK, data, 16) == MFRC522::STATUS_OK;
}

void cmdVerify() {
  if (!waitForCard("подноси карту (только чтение, ничего не меняется)...")) {
    return;
  }
  String name;
  if (auth(SECTOR_DATA_BLOCK, &newKey) && readName(name)) {
    Serial.printf("OK: прошитая карта, имя = '%s'\n", name.c_str());
    release();
    return;
  }
  release();

  if (!waitForCard("не открылась рабочим ключом. Убери карту и поднеси снова...")) {
    return;
  }
  if (auth(SECTOR_DATA_BLOCK, &factoryKey)) {
    Serial.println("ЧИСТАЯ карта: сектор 1 открыт заводским ключом FFFFFFFFFFFF.");
    Serial.println("Готова под 'write <имя>'.");
  } else {
    Serial.println("НИ ОДИН ключ не подошёл. Либо карта прошита другим ключом,");
    Serial.println("либо это не MIFARE Classic 1K, либо сектор 1 повреждён.");
  }
  release();
}

void cmdWrite(const String &name) {
  if (name.length() == 0 || name.length() > CARD_NAME_MAX) {
    Serial.printf("!! имя должно быть 1..%u символов\n", (unsigned)CARD_NAME_MAX);
    return;
  }
  if (!waitForCard("подноси ЧИСТУЮ карту...")) {
    return;
  }

  if (!auth(SECTOR_DATA_BLOCK, &factoryKey)) {
    Serial.println("!! сектор 1 не открылся заводским ключом FFFFFFFFFFFF.");
    Serial.println("   Карта уже персонализирована или не MIFARE Classic.");
    Serial.println("   НИЧЕГО НЕ ЗАПИСАНО. Для смены имени используй 'rename'.");
    release();
    return;
  }

  if (!writeName(name)) {
    Serial.println("!! ошибка записи блока 4. Ключ НЕ менялся, карта цела.");
    release();
    return;
  }
  Serial.println("   блок 4 записан");

  byte trailer[16];
  memcpy(trailer, kNewKeyBytes, 6);
  trailer[6] = 0xFF;  // access bits -- заводская конфигурация,
  trailer[7] = 0x07;  // МЕНЯТЬ НЕЛЬЗЯ: неверные биты необратимо
  trailer[8] = 0x80;  // закирпичивают сектор
  trailer[9] = 0x69;
  memcpy(trailer + 10, kNewKeyBytes, 6);  // key B = key A, запасной путь

  if (rfid.MIFARE_Write(SECTOR_TRAILER_BLOCK, trailer, 16) != MFRC522::STATUS_OK) {
    Serial.println("!! ошибка записи trailer. Ключ мог не смениться --");
    Serial.println("   проверь командой 'verify' до выдачи карты.");
    release();
    return;
  }
  Serial.println("   trailer записан, ключ сменён");
  release();

  Serial.println("Записано. Проверь: убери карту, поднеси заново, набери 'verify'.");
}

void cmdRename(const String &name) {
  if (name.length() == 0 || name.length() > CARD_NAME_MAX) {
    Serial.printf("!! имя должно быть 1..%u символов\n", (unsigned)CARD_NAME_MAX);
    return;
  }
  if (!waitForCard("подноси УЖЕ ПРОШИТУЮ карту...")) {
    return;
  }
  if (!auth(SECTOR_DATA_BLOCK, &newKey)) {
    Serial.println("!! карта не открылась рабочим ключом. Ничего не записано.");
    release();
    return;
  }
  if (!writeName(name)) {
    Serial.println("!! ошибка записи блока 4.");
    release();
    return;
  }
  Serial.printf("   имя изменено на '%s' (ключ не трогали)\n", name.c_str());
  release();
}

void help() {
  Serial.println("команды:");
  Serial.println("  verify         -- прочитать карту, ничего не меняя");
  Serial.println("  write <имя>    -- прошить ЧИСТУЮ карту (имя + смена ключа)");
  Serial.println("  rename <имя>   -- сменить имя на прошитой карте");
  Serial.println("  help");
  Serial.println("имя -- ASCII, до 16 символов, попадёт в поле \"card\" и в jsonl-лог");
}

void setup() {
  Serial.begin(115200);
  while (!Serial) {
    delay(10);
  }
  SPI.begin();
  rfid.PCD_Init();

  for (int i = 0; i < 6; i++) {
    factoryKey.keyByte[i] = 0xFF;
    newKey.keyByte[i] = kNewKeyBytes[i];
  }

  byte v = rfid.PCD_ReadRegister(MFRC522::VersionReg);
  Serial.printf("\ncard_provision. RC522 VersionReg = 0x%02X ", v);
  if (v == 0x00 || v == 0xFF) {
    Serial.println("<- SPI НЕ РАБОТАЕТ, проверь пайку и питание 3.3 В");
  } else if (v == 0x88) {
    Serial.println("(FM17522, клон -- нормально)");
  } else if (v == 0x91 || v == 0x92) {
    Serial.println("(оригинал MFRC522)");
  } else {
    Serial.println("(неизвестная ревизия)");
  }
  help();
}

void loop() {
  static String line;
  while (Serial.available()) {
    char c = static_cast<char>(Serial.read());
    if (c != '\n') {
      if (c != '\r') {
        line += c;
      }
      continue;
    }
    line.trim();
    if (line.length() == 0) {
      continue;
    }
    if (line == "help") {
      help();
    } else if (line == "verify") {
      cmdVerify();
    } else if (line.startsWith("write ")) {
      cmdWrite(line.substring(6));
    } else if (line.startsWith("rename ")) {
      cmdRename(line.substring(7));
    } else {
      Serial.println("!! неизвестная команда, набери help");
    }
    line = "";
  }
}
