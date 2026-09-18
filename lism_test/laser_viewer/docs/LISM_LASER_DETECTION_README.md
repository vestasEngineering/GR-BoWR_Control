# LISM Laser Detection and Centroid Evaluation Development Notes

## Purpose

This document records the current architecture, confirmed operating behavior, experimental configuration, centroid methodologies, validation status, and next steps for detecting and locating a LAP galvo-scanned laser with the LISM line-sensor system.

The current system treats the sensor as a **saturated-background, negative-going laser detector** rather than as a conventional grayscale camera.

The key observed behavior is:

```text
No laser on a pixel:
Raw value remains near the 16-bit upper rail, approximately 65535.

Laser reaches a pixel:
Raw value falls sharply below the upper rail.
```

The application inspects every acquired frame, identifies coherent downward-going regions, retains triggered frames, and calculates several candidate center estimates from each detected footprint.

The current development objective is to determine which center-estimation method most reliably identifies the **geometric center of the detected laser footprint** across the observed response varieties.

---

## Current System

Current test system:

- Coptonix USB LISM-PI26xx interface
- 2048-pixel CMOS line sensor
- 4096-byte frame packet
- 16-bit decoded pixel values
- LAP galvo-scanning laser
- Laser wavelength: 520 nm
- Laser power: 5 mW
- Optical bandpass filter: nominal 10 nm bandwidth
- Approximate physical LAP scan length under test: 2 m
- Camera acquisition rate: approximately 1000 frames per second
- Observed LAP scan-associated modulation: approximately 49.63 Hz
- Observed modulation period: approximately 20.15 ms

The active sensor area is a narrow line. Optical alignment must ensure that the scanned laser crosses the actual photosensitive strip rather than only the sensor package or filter surface.

---

## Current Experimental Camera Configuration

The standard experimental configuration is now:

```text
ADC bias:   282
ADC gain:   15
ST high:    9000
ST low:     100
Edge delay: 88
```

These settings place most background pixels near the upper ADC rail while allowing laser-responsive pixels to fall substantially below the rail.

This is the current baseline for centroid evaluation. Do not change these camera settings between fixed-position comparison sessions unless the experiment is specifically intended to evaluate camera configuration.

These parameters remain temporary. They should not be written to EEPROM until long-duration stability, repeatability, restoration, and environmental behavior have been validated.

---

## Established Detection Model

### Downward response

For every pixel:

```text
drop[pixel] = 65535 - raw[pixel]
```

Examples:

```text
Raw value 65535 -> drop     0
Raw value 60000 -> drop  5535
Raw value 30000 -> drop 35535
Raw value   100 -> drop 65435
```

This converts the saturated-high background into a near-zero drop baseline and produces a positive-valued representation of the laser response.

### Initial active-pixel threshold

A pixel is active when:

```text
raw[pixel] < threshold
```

Current initial threshold:

```text
60000
```

Equivalent downward-drop threshold:

```text
65535 - 60000 = 5535
```

### Region construction

Adjacent active pixels are combined into contiguous candidate regions.

Small inactive gaps may be filled when the gap length is no greater than:

```text
maximum_gap
```

The current initial value is:

```text
maximum_gap = 3
```

This allows a small amount of threshold fragmentation without permitting unrestricted merging of independent artifacts.

A candidate region must satisfy:

- Minimum width
- Maximum width
- Minimum integrated downward drop

Current initial values:

```text
Minimum width:            2 pixels
Maximum width:          512 pixels
Minimum integrated drop: 10000
```

### Region selection

When multiple regions qualify, the detector currently selects the region with the greatest integrated downward drop:

```text
integrated_drop =
    sum(65535 - raw[pixel])
```

All qualifying-region diagnostics should eventually be retained during development so that a stronger artifact cannot silently hide a valid laser region.

---

## Confirmed Operating Behavior

### Sensor response

Confirmed:

- The sensor detects stationary laser illumination.
- The detected response moves when the physical laser position moves.
- The useful laser response is negative-going.
- The optical path and filter pass useful laser energy under the tested alignment.
- The background can be intentionally maintained near the upper ADC rail.
- A qualifying laser event can pull a broad region substantially below the rail.

### Galvo behavior

Confirmed:

