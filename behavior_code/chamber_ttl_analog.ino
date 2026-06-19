/*
  chamber_ttl_analog.ino
  =======================
  Reads analog voltage on multiple pins. When a pin's voltage crosses the
  threshold and stays high long enough to be confirmed, the pin's name
  (e.g. "A0") is sent once over USB serial. Nothing is sent while voltage
  is below the threshold or during the debounce period.

  The Python script (multiAcquisition.py) reads the received pin name and
  looks it up directly in the ttl_map block of config.yaml to decide which
  chamber to start/stop recording for.  No sequential numbering or TTL
  channel concept exists here — the analog pin name IS the identifier.

  Supported pins
  --------------
  This sketch supports up to A12 where the board provides them.  The
  ANALOG_PINS and PIN_NAMES arrays are built at compile time using
  NUM_ANALOG_INPUTS, which the Arduino IDE sets automatically for the
  selected board target:

      Board          NUM_ANALOG_INPUTS   Pins available
      Uno            6                   A0 – A5
      Nano           8                   A0 – A7
      Due            12                  A0 – A11
      Mega 2560      16                  A0 – A12  (capped here at A12)

  No editing is required when switching boards — the preprocessor includes
  only the pins that exist on the selected target.  You do not need to
  remove entries from config.yaml for boards with fewer pins; the Python
  side simply never receives events for pins that aren't compiled in.

  Pin → config.yaml correspondence
  ---------------------------------
  Each active pin in ANALOG_PINS must have a matching entry in config.yaml's
  ttl_map list with that same pin name:

      ANALOG_PINS entry   serial output   ttl_map "pin:" field
      A0                  "A0\n"          pin: A0
      A3                  "A3\n"          pin: A3

  Debounce
  --------
  CONFIRM_COUNT consecutive high readings (at the loop polling rate) are
  required before the pin name is transmitted.  Once sent, the pin is
  marked as fired and will not retransmit until the voltage drops back
  below the threshold, clearing the fired flag.

  Serial protocol
  ---------------
  On confirmed HIGH:   send "<pin_name>\n"  (e.g. "A0\n", "A3\n")
  On LOW / debounce:   send nothing

  Voltage threshold & board reference voltage
  -------------------------------------------
  Set BOARD_VREF to match your Arduino's logic level:
      5.0 → Uno, Mega, Nano  (5 V boards)
      3.3 → Due, Zero, MKR, and most 3.3 V boards
  THRESHOLD_FRACTION (default 0.5) sets the trigger point as a fraction
  of VREF, so the threshold is VREF / 2.  Adjust if your TTL source's
  "high" level sits closer to one rail than the other.

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

// Number of consecutive high readings required before firing.
// At the default ~10 ms loop time (5 ms delay + ~5 ms serial transmit),
// 3 reads means the signal must stay high for ~30 ms to be confirmed.
// Raise this if you see false triggers; lower it if real triggers are missed.
#define CONFIRM_COUNT 3

// Board reference voltage — CHANGE THIS to match your hardware.
//   5.0 for Uno / Mega / Nano   (5 V boards)
//   3.3 for Due / Zero / MKR   (3.3 V boards)
const float BOARD_VREF = 5.0;

// Fraction of VREF that counts as "high" / active.
//   0.5 → threshold at VREF / 2  (2.5 V on 5 V board, 1.65 V on 3.3 V board)
// Adjust if your TTL source's "high" level sits closer to one rail.
const float THRESHOLD_FRACTION = 0.5;

// ADC resolution — 1023 for the default 10-bit analogRead on most boards.
// (Due/Zero default to 10-bit unless analogReadResolution() is called.)
const int ADC_MAX = 1023;

// ---------------------------------------------------------------------------
// Pin list — built automatically for the selected board target
//
// NUM_ANALOG_INPUTS is a compile-time constant set by the Arduino IDE for
// each board.  The #if blocks below include only the pins that physically
// exist on the board being compiled for, so this file works without editing
// on Uno, Nano, Due, and Mega.
//
// Upper cap: A12 (13 pins).  Boards with more than 13 analog inputs
// (e.g. Mega with A0–A15) will only monitor A0–A12 from this sketch.
// Add A13/A14/A15 entries manually below if you need them.
//
// ANALOG_PINS and PIN_NAMES MUST stay in the same order and have the same
// number of entries — the compiler cannot check this for you.
// ---------------------------------------------------------------------------

const uint8_t ANALOG_PINS[] = {
  // ---- Always present: Uno (6), Nano (8), Due (12), Mega (16) ----
  A0, A1, A2, A3, A4, A5,

  // ---- 8-pin boards and above: Nano, Due, Mega ----
#if NUM_ANALOG_INPUTS >= 8
  A6, A7,
#endif

  // ---- 12-pin boards and above: Due, Mega ----
#if NUM_ANALOG_INPUTS >= 12
  A8, A9, A10, A11,
#endif

  // ---- 13 or more analog inputs: Mega (16), any board with 13+ ----
#if NUM_ANALOG_INPUTS >= 13
  A12,
#endif
};

// Human-readable pin names sent over serial.
// MUST mirror ANALOG_PINS exactly — same order, same conditionals.
const char* const PIN_NAMES[] = {
  "A0", "A1", "A2", "A3", "A4", "A5",

#if NUM_ANALOG_INPUTS >= 8
  "A6", "A7",
#endif

#if NUM_ANALOG_INPUTS >= 12
  "A8", "A9", "A10", "A11",
#endif

#if NUM_ANALOG_INPUTS >= 13
  "A12",
#endif
};

// Number of active pins, derived at compile time from the array size.
const uint8_t N_PINS = sizeof(ANALOG_PINS) / sizeof(ANALOG_PINS[0]);

// ---------------------------------------------------------------------------
// Derived threshold (raw ADC counts) — do not edit
// ---------------------------------------------------------------------------

const int THRESHOLD_RAW = (int)(ADC_MAX * THRESHOLD_FRACTION);

// ---------------------------------------------------------------------------
// Per-pin debounce state
// ---------------------------------------------------------------------------

// Maximum entries in the debounce arrays.  Must be >= the largest possible
// N_PINS (13 with the current pin list above).  16 gives headroom.
#define MAX_PINS 16

// How many consecutive high readings have been seen for each pin.
// Reset to 0 when the pin reads low.
uint8_t consec[MAX_PINS];

// True once a pin has fired for a sustained high period.
// Prevents re-sending while voltage stays high.
// Reset to false when the pin reads low so the next rise can fire again.
bool fired[MAX_PINS];

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

void setup() {
  // Initialise debounce state to zero / false for all slots.
  memset(consec, 0, sizeof(consec));
  memset(fired,  0, sizeof(fired));

  Serial.begin(BAUD_RATE);

  // "READY" is sent once at startup so the Python listener knows the port
  // is open and the sketch is running.  The listener ignores this line.
  Serial.println("READY");
}

// ---------------------------------------------------------------------------
// Loop — check each active pin and send its name once on confirmed HIGH
// ---------------------------------------------------------------------------

void loop() {
  for (uint8_t i = 0; i < N_PINS; i++) {
    int raw = analogRead(ANALOG_PINS[i]);

    if (raw >= THRESHOLD_RAW) {
      // Voltage is above threshold — increment the consecutive-high counter.
      // Cap at 255 to avoid uint8_t overflow on very long sustained highs.
      if (consec[i] < 255) consec[i]++;

      // Fire only when the confirmation count is reached AND we have not
      // already fired for this sustained high period.
      if (consec[i] >= CONFIRM_COUNT && !fired[i]) {
        Serial.println(PIN_NAMES[i]);   // e.g. "A0\n"
        fired[i] = true;
        // Stays silent for the rest of this high period.
      }

    } else {
      // Voltage dropped below threshold — reset both counters so the next
      // sustained rise can fire again independently.
      consec[i] = 0;
      fired[i]  = false;
    }
  }

  // Delay controls the polling rate per pin.
  // At 115200 baud, "A12\n" is 5 bytes (~0.4 ms to transmit).
  // 5 ms delay gives ~5.4 ms per full cycle across all active pins,
  // putting debounce confirmation at ~16 ms (3 reads x ~5.4 ms).
  // Reduce for faster response; raise if you see serial buffer overflows.
  delay(5);
}
