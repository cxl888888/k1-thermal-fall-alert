"""Calibrated MLX90640 thermal display for the SpacemiT K1.

Uses Adafruit's open-source MLX90640 temperature/calibration driver and adapts
its CircuitPython I2C interface to the project's existing smbus2 transport.
The sensor and any later algorithm still receive the native 24x32 frame; only
the on-screen preview is enlarged for readability.
"""

from __future__ import annotations

import sys
import time
import csv
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from smbus2 import SMBus, i2c_msg

try:
    import adafruit_mlx90640
except ImportError as exc:  # Give a useful message on a fresh K1 install.
    raise SystemExit(
        "Missing Adafruit MLX90640 driver. Install the Adafruit CircuitPython "
        "MLX90640, Bus Device, and Register packages first."
    ) from exc

# The Adafruit driver defaults to very large I2C reads. Match the 32-word
# transaction size already used by the working K1 preview to suit its adapter.
adafruit_mlx90640.I2C_READ_LEN = 32
MLX90640 = adafruit_mlx90640.MLX90640


# Keep these aligned with the existing K1 project wiring.
I2C_BUS = 4
I2C_ADDRESS = 0x33
SENSOR_REFRESH_CODE = 3  # MLX90640 code 3 = 4 Hz per subpage (~2 complete frames/s).
REFRESH_RATES_HZ = (0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0)

STARTUP_DISCARD_FRAMES = 4  # Reads dropped right after rate config; early frames are often stale.
MIN_VALID_PIXELS = 384      # Below half of 32x24 the frame is recorded as invalid.

SENSOR_WIDTH = 32
SENSOR_HEIGHT = 24
PIXEL_COUNT = SENSOR_WIDTH * SENSOR_HEIGHT

WINDOW_NAME = "MLX90640 Calibrated Thermal View"
FALLBACK_SCREEN = (800, 480)
COLOR_MAP = cv2.COLORMAP_INFERNO

# Used when automatic contrast is switched off with the A key.
FIXED_TEMP_MIN_C = 18.0
FIXED_TEMP_MAX_C = 36.0


class SmbusI2CAdapter:
    """Small CircuitPython-I2C-compatible wrapper around the project's smbus2."""

    def __init__(self, bus_number: int) -> None:
        self._bus = SMBus(bus_number)
        self._locked = False
        self.reset_frame_stats()

    def reset_frame_stats(self) -> None:
        self._frame_i2c_transactions = 0
        self._frame_i2c_read_bytes = 0
        self._frame_i2c_write_bytes = 0
        self._frame_i2c_seconds = 0.0

    def frame_stats(self) -> dict[str, float | int]:
        return {
            "i2c_transactions": self._frame_i2c_transactions,
            "i2c_read_bytes": self._frame_i2c_read_bytes,
            "i2c_write_bytes": self._frame_i2c_write_bytes,
            "i2c_time_ms": self._frame_i2c_seconds * 1000.0,
        }

    def _transfer(self, messages: tuple[Any, ...], read_bytes: int, write_bytes: int) -> None:
        started = time.perf_counter()
        self._frame_i2c_transactions += 1
        self._frame_i2c_read_bytes += read_bytes
        self._frame_i2c_write_bytes += write_bytes
        try:
            self._bus.i2c_rdwr(*messages)
        finally:
            self._frame_i2c_seconds += time.perf_counter() - started

    def try_lock(self) -> bool:
        if self._locked:
            return False
        self._locked = True
        return True

    def unlock(self) -> None:
        self._locked = False

    def writeto(
        self,
        address: int,
        buffer: Any,
        start: int = 0,
        end: int | None = None,
        stop: bool = True,
    ) -> None:
        del stop  # smbus2's i2c_rdwr issues a normal stopped write.
        data = bytes(memoryview(buffer)[start:end])
        if not data:
            # Adafruit's I2CDevice probes the address with an empty write.
            # Some Linux I2C adapters reject zero-length messages, so use a
            # harmless one-byte read as the presence probe instead.
            probe = i2c_msg.read(address, 1)
            self._transfer((probe,), read_bytes=1, write_bytes=0)
            return
        self._transfer((i2c_msg.write(address, data),), read_bytes=0, write_bytes=len(data))

    def readfrom_into(
        self,
        address: int,
        buffer: Any,
        start: int = 0,
        end: int | None = None,
        stop: bool = True,
    ) -> None:
        del stop
        target = memoryview(buffer)[start:end]
        message = i2c_msg.read(address, len(target))
        self._transfer((message,), read_bytes=len(target), write_bytes=0)
        target[:] = bytes(message)

    def writeto_then_readfrom(
        self,
        address: int,
        out_buffer: Any,
        in_buffer: Any,
        out_start: int = 0,
        out_end: int | None = None,
        in_start: int = 0,
        in_end: int | None = None,
        stop: bool = False,
    ) -> None:
        del stop
        output = bytes(memoryview(out_buffer)[out_start:out_end])
        target = memoryview(in_buffer)[in_start:in_end]
        write_message = i2c_msg.write(address, output)
        read_message = i2c_msg.read(address, len(target))
        # One i2c_rdwr call keeps the register-address write and read together
        # with a repeated start, as required by MLX90640 register reads.
        self._transfer(
            (write_message, read_message), read_bytes=len(target), write_bytes=len(output)
        )
        target[:] = bytes(read_message)

    def close(self) -> None:
        self._bus.close()