- The useful galvo response is intermittent in the camera stream.
- The camera acquires approximately 20 frames during one observed 49.63 Hz modulation period.
- Only some frames contain the useful crossing.
- Browser refresh is much slower than camera acquisition.
- A live graph alone cannot be used to estimate event frequency.
- Trigger retention is required for operator inspection.

### Timing measurement

Repeated measurements identified a stable scan-associated modulation near:

```text
49.63 Hz
```

This corresponds to an observed period near:

```text
20.15 ms
```

This value is an observed modulation frequency. It is not necessarily identical to the physical galvo mechanical-cycle frequency because scan direction, gating, and multiple crossings may affect the relationship.

---

## Current Architecture

The current architecture separates high-rate acquisition from low-rate visualization.

```text
Camera acquisition thread
    |
    +-- Reads every camera frame
    |
    +-- Executes downward-region detection once
    |
    +-- Calculates all center methodologies
    |
    +-- Updates trigger counters and retained trigger state
    |
    +-- Sends frame and detection result to optional recorder
    |
    +-- Continues independently of browser refresh

Browser viewer
    |
    +-- Polls the current state at approximately 10 Hz
    |
    +-- Displays the latest live frame
    |
    +-- Displays a retained triggered frame
    |
    +-- Overlays candidate center methods
    |
    +-- Allows trigger-history browsing
```

### Camera ownership

Only one acquisition thread owns camera frame reading.

Camera configuration changes and frame reads are serialized using the existing camera lock.

No second camera reader should be added for centroid analysis, recording, WebSocket publication, or HMI integration.

### Detection ownership

The acquisition thread calls `detect_frame()` once per acquired frame.

All centroid methodologies derive from the exact region selected by that detector result.

The browser performs visualization only. The browser does not calculate authoritative center values.

### Recording ownership

The trigger recorder receives the same `DetectionResult` used by the viewer.

Recorder submission remains nonblocking. Disk writing must not delay camera acquisition.

---

## Current Viewer Behavior

The real-time viewer provides two separate displays.

### Live frame

The live graph continues updating with the newest acquired frame.

The live graph is not paused when trigger-history browsing is paused.

### Selected triggered frame

The selected-trigger graph displays a retained triggered frame and overlays the calculated center methods.

The selected-trigger graph supports:

- Follow latest trigger
- Pause trigger browser
- Previous trigger
- Next trigger
- Latest / Follow Live

### Frozen-history behavior

When the trigger browser is paused:

- Camera acquisition continues.
- Frame detection continues.
- Trigger counters continue.
- Good-trigger counters continue.
- `latest_trigger` continues updating.
- Recording continues.
- The live graph continues updating.
- The retained trigger-history buffer stops changing.
- Previous and Next browse the frozen history without old entries being overwritten.
- The viewer counts new triggers that were not added to the frozen browser history.

When Follow Live resumes:

1. The latest trigger is inserted once.
2. The selected display jumps to that trigger.
3. Normal bounded history updates resume.
4. The paused-trigger counter resets.

This memory-history pause does not pause acquisition or recording.

---

## Current Center-Estimation Methods

The system calculates several center estimates for the same detector-selected footprint.

The methods should not be averaged together. Each method represents a different characteristic of the response.

### 1. Boundary midpoint

```text
boundary_center =
    (region_start + region_end) / 2
```

This calculates the geometric midpoint of the threshold-selected region boundaries.

Advantages:

- Direct representation of footprint geometry
- Independent of amplitude variation inside the footprint
- Deterministic
- Easy to audit from region start and end

Limitations:

- Sensitive to isolated threshold-active pixels at either boundary
- Sensitive to threshold selection
- Sensitive to fragmentation or incorrect gap joining

### 2. Sustained-edge midpoint

The detector evaluates local active-pixel occupancy near each region edge.

Current initial parameters:

```text
Edge window:          5 pixels
Minimum active:       3 pixels
```

A five-pixel window must contain at least three active pixels to support an edge.

The center is:

```text
sustained_edge_center =
    (sustained_left_edge + sustained_right_edge) / 2
```

Advantages:

- Directly targets geometric center
- More resistant to isolated boundary pixels than the raw boundary midpoint
- Less sensitive to internal amplitude variation
- Suitable for broad and clipped responses

