/*
  chamber_ttl_analog.ino
  =======================
  Reads analog voltage on multiple pins. When a pin's voltage crosses
  the threshold and stays high long enough to be confirmed, the pin's
  name (e.g. "A0") is sent once over USB serial. Nothing is sent while
  voltage is below the threshold or during the debounce period.

  The Python script (multiAcquisition.py) reads the received pin name
  and looks it up directly in the ttl_map block of config.yaml to decide
  which chamber to start/stop recording for.  No sequential numbering or
  TTL channel concept exists here — the analog pin name IS the identifier.

  Pin → config.yaml correspondence
  ---------------------------------
  Each pin in ANALOG_PINS must have a matching entry in config.yaml's
  ttl_map list with that same pin name:

      ANALOG_PINS entry   serial output   ttl_map "pin:" field
      A0                  "A0\n"          pin: A0
      A3                  "A3\n"          pin: A3

  Only pins that are listed in ANALOG_PINS are monitored. Order within
  ANALOG_PINS does not matter to the Python side — the pin name is the
  key, not the array position.

  Debounce
  --------
  A single high reading is not trusted — analog signals near the threshold
  can bounce.  CONFIRM_COUNT consecutive high readings (at the loop polling
  rate) are required before the pin name is sent.  Once sent, the pin is
  marked as fired and will not send again until the voltage drops back
  below the threshold, clearing the fired flag.

  Serial protocol
  ---------------
  On confirmed HIGH:   send "<pin_name>\n"  (e.g. "A0\n", "A3\n")
  On LOW / debounce:   send nothing

  This replaces the previous "TTL:<number>:<state>\n" protocol that sent
  all pin states on every loop. The new protocol only transmits on events,
  reducing serial traffic and moving all decision logic to config.yaml.

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
// At the default ~10 ms loop time (5 ms delay + ~5 ms serial), 3 reads
// means the signal must stay high for ~30 ms before it is confirmed.
// Raise this if you see false triggers; lower it if real triggers are missed.
#define CONFIRM_COUNT 3

// Board reference voltage — CHANGE THIS to match your hardware.
//   5.0 for Uno/Mega/Nano,  3.3 for Due/Zero/MKR
const float BOARD_VREF = 5.0;

// Fraction of VREF that counts as "high" / active.
//   0.5 → threshold at VREF / 2  (2.5 V on a 5 V board, 1.65 V on 3.3 V)
const float THRESHOLD_FRACTION = 0.5;

// ADC resolution — 1023 for the default 10-bit analogRead on most boards.
// (Due/Zero default to 10-bit unless analogReadResolution() is called.)
const int ADC_MAX = 1023;

// ---------------------------------------------------------------------------
// Pin list — MUST stay in sync with PIN_NAMES below
//
// Add, remove, or reorder pins here to match what is physically wired.
// The Python side does not care about order — it matches by pin name.
// ---------------------------------------------------------------------------

const uint8_t ANALOG_PINS[] = {
  A0,   // matched by PIN_NAMES[0] = "A0"
  A1,   // matched by PIN_NAMES[1] = "A1"
  A2,   // matched by PIN_NAMES[2] = "A2"
  A3,   // matched by PIN_NAMES[3] = "A3"
  A4,   // matched by PIN_NAMES[4] = "A4"
  A5,   // matched by PIN_NAMES[5] = "A5"
  // A6,   // uncomment and add "A6" to PIN_NAMES to enable
  // A7,
  // A8,
};

// Human-readable pin names sent over serial.
// MUST have the same number of entries and the same order as ANALOG_PINS.
const char* const PIN_NAMES[] = {
  "A0",
  "A1",
  "A2",
  "A3",
  "A4",
  "A5",
  // "A6",
  // "A7",
  // "A8",
};

const uint8_t N_PINS = sizeof(ANALOG_PINS) / sizeof(ANALOG_PINS[0]);

// ---------------------------------------------------------------------------
// Derived threshold (raw ADC counts) — do not edit
// ---------------------------------------------------------------------------

const int THRESHOLD_RAW = (int)(ADC_MAX * THRESHOLD_FRACTION);

// ---------------------------------------------------------------------------
// Per-pin debounce state — allocated to a safe maximum
// ---------------------------------------------------------------------------

// Maximum number of pins this sketch supports without recompiling.
// Only the first N_PINS entries are used.
#define MAX_PINS 16

// How many consecutive high readings have been seen for each pin.
// Reset to 0 when the pin reads low.
uint8_t consec[MAX_PINS];

// True once a pin has fired for a sustained high period.
// Prevents re-sending on every subsequent high reading while voltage stays up.
// Reset to false when the pin reads low so the next rise can fire again.
bool fired[MAX_PINS];

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

void setup() {
  // Initialise debounce state arrays to zero / false.
  memset(consec, 0, sizeof(consec));
  memset(fired,  0, sizeof(fired));

  Serial.begin(BAUD_RATE);
  // "READY" is sent once at startup so the Python listener knows the port
  // is open and the sketch is running.  The listener ignores this line.
  Serial.println("READY");
}

// ---------------------------------------------------------------------------
// Loop — check each pin and send its name once on confirmed HIGH
// ---------------------------------------------------------------------------

void loop() {
  for (uint8_t i = 0; i < N_PINS; i++) {
    int raw = analogRead(ANALOG_PINS[i]);

    if (raw >= THRESHOLD_RAW) {
      // Voltage is above threshold — increment the consecutive-high counter.
      if (consec[i] < 255) consec[i]++;   // cap to avoid uint8_t overflow

      // Fire only when the confirmation count is reached AND we have not
      // already fired for this sustained high period.
      if (consec[i] >= CONFIRM_COUNT && !fired[i]) {
        Serial.println(PIN_NAMES[i]);   // e.g. "A0\n"
        fired[i] = true;
      }
      // If already fired, do nothing — the pin stays silent until it goes low.

    } else {
      // Voltage dropped below threshold — reset both counters so the next
      // sustained rise can fire again independently.
      consec[i] = 0;
      fired[i]  = false;
    }
  }

  // Delay sets the polling rate per pin.
  // At 115200 baud, "A0\n" is ~3 bytes ≈ 0.26 ms; delay(5) gives ~5 ms per
  // full cycle across all pins, putting debounce confirmation at ~15 ms
  // (3 reads × 5 ms).  Reduce for faster response; raise if pins are noisy.
  delay(5);
}