class DiagnosticsRecorder:
    """Stream per-frame timing, temperature variation, and native pixels to CSV."""

    METRIC_COLUMNS = [
        "frame_id", "timestamp", "status", "scene_label", "refresh_code", "subpage_target_hz",
        "frame_rate_fps", "sensor_read_ms", "i2c_transactions", "i2c_read_bytes",
        "i2c_write_bytes", "i2c_time_ms", "sensor_non_i2c_ms", "render_ms",
        "display_ms", "loop_ms", "temp_min_c", "temp_p05_c", "temp_median_c",
        "temp_p95_c", "temp_max_c", "temp_std_c", "mean_abs_delta_c",
        "p95_abs_delta_c", "max_abs_delta_c", "error",
    ]
    PIXEL_COLUMNS = [f"t_r{row:02d}_c{column:02d}"
                     for row in range(SENSOR_HEIGHT) for column in range(SENSOR_WIDTH)]

    def __init__(self, output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.csv_path = output_dir / f"thermal_diagnostic_{stamp}.csv"
        self.plot_path = output_dir / f"thermal_diagnostic_{stamp}.png"
        self._file = self.csv_path.open("w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(
            self._file, fieldnames=self.METRIC_COLUMNS + self.PIXEL_COLUMNS
        )
        self._writer.writeheader()
        self._chart_rows: list[dict[str, Any]] = []

    def write(self, row: dict[str, Any], frame: np.ndarray | None = None) -> None:
        output = {key: row.get(key, "") for key in self.METRIC_COLUMNS}
        if frame is not None:
            flat = frame.reshape(-1)
            good = valid_temperature_frame(frame).reshape(-1)
            for index, column in enumerate(self.PIXEL_COLUMNS):
                output[column] = f"{float(flat[index]):.3f}" if good[index] else ""
        else:
            for column in self.PIXEL_COLUMNS:
                output[column] = ""
        self._writer.writerow(output)
        self._chart_rows.append(dict(row))
        if len(self._chart_rows) % 10 == 0:
            self._file.flush()

    def finish(self) -> None:
        self._file.flush()
        self._file.close()
        self._write_plot()

    def _write_plot(self) -> None:
        good_rows = [row for row in self._chart_rows if row.get("status") == "ok"]
        canvas = np.full((780, 1280, 3), (247, 248, 250), dtype=np.uint8)
        cv2.putText(canvas, "MLX90640 RUN DIAGNOSTICS", (24, 34),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (35, 42, 52), 2, cv2.LINE_AA)
        cv2.putText(canvas, f"Successful frames: {len(good_rows)}  |  Attempts: {len(self._chart_rows)}",
                    (24, 62), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (75, 84, 96), 1, cv2.LINE_AA)

        plots = [
            ("Actual frame rate (fps)", "frame_rate_fps", (204, 91, 35)),
            ("Sensor read time (ms)", "sensor_read_ms", (42, 126, 190)),
            ("I2C time inside sensor read (ms)", "i2c_time_ms", (63, 145, 91)),
            ("Temperature median / 95th percentile (C)",
             ("temp_median_c", "temp_p95_c"), ((120, 76, 175), (35, 160, 205))),
            ("Mean absolute change from previous frame (C)", "mean_abs_delta_c", (30, 145, 170)),
            ("Non-I2C sensor time (ms)", "sensor_non_i2c_ms", (170, 112, 35)),
        ]
        panel_w, panel_h = 600, 218
        for index, (title, fields, colors) in enumerate(plots):
            col, row_index = index % 2, index // 2
            x0, y0 = 24 + col * 626, 82 + row_index * 226
            x1, y1 = x0 + panel_w, y0 + panel_h
            cv2.rectangle(canvas, (x0, y0), (x1, y1), (255, 255, 255), -1)
            cv2.rectangle(canvas, (x0, y0), (x1, y1), (215, 220, 226), 1)
            cv2.putText(canvas, title, (x0 + 12, y0 + 22),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.44, (43, 50, 60), 1, cv2.LINE_AA)
            graph = (x0 + 44, y0 + 38, x1 - 12, y1 - 28)
            gx0, gy0, gx1, gy1 = graph
            cv2.line(canvas, (gx0, gy1), (gx1, gy1), (170, 178, 188), 1)
            cv2.line(canvas, (gx0, gy0), (gx0, gy1), (170, 178, 188), 1)

            fields_list = (fields,) if isinstance(fields, str) else fields
            colors_list = (colors,) if isinstance(colors, tuple) and len(colors) == 3 and isinstance(colors[0], int) else colors
            for series_index, field in enumerate(fields_list):
                points: list[tuple[int, int]] = []
                values: list[float] = []
                for sample_index, data_row in enumerate(good_rows):
                    try:
                        values.append(float(data_row[field]))
                    except (KeyError, TypeError, ValueError):
                        values.append(float("nan"))
                finite = [value for value in values if np.isfinite(value)]
                if not finite:
                    continue
                lower, upper = min(finite), max(finite)
                if upper - lower < 1e-6:
                    lower -= 0.5
                    upper += 0.5
                denominator = max(len(values) - 1, 1)
                color = colors_list[series_index]
                for sample_index, value in enumerate(values):
                    if not np.isfinite(value):
                        points.append((-1, -1))
                        continue
                    px = int(gx0 + sample_index * (gx1 - gx0) / denominator)
                    py = int(gy1 - (value - lower) * (gy1 - gy0) / (upper - lower))
                    points.append((px, py))
                previous_point = None
                for point in points:
                    if point[0] < 0:
                        previous_point = None
                        continue
                    if previous_point is not None:
                        cv2.line(canvas, previous_point, point, color, 2, cv2.LINE_AA)
                    previous_point = point
                cv2.putText(canvas, f"{upper:.2f}", (x0 + 4, gy0 + 8),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.32, (90, 98, 108), 1, cv2.LINE_AA)
                cv2.putText(canvas, f"{lower:.2f}", (x0 + 4, gy1),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.32, (90, 98, 108), 1, cv2.LINE_AA)

        cv2.imwrite(str(self.plot_path), canvas)


def open_display() -> tuple[int, int]:
    flags = cv2.WINDOW_NORMAL | getattr(cv2, "WINDOW_GUI_NORMAL", 0)
    cv2.namedWindow(WINDOW_NAME, flags)
    try:
        cv2.setWindowProperty(WINDOW_NAME, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    except cv2.error:
        pass
    cv2.imshow(WINDOW_NAME, np.zeros((FALLBACK_SCREEN[1], FALLBACK_SCREEN[0], 3), np.uint8))
    cv2.waitKey(100)
    try:
        _, _, width, height = cv2.getWindowImageRect(WINDOW_NAME)
        if width >= 480 and height >= 320:
            return width, height
    except cv2.error:
        pass
    return FALLBACK_SCREEN


def put_text(
    image: np.ndarray,
    text: str,
    x: int,
    y: int,
    scale: float = 0.48,
    color: tuple[int, int, int] = (225, 230, 238),
    thickness: int = 1,
) -> None:
    cv2.putText(
        image, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX,
        scale, color, thickness, cv2.LINE_AA,
    )


def valid_temperature_frame(frame: np.ndarray) -> np.ndarray:
    # Temperatures outside this broad physical range usually indicate a bad read.
    return np.isfinite(frame) & (frame > -40.0) & (frame < 300.0)


def measure_temperature_variation(
    frame: np.ndarray,
    previous_frame: np.ndarray | None,
) -> dict[str, float | str]:
    valid = valid_temperature_frame(frame)
    values = frame[valid]
    if values.size == 0:
        return {key: "" for key in (
            "temp_min_c", "temp_p05_c", "temp_median_c", "temp_p95_c",
            "temp_max_c", "temp_std_c", "mean_abs_delta_c", "p95_abs_delta_c",
            "max_abs_delta_c",
        )}

    result: dict[str, float | str] = {
        "temp_min_c": float(np.min(values)),
        "temp_p05_c": float(np.percentile(values, 5)),
        "temp_median_c": float(np.median(values)),
        "temp_p95_c": float(np.percentile(values, 95)),
        "temp_max_c": float(np.max(values)),
        "temp_std_c": float(np.std(values)),
        "mean_abs_delta_c": "",
        "p95_abs_delta_c": "",
        "max_abs_delta_c": "",
    }
    if previous_frame is not None:
        both_valid = valid & valid_temperature_frame(previous_frame)
        if both_valid.any():
            delta = np.abs(frame[both_valid] - previous_frame[both_valid])
            result.update({
                "mean_abs_delta_c": float(np.mean(delta)),
                "p95_abs_delta_c": float(np.percentile(delta, 95)),
                "max_abs_delta_c": float(np.max(delta)),
            })
    return result


def choose_temperature_range(
    frame: np.ndarray,
    previous: tuple[float, float] | None,
    auto_range: bool,
) -> tuple[float, float]:
    if not auto_range:
        return FIXED_TEMP_MIN_C, FIXED_TEMP_MAX_C

    valid = frame[valid_temperature_frame(frame)]
    if valid.size == 0:
        return previous or (FIXED_TEMP_MIN_C, FIXED_TEMP_MAX_C)

    low, high = np.percentile(valid, (3.0, 97.0))
    center = float((low + high) * 0.5)
    span = max(float(high - low), 5.0)  # Avoid amplifying small sensor noise.
    target = (center - span * 0.5, center + span * 0.5)
    if previous is None:
        return target

    # Smooth palette limits, not the sensor image, to reduce color pumping.
    alpha = 0.16
    return (
        previous[0] * (1.0 - alpha) + target[0] * alpha,
        previous[1] * (1.0 - alpha) + target[1] * alpha,
    )


def render_frame(
    frame: np.ndarray,
    screen_size: tuple[int, int],
    auto_range: bool,
    previous_range: tuple[float, float] | None,
    measured_fps: float,
    frame_duration_ms: float,
    sensor_subpage_hz: float,
    scene_label: str,
) -> tuple[np.ndarray, tuple[float, float]]:
    width, height = screen_size
    canvas = np.full((height, width, 3), (20, 23, 29), dtype=np.uint8)
    cv2.rectangle(canvas, (0, 0), (width, 42), (37, 43, 52), -1)
    put_text(canvas, "MLX90640  |  CALIBRATED THERMAL VIEW", 16, 28, 0.62, (248, 249, 250), 2)

    valid_mask = valid_temperature_frame(frame)
    if not valid_mask.any():
        put_text(canvas, "Waiting for a valid temperature frame...", 24, height // 2, 0.58)
        limits = previous_range or (FIXED_TEMP_MIN_C, FIXED_TEMP_MAX_C)
    else:
        limits = choose_temperature_range(frame, previous_range, auto_range)
        low, high = limits
        safe = np.where(valid_mask, frame, (low + high) * 0.5).astype(np.float32)

        # Enlarge temperature values before applying color. This avoids blocky
        # color boundaries while preserving the sensor's true 32x24 data grid.
        panel_x = int(width * 0.72)
        image_area_width = max(32, panel_x - 36)
        image_area_height = max(24, height - 82)
        scale = min(image_area_width / SENSOR_WIDTH, image_area_height / SENSOR_HEIGHT)
        image_width = max(SENSOR_WIDTH, int(SENSOR_WIDTH * scale))
        image_height = max(SENSOR_HEIGHT, int(SENSOR_HEIGHT * scale))
        enlarged = cv2.resize(safe, (image_width, image_height), interpolation=cv2.INTER_CUBIC)
        normalized = np.clip((enlarged - low) * (255.0 / max(high - low, 0.1)), 0, 255)
        colored = cv2.applyColorMap(normalized.astype(np.uint8), COLOR_MAP)
        image_x = 16
        image_y = 50 + max(0, (height - 82 - image_height) // 2)
        canvas[image_y:image_y + image_height, image_x:image_x + image_width] = colored

        panel_x = image_x + image_width + 24
        values = frame[valid_mask]
        minimum = float(np.min(values))
        median = float(np.median(values))
        maximum = float(np.max(values))
        lines = [
            "CALIBRATED TEMPERATURE",
            f"Scene label: {scene_label}",
            "",
            f"Frame rate: {measured_fps:.1f} fps",
            f"Read time: {frame_duration_ms:.0f} ms",
            f"Subpage target: {sensor_subpage_hz:g} Hz",
            f"Valid pixels: {int(valid_mask.sum())}/768",
            "",
            f"Min:    {minimum:.1f} C",
            f"Median: {median:.1f} C",
            f"Max:    {maximum:.1f} C",
            "",
            f"Palette: {low:.1f} to {high:.1f} C",
            f"Auto range: {'ON' if auto_range else 'OFF'}",
            "",
            "Enlarged for display",
            "Analysis stays 32 x 24",
        ]
        y = 76
        for line in lines:
            if line:
                put_text(canvas, line, panel_x, y, 0.43, (225, 230, 238))
            y += 23

        # Temperature legend.
        legend_y = min(height - 60, y + 2)
        gradient = np.linspace(255, 0, 160, dtype=np.uint8).reshape(1, 160)
        legend = cv2.applyColorMap(gradient, COLOR_MAP)
        legend = cv2.resize(legend, (160, 14), interpolation=cv2.INTER_NEAREST)
        if panel_x + legend.shape[1] < width and legend_y + legend.shape[0] < height - 24:
            canvas[legend_y:legend_y + 14, panel_x:panel_x + 160] = legend
            put_text(canvas, f"{high:.1f} C", panel_x, legend_y + 32, 0.38)
            put_text(canvas, f"{low:.1f} C", panel_x + 105, legend_y + 32, 0.38)

    put_text(canvas, "1: static  2: moving  0: unlabeled  A: auto range  Q: quit",
             16, height - 14, 0.39, (170, 180, 194))
    return canvas, limits


def main() -> int:
    print(f"Opening MLX90640 on /dev/i2c-{I2C_BUS}, address 0x{I2C_ADDRESS:02X}")
    print("This diagnostic run saves one row per frame and the full 32x24 temperature grid.")
    diagnostics = DiagnosticsRecorder(Path(__file__).resolve().parent / "diagnostics")
    print(f"Diagnostics: {diagnostics.csv_path}")
    adapter: SmbusI2CAdapter | None = None
    invalid_frames = 0
    try:
        adapter = SmbusI2CAdapter(I2C_BUS)
        sensor = MLX90640(adapter, address=I2C_ADDRESS)
        refresh_code = SENSOR_REFRESH_CODE
        sensor.refresh_rate = refresh_code
        print(
            f"Configured sensor subpage rate: "
            f"{REFRESH_RATES_HZ[sensor.refresh_rate]:g} Hz"
        )
        screen_size = open_display()
        frame_buffer = [0.0] * PIXEL_COUNT
        # Fresh reads right after a rate change can carry stale subpage data,
        # so discard a few frames before the real loop and its diagnostics.
        print(f"Discarding the first {STARTUP_DISCARD_FRAMES} frames so the sensor settles...")
        for _ in range(STARTUP_DISCARD_FRAMES):
            try:
                sensor.getFrame(frame_buffer)
            except (RuntimeError, ValueError, ArithmeticError):
                # A settle read may overrun once or trip the driver's math
                # (the same ValueError the main loop guards against).
                pass
            time.sleep(0.05)
        auto_range = True
        display_range: tuple[float, float] | None = None
        frame_times: list[float] = []
        previous_frame: np.ndarray | None = None
        frame_id = 0
        scene_label = "unlabeled"

        def record_invalid_frame(reason: str) -> None:
            # One bad read should never kill the viewer: write a diagnostic row
            # with the attempt's I2C stats, count it, and keep going.
            nonlocal invalid_frames
            invalid_frames += 1
            read_ms = (time.perf_counter() - sensor_started) * 1000.0
            transfer = adapter.frame_stats()
            diagnostics.write({
                "frame_id": frame_id,
                "timestamp": datetime.now().isoformat(timespec="milliseconds"),
                "status": "invalid_frame",
                "scene_label": scene_label,
                "refresh_code": refresh_code,
                "subpage_target_hz": REFRESH_RATES_HZ[refresh_code],
                "sensor_read_ms": read_ms,
                **transfer,
                "sensor_non_i2c_ms": max(0.0, read_ms - transfer["i2c_time_ms"]),
                "error": reason,
            })
            print(f"[Invalid frame] #{invalid_frames}: {reason}")

        while True:
            frame_id += 1
            loop_started = time.perf_counter()
            adapter.reset_frame_stats()
            sensor_started = time.perf_counter()
            try:
                sensor.getFrame(frame_buffer)  # Reads both subpages and calculates Celsius.
            except RuntimeError as exc:
                # "Too many retries" means the host could not finish reading a
                # subpage before another one became ready: lower the rate and
                # retry. At the lowest rate, or for any other RuntimeError,
                # fall through to the invalid-frame record instead of raising
                # and killing the viewer.
                if "Too many retries" in str(exc) and refresh_code > 0:
                    sensor_read_ms = (time.perf_counter() - sensor_started) * 1000.0
                    transfer = adapter.frame_stats()
                    diagnostics.write({
                        "frame_id": frame_id,
                        "timestamp": datetime.now().isoformat(timespec="milliseconds"),
                        "status": "retry_rate_downshift",
                        "scene_label": scene_label,
                        "refresh_code": refresh_code,
                        "subpage_target_hz": REFRESH_RATES_HZ[refresh_code],
                        "sensor_read_ms": sensor_read_ms,
                        **transfer,
                        "sensor_non_i2c_ms": max(0.0, sensor_read_ms - transfer["i2c_time_ms"]),
                        "error": str(exc),
                    })
                    refresh_code -= 1
                    sensor.refresh_rate = refresh_code
                    frame_times.clear()
                    display_range = None
                    print(
                        f"Frame read overran the sensor; reducing subpage rate to "
                        f"{REFRESH_RATES_HZ[refresh_code]:g} Hz and retrying."
                    )
                    time.sleep(0.05)
                    continue
                record_invalid_frame(f"{type(exc).__name__}: {exc}")
                time.sleep(0.05)
                continue
            except (ValueError, ArithmeticError) as exc:
                # A bad read can make the Adafruit driver fail mid-calculation
                # (e.g. sqrt of a negative number -> "math domain error").
                record_invalid_frame(f"{type(exc).__name__}: {exc}")
                time.sleep(0.05)
                continue
            frame = np.asarray(frame_buffer, dtype=np.float32).reshape(SENSOR_HEIGHT, SENSOR_WIDTH)
            valid_mask = valid_temperature_frame(frame)
            if int(valid_mask.sum()) < MIN_VALID_PIXELS:
                # A read that completed on the bus can still carry garbage.
                # Record it instead of letting it pollute the temperature
                # statistics and frame-rate window.
                record_invalid_frame(
                    f"only {int(valid_mask.sum())}/{PIXEL_COUNT} pixels in "
                    f"the valid temperature range"
                )
                continue
            sensor_read_ms = (time.perf_counter() - sensor_started) * 1000.0
            transfer = adapter.frame_stats()
            frame_times.append(time.monotonic())
            if len(frame_times) > 12:
                frame_times.pop(0)
            measured_fps = (
                (len(frame_times) - 1) / (frame_times[-1] - frame_times[0])
                if len(frame_times) > 1 and frame_times[-1] > frame_times[0]
                else 0.0
            )

            render_started = time.perf_counter()
            screen, display_range = render_frame(
                frame,
                screen_size,
                auto_range,
                display_range,
                measured_fps,
                sensor_read_ms,
                REFRESH_RATES_HZ[refresh_code],
                scene_label,
            )
            render_ms = (time.perf_counter() - render_started) * 1000.0
            display_started = time.perf_counter()
            try:
                cv2.imshow(WINDOW_NAME, screen)
                key = cv2.waitKey(1) & 0xFF
            except cv2.error:
                # The window was closed from the title bar; treat it like 'q'.
                print("Display window closed; exiting.")
                break
            display_ms = (time.perf_counter() - display_started) * 1000.0
            loop_ms = (time.perf_counter() - loop_started) * 1000.0
            row: dict[str, Any] = {
                "frame_id": frame_id,
                "timestamp": datetime.now().isoformat(timespec="milliseconds"),
                "status": "ok",
                "scene_label": scene_label,
                "refresh_code": refresh_code,
                "subpage_target_hz": REFRESH_RATES_HZ[refresh_code],
                "frame_rate_fps": measured_fps,
                "sensor_read_ms": sensor_read_ms,
                **transfer,
                "sensor_non_i2c_ms": max(0.0, sensor_read_ms - transfer["i2c_time_ms"]),
                "render_ms": render_ms,
                "display_ms": display_ms,
                "loop_ms": loop_ms,
            }
            row.update(measure_temperature_variation(frame, previous_frame))
            diagnostics.write(row, frame)
            previous_frame = frame.copy()

            if key in (ord("q"), 27):
                break
            if key in (ord("a"), ord("A")):
                auto_range = not auto_range
                display_range = None
            elif key == ord("1"):
                scene_label = "static"
            elif key == ord("2"):
                scene_label = "moving"
            elif key == ord("0"):
                scene_label = "unlabeled"
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        print(f"I2C error: {exc}", file=sys.stderr)
        return 1
    finally:
        if adapter is not None:
            adapter.close()
        cv2.destroyAllWindows()
        diagnostics.finish()
        print(f"Invalid frames skipped: {invalid_frames}")
        print(f"Frame data and timing CSV: {diagnostics.csv_path}")
        print(f"Variation plots: {diagnostics.plot_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