Limitations:

- Depends on the edge-window parameters
- Can be biased if one edge is heavily fragmented
- May be unavailable for narrow or sparsely active regions
- Still depends on the initial threshold mask

Current role:

```text
Primary candidate method
```

### 3. Drop-weighted centroid

```text
centroid =
    sum(pixel_index * drop[pixel])
    / sum(drop[pixel])
```

Advantages:

- Subpixel output
- Appropriate for smooth, symmetric, unsaturated profiles
- Uses the full downward-response distribution

Limitations:

- Measures center of downward signal energy, not necessarily geometric center
- Moves toward the deeper side of an asymmetric footprint
- Vulnerable to random deep samples
- Provides limited additional information for broad clipped plateaus

Current role:

```text
Diagnostic method
```

The drop-weighted centroid should not automatically become the production geometric-position output.

### 4. Drop-quantile midpoint

The detector finds positions containing selected cumulative fractions of the total downward drop.

Current initial quantiles:

```text
Low quantile:  10%
High quantile: 90%
```

The center is:

```text
quantile_center =
    (low_quantile_position + high_quantile_position) / 2
```

Advantages:

- Less sensitive to isolated amplitude extremes than a conventional weighted centroid
- Uses the distribution of downward response
- Supports subpixel interpolation
- Provides an independent cross-check for edge-based methods

Limitations:

- Can move if one shoulder contributes substantially more total drop
- Quantile values require experimental validation
- Depends on the selected region containing the complete footprint

Current role:

```text
Primary cross-check and fallback candidate
```

### 5. Deep-core midpoint

The detector identifies the strongest coherent low-valued portion of the region.

Current initial settings:

```text
Core fraction:       0.80
Minimum core width:  2 pixels
```

The center is:

```text
deep_core_center =
    (core_start + core_end) / 2
```

Advantages:

- Well suited to flat-bottom or strongly clipped responses
- Ignores shallow noisy shoulders
- Useful for distinguishing the strongest response core

Limitations:

- May be unavailable for noisy broad responses without a coherent core
- The core may not represent the full footprint center
- Requires an experimentally justified core threshold

Current role:

```text
Conditional diagnostic and cross-check
```

### 6. Active-pixel median

```text
active_median_center =
    median(active_pixel_indices)
```

Advantages:

- Independent of response amplitude
- Resistant to isolated amplitude outliers
- Computationally inexpensive

Limitations:

- Sensitive to uneven mask fragmentation
- Can shift when active-pixel density differs between the two sides
- Often duplicates the geometric midpoint for dense contiguous masks

Current role:

```text
Diagnostic method
```

### 7. Selected center

The current implementation uses:

```text
Primary:
Sustained-edge midpoint

Fallback:
Drop-quantile midpoint
```

The detector explicitly records:

```text
selected_center
selected_center_method
center_disagreement
center_confidence
measurement_quality
```

The fallback method is never hidden. A result produced by the quantile fallback is labeled differently from a sustained-edge result.

---

## Viewer Legend

Current overlay colors:

```text
Green:
Selected center
Sustained-edge primary or quantile fallback

Red:
Drop-weighted centroid

Yellow:
10%-90% drop-quantile midpoint

Purple:
Deep-core midpoint

Blue:
Boundary midpoint
```

The selected-trigger status reports values such as:

```text
Selected center
Selected method
Center confidence
Sustained-edge center
Quantile center
Drop-weighted centroid
Boundary midpoint
Deep-core midpoint
Active-pixel median
Method disagreement
Measurement-quality result
```

---

## Current Trigger and Centroid Configuration

Recommended starting configuration:

```text
Camera:
  ADC bias:                    282
  ADC gain:                     15
  ST high:                    9000
  ST low:                      100
  Edge delay:                   88

Trigger:
  Raw threshold:             60000
  Minimum width:                 2
  Maximum width:               512
  Minimum integrated drop:   10000
  Maximum gap:                   3

Centroid evaluation:
  Edge window:                   5
  Edge minimum active:           3
  Low drop quantile:          0.10
  High drop quantile:         0.90
  Deep-core fraction:         0.80
  Deep-core minimum width:       2
  Maximum center disagreement:  12 pixels

Viewer:
  Trigger history size:        128
  Port:                       8769
```

