/*
  arduino_hid_bridge.ino

  Turns a 32u4-based board (Leonardo, Micro, Pro Micro) into a USB HID
  keyboard AND mouse that is remote-controlled over serial from a laptop.

  Wiring:
    - Plug the board's native USB port into the TARGET machine
      (the one you want to control). This is the port that shows up
      as "a keyboard/mouse" to that machine.
    - Feed it commands over the RX1/TX1 pins from your laptop via a
      USB-TTL serial adapter. This uses Serial1 (the hardware UART),
      which is separate from the native USB port used for HID above.

  The onboard LED flashes briefly on every event received, so you can
  visually confirm the control link is alive even before checking
  whether input lands on the target.

  Wire protocol: every frame starts with a sync byte and a type byte,
  followed by a type-specific number of payload bytes:

    [0xAA][0x01][code]           key down   (code = Keyboard.h code)
    [0xAA][0x02][code]           key up
    [0xAA][0x03][dx][dy]         mouse move, relative, signed bytes (-127..127)
    [0xAA][0x04][button]         mouse button down (1=left, 2=right, 4=middle)
    [0xAA][0x05][button]         mouse button up
    [0xAA][0x06][amount]         mouse scroll, signed byte

  Key codes match Arduino's built-in Keyboard.h constants exactly, and
  mouse button values match Mouse.h's MOUSE_LEFT/MOUSE_RIGHT/MOUSE_MIDDLE,
  so no translation table is needed on this side.
*/

#include <Keyboard.h>
#include <Mouse.h>

const uint8_t SYNC_BYTE = 0xAA;

const uint8_t EVT_KEYDOWN     = 0x01;
const uint8_t EVT_KEYUP       = 0x02;
const uint8_t EVT_MOUSE_MOVE  = 0x03;
const uint8_t EVT_MOUSE_DOWN  = 0x04;
const uint8_t EVT_MOUSE_UP    = 0x05;
const uint8_t EVT_MOUSE_SCROLL = 0x06;

// Number of payload bytes that follow the type byte for each event type.
uint8_t payloadLength(uint8_t type) {
  switch (type) {
    case EVT_KEYDOWN:      return 1;
    case EVT_KEYUP:        return 1;
    case EVT_MOUSE_MOVE:   return 2;
    case EVT_MOUSE_DOWN:   return 1;
    case EVT_MOUSE_UP:     return 1;
    case EVT_MOUSE_SCROLL: return 1;
    default:                return 0;
  }
}

void setup() {
  // IMPORTANT: use Serial1 (the hardware UART on the RX/TX pins), not
  // Serial. On 32u4 boards, "Serial" is the USB CDC port that rides over
  // the SAME native USB cable as the HID connection -- which needs to go
  // to the TARGET machine. Your TTL adapter feeds the physical RX/TX
  // pins from the laptop, so that's Serial1.
  Serial1.begin(115200);  // laptop <-> Arduino control link (via TTL adapter)
  Keyboard.begin();       // starts USB HID keyboard on the native USB port
  Mouse.begin();          // starts USB HID mouse on the same native USB port

  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);
}

void loop() {
  if (Serial1.available() < 1) {
    return;
  }

  uint8_t b0 = Serial1.read();
  if (b0 != SYNC_BYTE) {
    return; // resync: drop bytes until we see 0xAA
  }

  while (Serial1.available() < 1) {
    // wait for the type byte
  }
  uint8_t type = Serial1.read();

  uint8_t need = payloadLength(type);
  uint8_t payload[2] = {0, 0};
  for (uint8_t i = 0; i < need; i++) {
    while (Serial1.available() < 1) {
      // wait for each payload byte
    }
    payload[i] = Serial1.read();
  }

  digitalWrite(LED_BUILTIN, HIGH); // flash on every received event

  switch (type) {
    case EVT_KEYDOWN:
      Keyboard.press((uint8_t)payload[0]);
      break;
    case EVT_KEYUP:
      Keyboard.release((uint8_t)payload[0]);
      break;
    case EVT_MOUSE_MOVE: {
      int8_t dx = (int8_t)payload[0];
      int8_t dy = (int8_t)payload[1];
      Mouse.move(dx, dy, 0);
      break;
    }
    case EVT_MOUSE_DOWN:
      Mouse.press((uint8_t)payload[0]);
      break;
    case EVT_MOUSE_UP:
      Mouse.release((uint8_t)payload[0]);
      break;
    case EVT_MOUSE_SCROLL: {
      int8_t amount = (int8_t)payload[0];
      Mouse.move(0, 0, amount);
      break;
    }
  }

  digitalWrite(LED_BUILTIN, LOW);
}
