/*
  chamber_ttl_analog.ino
  =======================
  Reads analog voltage on multiple pins and reports threshold-crossing
  state for each pin to Python over USB serial as numbered TTL channels.

  TTL channel numbering
  ---------------------
  Channels are numbered sequentially starting at 1, in the same order as
  ANALOG_PINS below:
      ANALOG_PINS[0] → TTL 1
      ANALOG_PINS[1] → TTL 2
      ANALOG_PINS[2] → TTL 3
      ...

  IMPORTANT: The order of pins in ANALOG_PINS MUST match the order of
  entries in the ttl_map list in config.yaml. The Python side assigns
  each entry a TTL number by its position in that list (index + 1), and
  uses that number to look up which chamber/action to trigger. This
  sketch only reports raw per-pin state — all chamber assignment and
  start/stop logic lives in config.yaml under ttl_map.

  Example: if config.yaml ttl_map reads
      ttl_map:
        - { pin: A0, chamber: chamber_A, action: start_recording, ... }  # → TTL 1
        - { pin: A1, chamber: chamber_A, action: stop_recording,  ... }  # → TTL 2
        - { pin: A2, chamber: chamber_A, action: log_event,       ... }  # → TTL 3
        - { pin: A3, chamber: chamber_B, action: start_recording, ... }  # → TTL 4
        - { pin: A4, chamber: chamber_B, action: stop_recording,  ... }  # → TTL 5
        - { pin: A5, chamber: chamber_B, action: log_event,       ... }  # → TTL 6
  then ANALOG_PINS must be { A0, A1, A2, A3, A4, A5 } in that exact order.

  To add or rearrange channels: update ttl_map in config.yaml first,
  then update ANALOG_PINS here to match. The sketch does not need to be
  aware of chambers, actions, or any other semantic — only pin order
  must stay in sync.

  Serial protocol
  ---------------
  Every loop iteration, for EVERY configured pin, send:
      TTL:<number>:<state>\n
  where <state> is 1 (voltage above threshold) or 0 (below).

  All pins are reported every loop regardless of state change. This lets
  the Python AnalogTTLListener apply its own "3 consecutive active reads"
  debounce/confirmation before acting — the decision logic stays on the
  Python side.

  Voltage threshold & board reference voltage
  -------------------------------------------
  Set BOARD_VREF to match your Arduino's logic level:
      5.0 → Uno, Mega, Nano  (5 V boards)
      3.3 → Due, Zero, MKR, and most 3.3 V boards
  THRESHOLD_FRACTION (default 0.5) sets the trigger point as a fraction
  of VREF, so the threshold is automatically VREF / 2. Adjust if your
  TTL source's "high" level sits closer to one rail.

  Wiring
  ------
  TTL source output → Arduino analog pin (A0, A1, A2, ...)
  TTL source GND    → Arduino GND
  If your TTL source outputs 5 V logic into a 3.3 V board's analog pin,
  use a voltage divider — analog pins can be damaged by overvoltage.

  Baud rate: 115200 (must match arduino.baud in config.yaml)
*/

// ---------------------------------------------------------------------------
// Configuration — edit this section
// ---------------------------------------------------------------------------

#define BAUD_RATE 115200

// Board reference voltage — CHANGE THIS to match your hardware
//   5.0 for Uno/Mega/Nano,  3.3 for Due/Zero/MKR
const float BOARD_VREF = 5.0;

// Fraction of VREF that counts as "high" / active.
//   0.5 → threshold at VREF / 2  (2.5 V on a 5 V board, 1.65 V on 3.3 V)
const float THRESHOLD_FRACTION = 0.5;

// ADC resolution — 1023 for the default 10-bit analogRead on most boards.
// (Due/Zero default to 10-bit unless analogReadResolution() is called.)
const int ADC_MAX = 1023;

// ---------------------------------------------------------------------------
// Analog pin list — MUST match the pin order in ttl_map in config.yaml
//
// Each entry here becomes the TTL channel with that 1-based index:
//   ANALOG_PINS[0] → TTL 1  (must be the pin listed in ttl_map entry 0)
//   ANALOG_PINS[1] → TTL 2  (must be the pin listed in ttl_map entry 1)
//   ...
//
// Add, remove, or reorder pins here whenever you change ttl_map.
// ---------------------------------------------------------------------------

const uint8_t ANALOG_PINS[] = {
  A0,   // TTL 1 — set action in ttl_map entry 0
  A1,   // TTL 2 — set action in ttl_map entry 1
  A2,   // TTL 3 — set action in ttl_map entry 2
  A3,   // TTL 4 — set action in ttl_map entry 3
  A4,   // TTL 5 — set action in ttl_map entry 4
  A5,   // TTL 6 — set action in ttl_map entry 5
  // A6,   // TTL 7 — uncomment and add matching ttl_map entry
  // A7,   // TTL 8
  // A8,   // TTL 9
};

const uint8_t N_PINS = sizeof(ANALOG_PINS) / sizeof(ANALOG_PINS[0]);

// ---------------------------------------------------------------------------
// Derived threshold (raw ADC counts) — do not edit
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
// Loop — report state of every pin every iteration
// ---------------------------------------------------------------------------

void loop() {
  for (uint8_t i = 0; i < N_PINS; i++) {
    int raw        = analogRead(ANALOG_PINS[i]);
    int state      = (raw >= THRESHOLD_RAW) ? 1 : 0;
    int ttl_number = i + 1;   // 1-based, matches ttl_map list index + 1

    Serial.print("TTL:");
    Serial.print(ttl_number);
    Serial.print(":");
    Serial.println(state);
  }

  // Small delay to set the polling rate.
  // At 115200 baud each line is ~1 ms to transmit; 6 pins → ~6 ms per cycle.
  // 5 ms extra gives ~10 ms total cycle time (~100 Hz per pin), which comfortably
  // satisfies the 3-consecutive-reads debounce on the Python side.
  // Reduce this if you need faster response; increase if the serial buffer
  // overflows on boards with many pins.
  delay(5);
}