Recommended command:

```bash
python -m laser_viewer.raw_pixel_viewer   --config config/default.json   --bias 282   --gain 15   --st-high 9000   --st-low 100   --edge-delay 88   --threshold 60000   --minimum-width 2   --maximum-width 512   --minimum-integrated-drop 10000   --maximum-gap 3   --edge-window 5   --edge-minimum-active 3   --quantile-low 0.10   --quantile-high 0.90   --core-fraction 0.80   --core-minimum-width 2   --maximum-center-disagreement 12   --history-size 128   --port 8769
```

Open:

```text
http://127.0.0.1:8769
```

---

# Next Development Phase: Centroid Algorithm Downselection

## Objective

The next phase is to identify which center method most reliably estimates the geometric center of the detected laser footprint.

The method should perform consistently across:

- Broad noisy troughs
- Deep clipped plateaus
- Asymmetric shoulders
- Internally fragmented responses
- Variations in integrated drop
- Variations in footprint width
- Repeated captures at the same physical position
- Return to previously tested physical positions

The method should be selected from measured data, not from how convincing a single frame looks.

---

## Key Experimental Principle

The absolute physical center is not known from a raw frame alone.

Therefore, the downselection must use a combination of:

1. Fixed-position repeatability
2. Return-position repeatability
3. Monotonic movement between physical positions
4. Robustness across response shapes
5. Sensitivity to trigger and centroid parameters
6. Valid-result yield
7. Resistance to malformed and false-trigger frames
8. Event-to-event stability
9. Agreement with an independently defined physical reference when available

A method that produces a stable but consistently biased position may still appear good in fixed-position statistics. Eventually, a physical reference or calibration fixture is required to evaluate absolute geometric accuracy.

---

## Required Data Collection Sessions

Collect the following sessions while keeping camera and trigger settings unchanged.

### 1. Scan-off session

Label:

```text
off
```

Duration:

```text
At least 30 seconds
```

Purpose:

- Measure false-trigger rate
- Identify common false-region widths
- Identify false-trigger center distributions
- Determine whether a fixed artifact dominates
- Determine whether any center method produces convincing but invalid stability

### 2. Left fixed-position session

Label:

```text
left
```

Target:

```text
At least 100 valid triggered frames
```

Purpose:

- Measure fixed-position spread
- Establish the left ordering point
- Characterize response-shape variation at the left position

### 3. Center fixed-position session

Label:

```text
center
```

Target:

```text
At least 100 valid triggered frames
```

Purpose:

- Measure fixed-position spread near the center
- Compare all methods against the widest range of observed response shapes
- Establish the initial reference point for return tests

### 4. Right fixed-position session

Label:

```text
right
```

Target:

```text
At least 100 valid triggered frames
```

Purpose:

- Measure fixed-position spread
- Establish the right ordering point
- Detect any position-dependent method bias

### 5. Return-center session

Label:

```text
return_center
```

Target:

```text
At least 100 valid triggered frames
```

Purpose:

- Measure return-position error
- Detect hysteresis, mechanical repositioning differences, or threshold-dependent drift
- Compare the second center session with the original center session

### 6. Return-left session

Recommended additional label:

```text
return_left
```

Target:

```text
At least 100 valid triggered frames
```

Purpose:

- Add a second return-position check
- Avoid selecting a method based only on one repeated position

### 7. Long-duration session

Label:

```text
long_run
```

Initial duration:

```text
5 minutes
```

Final duration:

```text
At least 1 hour
```

Purpose:

- Measure centroid drift
- Measure footprint-width drift
- Monitor memory and recorder behavior
- Monitor method-disagreement frequency
- Detect configuration or temperature-dependent changes

---

## Session Isolation

Before each session:

1. Hold the physical laser or scan position stable.
2. Confirm the camera configuration remains:

```text
282 / 15 / 9000 / 100 / 88
```

3. Confirm the trigger configuration is unchanged.
4. Clear the viewer history.
5. Start a new recording session.
6. Do not combine multiple physical positions in the same session.
7. Record the physical target name and any external reference measurement.

Do not change threshold, gap size, quantiles, or edge settings between the primary left, center, right, and return sessions.

