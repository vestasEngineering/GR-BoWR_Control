# File Inventory

All files are NEW FILES. No existing project file is replaced.

- `pyproject.toml`: package metadata and CLI entry point.
- `requirements.txt`: NumPy runtime dependency.
- `config/default.json`: validated hardware and runtime defaults.
- `laser_viewer/camera.py`: exclusive SDK adapter and packet validation.
- `laser_viewer/calibration.py`: dark/laser calibration and atomic persistence.
- `laser_viewer/processing.py`: thresholding, peak, saturation, and centroid.
- `laser_viewer/service.py`: single-owner acquisition thread, command serialization, snapshots, CSV logging.
- `laser_viewer/web_server.py`: dependency-free HTTP API and UI server.
- `laser_viewer/web/index.html`: live browser profile and controls.
- `laser_viewer/cli.py`: status, calibration, live, and server commands.
- `tests/`: synthetic automated tests.
- `docs/HARDWARE_TEST_CHECKLIST.md`: controlled target validation procedure.
