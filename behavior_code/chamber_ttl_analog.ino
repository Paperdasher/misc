/*
  chamber_ttl_analog.ino
  =======================
  Reads analog voltage on multiple pins and reports threshold-crossing
  events to Python over USB serial as numbered TTL channels.

  Numbering
  ---------
  TTL channels are numbered sequentially starting at 1, in the same order
  as ANALOG_PINS below:
      ANALOG_PINS[0] → TTL 1
      ANALOG_PINS[1] → TTL 2
      ANALOG_PINS[2] → TTL 3
      ANALOG_PINS[3] → TTL 4
      ...
  Each chamber typically uses 3 consecutive TTL numbers, e.g.:
      Chamber 1 → TTL 1, 2, 3   (pins A0, A1, A2)
      Chamber 2 → TTL 4, 5, 6   (pins A3, A4, A5)
  This grouping is defined in config.yaml on the Python side, NOT here —
  this sketch only reports raw per-pin threshold state.

  Protocol
  --------
  Every loop iteration, for EVERY configured pin, send:
      TTL:<number>:<state>\n
  where state is 1 (voltage above threshold) or 0 (below).

  Sending every loop (not just on change) lets Python apply its own
  "3 consecutive active reads" debounce/confirmation logic on its side,
  which is where the actual start/stop decision is made.

  Voltage threshold & board reference voltage
  ---------------------------------------------
  Set BOARD_VREF to match your Arduino's logic level:
      5.0   → Uno, Mega, Nano (5V boards)
      3.3   → Due, Zero, MKR, most 3.3V boards
  THRESHOLD_FRACTION (default 0.5) sets the trigger point as a fraction
  of VREF, so threshold is automatically VREF/2 — adjust if your TTL
  source's "high" voltage is closer to one rail.

  Wiring
  ------
  TTL source output → Arduino analog pin (A0, A1, A2, ...)
  TTL source GND    → Arduino GND
  If your TTL source outputs 5V logic into a 3.3V board's analog pin,
  add a voltage divider — analog pins can be damaged by overvoltage.

  Baud rate: 115200 (must match config.yaml arduino.baud)
*/

// ---------------------------------------------------------------------------
// Configuration — edit this section
// ---------------------------------------------------------------------------

#define BAUD_RATE 115200

// Board reference voltage — CHANGE THIS to match your hardware
//   5.0 for Uno/Mega/Nano,  3.3 for Due/Zero/MKR
const float BOARD_VREF = 5.0;

// Fraction of VREF that counts as "high" / active
//   0.5 → threshold at half of VREF (2.5V on a 5V board, 1.65V on 3.3V)
const float THRESHOLD_FRACTION = 0.5;

// ADC resolution — 1023 for the default 10-bit analogRead on most boards
// (Due/Zero default to 10-bit unless analogReadResolution() is called)
const int ADC_MAX = 1023;

// List every analog pin in use, in TTL-number order.
// Index 0 → TTL 1, index 1 → TTL 2, etc.
const uint8_t ANALOG_PINS[] = {
  A0, A1, A2,   // Chamber 1 → TTL 1, 2, 3
  A3, A4, A5,   // Chamber 2 → TTL 4, 5, 6
  // A6, A7, A8,  // Chamber 3 → TTL 7, 8, 9  (uncomment + extend as needed)
};

const uint8_t N_PINS = sizeof(ANALOG_PINS) / sizeof(ANALOG_PINS[0]);

// ---------------------------------------------------------------------------
// Derived threshold (raw ADC counts)
// ---------------------------------------------------------------------------

const int THRESHOLD_RAW = (int)(ADC_MAX * THRESHOLD_FRACTION);

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

void setup() {
  Serial.begin(BAUD_RATE);
  Serial.println("READY");
}

// ---------------------------------------------------------------------------
// Loop
// ---------------------------------------------------------------------------

void loop() {
  for (uint8_t i = 0; i < N_PINS; i++) {
    int raw   = analogRead(ANALOG_PINS[i]);
    int state = (raw >= THRESHOLD_RAW) ? 1 : 0;
    int ttl_number = i + 1;   // sequential numbering starting at 1

    Serial.print("TTL:");
    Serial.print(ttl_number);
    Serial.print(":");
    Serial.println(state);
  }

  // Small delay to set the polling rate. At 115200 baud, each line is
  // roughly 1ms to transmit; with 6 pins that's ~6ms per full cycle.
  // 5ms extra delay gives ~10ms cycle time (100Hz per-pin sampling),
  // matching the debounce assumptions on the Python side.
  delay(5);
}