If a parameter is changed, begin a new explicitly labeled experiment series.

---

## Required Per-Frame Data

Each triggered frame should retain:

```text
Session identity
Frame number
Monotonic timestamp
Raw 2048-pixel frame
Trigger configuration ID
Camera configuration
Region start
Region end
Region width
Integrated drop
Minimum raw value
Peak drop
Active coverage
Largest internal gap
Filled gap count
Filled gap pixels
Background rail fraction
Interior low fraction
Shape score
Measurement quality
Boundary midpoint
Sustained left edge
Sustained right edge
Sustained-edge midpoint
Low quantile position
High quantile position
Quantile midpoint
Drop-weighted centroid
Deep-core start
Deep-core end
Deep-core midpoint
Active-pixel median
Selected center
Selected method
Center disagreement
Center confidence
```

Raw frames must be retained. This allows alternative parameters and methods to be replayed against identical data without repeating the physical experiment.

---

## Method Metrics

For every session and every method, calculate:

```text
Total triggered frames
Method-valid frame count
Method-invalid frame count
Valid-result fraction
Mean center
Median center
Standard deviation
Median absolute deviation
Minimum center
Maximum center
5th percentile
95th percentile
Peak-to-peak range
Outlier count
```

Median absolute deviation is important because a small number of malformed frames can inflate standard deviation.

A robust outlier rule can initially use:

```text
Median +/- 5 times the median absolute deviation
```

The exact outlier policy should be reported and should not silently delete measurements from the primary statistics.

---

## Return-Position Error

For each method, compare the original and returned sessions.

Example:

```text
Center return error =
    median(return_center)
    - median(center)
```

Also calculate:

```text
Absolute median return error
Difference in standard deviation
Difference in median absolute deviation
Difference in valid-result fraction
```

A strong method should return close to the prior value without requiring the response amplitude or clipped-core shape to be identical.

---

## Monotonicity

For each method, calculate the session medians:

```text
left_median
center_median
right_median
```

A usable method must be monotonic:

```text
left < center < right
```

or:

```text
left > center > right
```

The direction depends on physical sensor orientation.

No method should pass downselection if its median ordering changes across repeated experiments.

Also calculate separation between adjacent positions:

```text
center_to_left_separation
right_to_center_separation
```

The separation should be large relative to fixed-position spread.

For example:

```text
separation_ratio =
    adjacent_position_separation
    / maximum(fixed_position_standard_deviation)
```

Higher separation ratios are preferable.

---

## Shape-Sensitivity Analysis

Classify or group triggered frames by response characteristics:

- Narrow versus broad footprint
- Low versus high integrated drop
- Low versus high active coverage
- Deep-core present versus absent
- Small versus large internal gap
- Low versus high background rail fraction
- Measurement quality accepted versus rejected
- Small versus large method disagreement

Then calculate the center distribution for each method within each group.

This exposes behaviors such as:

```text
Weighted centroid shifts when one side becomes deeper.

Boundary midpoint shifts when a marginal edge pixel appears.

Deep-core midpoint is stable only when a clipped plateau exists.

Quantile midpoint changes as shoulder energy changes.

Sustained-edge midpoint remains stable until edge occupancy degrades.
```

---

## Parameter-Sensitivity Analysis

Replay the saved raw frames with controlled parameter variations.

### Threshold sweep

Suggested initial values:

```text
55000
57500
60000
62500
```

Purpose:

- Determine how strongly each center method depends on the active-pixel threshold
- Measure boundary movement
- Detect methods that appear stable only at one threshold

### Maximum-gap sweep

Suggested values:

```text
0
1
3
5
8
```

Purpose:

- Determine when fragmented physical responses are correctly joined
- Detect when independent artifacts begin to merge

### Edge-window sweep

Suggested combinations:

```text
Window 3, minimum active 2
Window 5, minimum active 3
Window 7, minimum active 4
```

Purpose:

- Measure resistance to isolated edge pixels
- Detect excessive edge erosion
- Determine whether sustained-edge center is stable across reasonable settings

### Quantile sweep

Suggested pairs:

```text
5% and 95%
10% and 90%
20% and 80%
```

Purpose:

- Determine sensitivity to broad shoulders and asymmetric drop distribution
- Compare method repeatability and return error

### Deep-core sweep

Suggested core fractions:

```text
0.60
0.70
0.80
0.90
```

Purpose:

- Determine whether deep-core center is stable across clipped and non-clipped response shapes
- Quantify the method-valid fraction

Only one parameter family should change at a time.

---

## Candidate Downselection Criteria

A center method should remain under consideration only if it satisfies all required criteria.

### Required criteria

1. Produces valid results for a high fraction of true triggered frames.
2. Maintains consistent physical ordering across left, center, and right.
3. Produces low fixed-position spread.
4. Produces low return-position error.
5. Does not generate stable false positions during scan-off testing.
6. Does not depend excessively on one narrow threshold setting.
7. Handles all major observed response varieties.
8. Records explicit invalid status when the method cannot produce a defensible result.
9. Does not silently reuse an old center.
10. Can run on every acquired frame without compromising acquisition.

### Preferred criteria

1. Uses geometric footprint information rather than amplitude dominance.
2. Has understandable failure modes.
3. Produces auditable boundary or quantile diagnostics.
4. Requires few tunable parameters.
5. Remains stable across sensor position.
6. Remains stable across integrated-drop variation.
7. Provides a reliable confidence cross-check.

---

## Proposed Ranking Method

Do not rank methods using only standard deviation.

Create a normalized score from several measured components:

```text
Repeatability component
Return-error component
Valid-yield component
Monotonicity component
False-trigger component
Parameter-sensitivity component
Shape-robustness component
```

A conceptual score is:

```text
method_score =
    repeatability_score
    + return_score
    + valid_yield_score
    + monotonicity_score
    + false_trigger_score
    + parameter_stability_score
    + shape_robustness_score
```

However, do not allow a high total score to compensate for a critical failure.

Use hard rejection gates first:

```text
Reject if physical ordering is not monotonic.

Reject if scan-off false triggers produce a dominant stable position.

Reject if return-position error exceeds the experimental tolerance.

Reject if the valid-result fraction is too low.

Reject if the method silently falls back or returns stale data.

Reject if the method fails on one of the common response families.
```

Only methods passing all gates should be ranked.

The numerical acceptance tolerances should be derived after the first dataset is collected. They should not be declared as production limits before the system’s natural variation is measured.

---

## Expected Initial Outcome

The current engineering expectation is:

```text
Primary candidate:
Sustained-edge midpoint

Primary cross-check:
10%-90% drop-quantile midpoint

Conditional cross-check:
Deep-core midpoint

Geometric diagnostic:
Boundary midpoint

Amplitude-sensitive diagnostic:
Drop-weighted centroid

Mask-distribution diagnostic:
Active-pixel median
```

This is an initial hypothesis, not the final result.

The sustained-edge method is expected to perform well because the desired quantity is the geometric midpoint of the footprint. The quantile method is expected to provide a useful independent check. The weighted centroid is expected to move under amplitude asymmetry and may therefore be less appropriate as the final geometric-position output.

The collected data must be allowed to disprove these expectations.

---

## Proposed Final Selection Policy

A likely production policy is:

```text
1. Detect a qualifying footprint.
2. Calculate sustained-edge midpoint.
3. Calculate drop-quantile midpoint.
4. Compare the methods.
5. Accept the sustained-edge midpoint when:
   - both sustained edges are present,
   - region width is valid,
   - active coverage is sufficient,
   - background rail fraction is valid,
   - internal gaps are acceptable,
   - and quantile disagreement is below tolerance.
6. Otherwise report invalid or an explicitly labeled fallback.
```

Do not automatically average disagreeing methods.

If quantile fallback is retained, downstream consumers must receive:

```text
position_pixels
position_method
position_valid
center_confidence
center_disagreement
```

A fallback result must not be presented as equivalent to a high-confidence sustained-edge result.

---

## Automated Test Status

The project test suite currently covers:

- Downward-region detection
- Weighted-centroid behavior
- Boundary-center behavior
- Single-pixel glitch rejection
- Strongest-region selection
- Small-gap joining
- Large-gap separation
- Broad noisy responses
- Clipped-bottom responses
- Amplitude asymmetry
- Configuration validation
- Fast optimizer and brute-force consistency
- Scan-rate frequency recovery
- Intermittent crossing detection
- Camera restoration retries
- Restoration-failure persistence
- Existing positive and negative processing paths

