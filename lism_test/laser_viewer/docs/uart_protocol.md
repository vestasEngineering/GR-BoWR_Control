# UART Steering Protocol

## JSON bring-up format

ASCII JSON followed by newline. Fields: `type`, `version`, `sequence`, `monotonic_us`, `valid`, `angle_deg`, `top_mm`, `bottom_mm`, `pair_age_us`, `reason`.

The receiver must reject missing fields, unsupported versions, non-finite numbers, old/repeated sequences, and data older than its local timeout.

## Binary format version 1

Little-endian, 38 bytes total:

- `uint16 magic`: `0xA55A`
- `uint8 version`: `1`
- `uint8 message_type`: `1`
- `uint32 sequence`
- `uint64 monotonic_us`
- `float32 angle_degrees`
- `float32 top_position_mm`
- `float32 bottom_position_mm`
- `float32 pair_age_us`
- `uint16 flags`
- `uint32 crc32`: CRC-32 over the preceding 34 bytes

Flags: bit 0 valid, bit 1 top valid, bit 2 bottom valid, bit 3 pair fresh. Invalid floating fields are NaN in binary and null in JSON. The MCU must check flags before reading angle fields.

This packet reports steering geometry only. It contains no speed request, motor-enable permission, or motion command.
