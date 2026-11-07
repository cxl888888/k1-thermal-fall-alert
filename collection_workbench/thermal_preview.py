"""Live Celsius preview using the existing K1 driver, without recording files."""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--driver', type=Path,
                        default=Path.home() / 'k1-thermal' / 'show_thermal_calibrated.py')
    args = parser.parse_args()
    if sys.platform != 'linux':
        parser.error('The live sensor and physical display must be on the K1.')
    import fcntl

    lock = open('/tmp/k1_thermal_collector.lock', 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise SystemExit('热像正在被采集程序或另一预览占用；请先结束该读取。')

    adapter = None
    driver = None
    try:
        spec = importlib.util.spec_from_file_location('thermal_preview_driver', args.driver)
        driver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(driver)
        cv2, np = driver.cv2, driver.np
        adapter = driver.SmbusI2CAdapter(driver.I2C_BUS)
        sensor = driver.MLX90640(adapter, address=driver.I2C_ADDRESS)
        sensor.refresh_rate = driver.SENSOR_REFRESH_CODE
        width, height = driver.open_display()
        stopped = False

        def click(event, x, y, _flags, _param):
            nonlocal stopped
            if event == cv2.EVENT_LBUTTONUP and width - 110 <= x < width and 0 <= y < 48:
                stopped = True

        cv2.setMouseCallback(driver.WINDOW_NAME, click)
        limits = None
        times = []
        warmup = 4
        ready = False
        while not stopped:
            grid = [float('nan')] * 768
            begin = time.monotonic()
            error = None
            try:
                sensor.getFrame(grid)
                frame = np.asarray(grid, dtype=np.float32).reshape(24, 32)
                if not bool(driver.valid_temperature_frame(frame).all()):
                    raise ValueError('Incomplete temperature frame')
            except (OSError, RuntimeError, ValueError, ArithmeticError) as exc:
                error = f'{type(exc).__name__}: {exc}'

            if error is None and warmup:
                warmup -= 1
                error = f'Sensor warmup: {warmup} reads remaining'
            if error is None:
                times.append(time.monotonic())
                times = times[-12:]
                fps = (len(times) - 1) / (times[-1] - times[0]) if len(times) > 1 else 0.0
                screen, limits = driver.render_frame(
                    frame, (width, height), True, limits, fps,
                    (time.monotonic() - begin) * 1000,
                    driver.REFRESH_RATES_HZ[sensor.refresh_rate], 'preview',
                )
            else:
                screen = np.zeros((height, width, 3), np.uint8)
                driver.put_text(screen, 'LIVE PREVIEW - waiting for a valid frame', 16, 70)
                driver.put_text(screen, error[:95], 16, 105, 0.4)
                limits = None
                times.clear()
            cv2.rectangle(screen, (width - 110, 0), (width - 1, 47), (45, 45, 45), -1)
            driver.put_text(screen, 'CLOSE', width - 94, 31, 0.65)
            cv2.imshow(driver.WINDOW_NAME, screen)
            key = cv2.waitKey(1) & 0xff
            if error is None and not ready:
                print('THERMAL_PREVIEW_READY valid_pixels=768 units=celsius recording=false', flush=True)
                ready = True
            if key in (ord('q'), 27) or cv2.getWindowProperty(driver.WINDOW_NAME, cv2.WND_PROP_VISIBLE) < 1:
                break
            if error is not None:
                time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        if adapter is not None:
            adapter.close()
        if driver is not None:
            driver.cv2.destroyAllWindows()
        lock.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
