from __future__ import annotations

import argparse
import json
import signal
import time
from pathlib import Path

from .calibration import CalibrationStore, add_laser, build_dark
from .camera import LismCamera
from .config import Config
from .optimizer import ConfigOptimizer
from .processing import detect
from .service import AcquisitionService
from .web_server import serve

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "default.json"


def parser():
    root = argparse.ArgumentParser(description="LISM viewer, calibration, and configuration optimizer")
    root.add_argument("--config", default=str(DEFAULT_CONFIG))
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("calibrate-dark")
    commands.add_parser("calibrate-laser")
    live = commands.add_parser("live"); live.add_argument("--interval", type=float, default=0.1)
    server = commands.add_parser("serve"); server.add_argument("--host"); server.add_argument("--port", type=int)
    optimize = commands.add_parser("optimize-config")
    optimize_sub = optimize.add_subparsers(dest="optimize_command", required=True)
    dark = optimize_sub.add_parser("dark"); dark.add_argument("--output")
    for name in ("refine", "laser", "verify", "apply"):
        item = optimize_sub.add_parser(name); item.add_argument("--run", required=True)
        if name == "laser": item.add_argument("--label")
    return root


def calibration_command(config, base, laser: bool):
    store = CalibrationStore(config.resolve(base, config.calibration_directory))
    with LismCamera(config, base) as camera:
        signature = camera.signature(); camera.start()
        if laser:
            calibration = store.load()
            if calibration is None: raise RuntimeError("Dark calibration is required first")
            store.validate(calibration, signature)
            frames = camera.capture_frames(config.laser_frames, config.settle_frames)
            calibration = add_laser(calibration, frames, config.noise_sigma, config.minimum_signal, config.calibration_noise_sigma, config.calibration_minimum_signal, config.calibration_relative_threshold)
        else:
            calibration = build_dark(camera.capture_frames(config.dark_frames, config.settle_frames), signature)
        store.save(calibration)
        print(json.dumps({"dark_created_utc": calibration.created_utc, "laser_created_utc": calibration.laser_created_utc, "signature": signature.as_dict()}, indent=2))


def main(argv=None):
    args = parser().parse_args(argv)
    config, base = Config.load(args.config)
    if args.command == "status":
        with LismCamera(config, base) as camera: print(json.dumps(camera.signature().as_dict(), indent=2))
    elif args.command == "calibrate-dark": calibration_command(config, base, False)
    elif args.command == "calibrate-laser": calibration_command(config, base, True)
    elif args.command == "live":
        store = CalibrationStore(config.resolve(base, config.calibration_directory))
        with LismCamera(config, base) as camera:
            calibration = store.load()
            if calibration is None: raise RuntimeError("Run calibrate-dark first")
            store.validate(calibration, camera.signature()); camera.start()
            while True:
                result = detect(camera.read_frame(), calibration, config.noise_sigma, config.minimum_signal, config.centroid_half_width, config.minimum_region_width, config.maximum_region_width, config.clip_margin)
                print(f"\rdetected={result.detected} polarity={result.polarity} centroid={result.centroid} peak={result.peak_pixel} low_clip={result.low_clipped} high_clip={result.high_clipped}", end="", flush=True)
                time.sleep(max(0.0, args.interval))
    elif args.command == "serve":
        service = AcquisitionService(config, base); service.start()
        def stop(*_): service.stop(); raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop)
        host, port = args.host or config.host, args.port or config.port
        print(f"Open http://{host}:{port} or forward port {port} in VS Code")
        try: serve(service, host, port)
        except KeyboardInterrupt: pass
        finally: service.stop()
    elif args.command == "optimize-config":
        if args.optimize_command == "dark":
            output = Path(args.output).expanduser().resolve() if args.output else config.resolve(base, config.optimizer_directory) / time.strftime("run_%Y%m%d_%H%M%S")
        else:
            output = Path(args.run).expanduser().resolve()
        with LismCamera(config, base) as camera:
            optimizer = ConfigOptimizer(camera, config, output)
            if args.optimize_command == "dark": result = optimizer.dark_search()
            elif args.optimize_command == "refine": result = optimizer.refine_search()
            elif args.optimize_command == "laser": result = optimizer.laser_search(args.label)
            elif args.optimize_command == "verify": result = optimizer.verify()
            else: result = optimizer.apply()
        print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
