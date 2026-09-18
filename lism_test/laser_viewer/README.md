# LISM Laser Viewer

Headless continuous visualization and two-stage calibration for the verified S11639-01 + USB LISM-PI26xx system.

## Integration

Place this entire `laser_viewer/` directory beside the existing `sdk/` directory:

```text
lism_test/
├── sdk/
│   ├── usblismpi26.py
│   └── libusblismpi2664.so
└── laser_viewer/
```

The package does not replace or modify the vendor wrapper. All included source files are **NEW FILES**.

## Install

```bash
cd lism_test/laser_viewer
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

## Required operating sequence

1. Ensure the laser is OFF and the optical setup is in its normal operating state.
2. Run dark calibration.
3. Turn the laser ON and run laser-reference calibration.
4. Start live viewing or the browser server.

```bash
python -m laser_viewer --config config/default.json status
python -m laser_viewer --config config/default.json calibrate-dark
python -m laser_viewer --config config/default.json calibrate-laser
python -m laser_viewer --config config/default.json live
python -m laser_viewer --config config/default.json serve
```

For VS Code Remote, forward TCP port 8765, then open `http://127.0.0.1:8765`. To expose on a trusted LAN, use `serve --host 0.0.0.0`; the server has no authentication, so do not expose it to untrusted networks.

## Architecture and ownership

- Exactly one acquisition thread owns the SDK handle.
- Browser request threads never read the USB pipe.
- Calibration commands are serialized through the acquisition-owner command queue.
- Published visualization data is a latest-value snapshot. Slow clients do not accumulate frames.
- A USB timeout or SDK error transitions the service to `fault`. It does not auto-reopen or auto-resume.
- Calibration is bound to serial, packet length, pixel count, ST timing, edge delay, ADC gain, bias, and offset.
- Calibration writes use temporary files followed by atomic rename.

## Calibration

Dark calibration stores a per-pixel median baseline and per-pixel sample standard deviation. Laser calibration stores the median dark-corrected reference profile. Runtime thresholding uses the larger of `noise_sigma * pixel_noise` and `minimum_signal`. Detection reports the strongest above-threshold region and a weighted sub-pixel centroid in a bounded window.

The saved laser reference is retained for traceability and future reference-envelope checks. Runtime detection currently uses dark baseline and measured dark noise, not a forced positional lock to the reference centroid.

## Configuration

Defaults match the validated setup: 2048 pixels, 4096-byte packets, big-endian unsigned 16-bit samples, bias 768 expected from the device, and gain 0 expected from the device. This implementation verifies settings but does not write ADC or timing settings, preserving the existing device configuration.

## CSV logging

The browser buttons write timestamp, sequence, detection, peak, centroid, amplitude, and SNR to `logs/`. Raw 2048-pixel profiles are not logged by default to avoid high-volume storage.

## Tests

```bash
python -m unittest discover -s tests -v
```

Tests use synthetic arrays only. They validate calibration persistence, signature rejection, threshold detection, centroiding, and saturation reporting. They do not validate the proprietary ARM64 library, USB transport, optical behavior, or hardware timing.

## Rollback

Stop the process with Ctrl+C, remove the `laser_viewer/` folder, and leave the existing `sdk/`, scripts, captures, and reports unchanged. To roll back calibration only, back up and remove `calibration/active_calibration.npz` and `.json`, then rerun dark and laser calibration.

## Known limitations and risks

- The exact optical acceptance limits and physical pixel-to-distance mapping are not yet defined.
- The SDK binary and physical device are unavailable in the test environment, so target compilation/import and hardware acquisition remain unverified.
- The web API has no authentication or TLS.
- Laser safety remains a hardware/operational responsibility. Software calibration does not control or interlock the laser.
- If stopping the stream fails, cleanup still closes the handle, but the original SDK error is raised.
- Python cannot cancel a proprietary blocking ctypes call mid-call. The configured SDK timeout bounds normal waits.