The simulated restoration warning is expected:

```text
WARNING: Original configuration restoration failed:
simulated restore failure
```

Software tests validate implemented calculations and regression behavior. They do not establish physical accuracy or machine safety.

---

## Current Validation Status

### Confirmed

- Camera enumeration and status readback work.
- 2048-pixel frames are acquired.
- Temporary camera configuration and restoration work in current tests.
- Stationary laser illumination creates a negative-going spatial response.
- The response moves with physical laser movement.
- LAP scan-on data contains repeatable modulation near 49.63 Hz.
- Per-frame triggering captures brief useful frames.
- The browser can retain and display triggered frames.
- The live graph remains active during trigger-history browsing.
- Trigger history can be frozen for frame-by-frame inspection.
- Multiple center methodologies are calculated from the same detector result.
- Duplicate detector execution in the acquisition path has been removed.
- Current software tests pass after restoring shared quality-configuration compatibility.

### Not yet confirmed

- Best authoritative center method
- Fixed-position center standard deviation
- Return-position error
- Method validity yield on real frames
- Method behavior over the full scan range
- Sensitivity to trigger threshold
- Sensitivity to maximum-gap configuration
- Sensitivity to edge-window configuration
- Long-duration drift
- Production false-trigger rate
- Production missed-trigger rate
- One-event-per-crossing behavior
- Physical-unit accuracy
- Environmental stability
- Behavior after camera disconnect and reconnect
- Behavior after LAP restart
- Production WebSocket event format
- Runtime-service integration
- Database integration
- HMI validity display
- Final calibration format
- Safe production defaults
- EEPROM configuration

---

## Hardware Validation Guidance

Begin testing with the fixture mechanically secured and any downstream motion or tooling isolated.

The centroid viewer is an experimental measurement tool. A valid laser estimate must not act as permission to start or resume motion.

Required safety behavior remains:

```text
No automatic motion after camera reconnection.
No automatic motion after LAP restart.
No automatic motion after Linux-service restart.
No automatic motion after timeout recovery.
No automatic motion after fault clearing.
No use of a stale center as a current position.
No silent fallback from invalid to last-known position.
```

A new valid trigger must be received before the position may be considered fresh after startup or recovery.

---

## Rollback and Restoration

The viewer applies camera settings temporarily.

Stop the viewer with:

```text
Ctrl+C
```

Verify camera restoration:

```bash
python -m laser_viewer   --config config/default.json   status
```

The restored settings should match the startup configuration.

Development deliverables must continue using complete source files or complete replacement units. Do not distribute Git patches, unified diffs, `.patch` files, or `.diff` files.

---

## Current Recommended Direction

Use the fixed camera configuration:

```text
Bias 282
Gain 15
ST high 9000
ST low 100
Edge delay 88
```

Then proceed in this order:

```text
1. Record scan-off data.
2. Record LEFT fixed-position data.
3. Record CENTER fixed-position data.
4. Record RIGHT fixed-position data.
5. Record RETURN CENTER data.
6. Record RETURN LEFT data.
7. Run every center method against identical saved frames.
8. Calculate repeatability, return error, yield, and monotonicity.
9. Replay parameter sweeps against the saved raw frames.
10. Apply rejection gates.
11. Rank only the surviving methods.
12. Select the authoritative center algorithm.
13. Define validity and freshness behavior.
14. Integrate the selected method into the single-owner runtime service.
15. Perform geometric pixel-to-physical-position calibration.
```

The immediate goal is not to add more center algorithms. The immediate goal is to collect enough controlled data to determine which of the existing methods is reliable.

The current working principle is:

> The sensor background is intentionally held near the 16-bit upper rail using ADC bias 282, gain 15, ST high 9000, ST low 100, and edge delay 88. A laser crossing creates a brief negative-going footprint. Every camera frame is inspected, qualifying footprints are retained, and several center methodologies are calculated from the same detector-selected region. The final center method will be selected using measured repeatability, return error, monotonicity, valid-result yield, false-trigger behavior, and parameter sensitivity.
