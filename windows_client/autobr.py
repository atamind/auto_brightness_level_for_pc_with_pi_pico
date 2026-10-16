import ctypes
import json
import logging
import math
import re
import serial
import screen_brightness_control as sbc
import sys
import threading
import time
from collections import deque
from pathlib import Path

import pystray
from PIL import Image, ImageDraw
from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QIcon, QImage, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QRadioButton,
    QSlider,
    QVBoxLayout,
)


PORT = "COM5"
BAUD = 115200
RECONNECT_DELAY = 2
MIN_BRIGHTNESS = 0
MAX_BRIGHTNESS = 100
MAX_LUX = 800
GAMMA = 0.55
SMOOTHING = 0.18
MIN_CHANGE = 2
UPDATE_INTERVAL = 0.20
MIN_CUSTOM_BASE_BRIGHTNESS = 1.0
MAX_MULTIPLIER = 10000
WM_LBUTTONDBLCLK = 0x0203

APP_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
SETTINGS_PATH = APP_DIR / "autobr_settings.json"
LOG_PATH = APP_DIR / "autobr.log"


def multiplier_for_target(desired, base_brightness):
    return min(
        desired / max(base_brightness, MIN_CUSTOM_BASE_BRIGHTNESS),
        MAX_MULTIPLIER,
    )


class Preferences:
    def __init__(self, settings_path):
        self.settings_path = settings_path
        self.lock = threading.Lock()
        self.mode = "default"
        self.multiplier = 1.0
        self.desired_brightness = 50
        self.reference_brightness = None
        self.pending_calibration = False
        self.current_base_brightness = None

    def load(self):
        if not self.settings_path.exists():
            return

        with self.settings_path.open("r", encoding="utf-8") as settings_file:
            data = json.load(settings_file)

        if not isinstance(data, dict):
            raise ValueError("Ayar dosyasının biçimi geçersiz.")

        mode = data.get("mode", "default")
        multiplier = data.get("multiplier", 1.0)
        desired = data.get("desired_brightness", 50)
        reference = data.get("reference_brightness")
        pending = data.get("pending_calibration", False)

        if mode not in ("default", "custom"):
            raise ValueError("Ayar dosyasındaki mod geçersiz.")
        if isinstance(multiplier, bool) or not isinstance(multiplier, (int, float)):
            raise ValueError("Ayar dosyasındaki çarpan geçersiz.")
        if not math.isfinite(multiplier) or not 0 <= multiplier <= MAX_MULTIPLIER:
            raise ValueError("Ayar dosyasındaki çarpan geçersiz.")
        if isinstance(desired, bool) or not isinstance(desired, int) or not 0 <= desired <= 100:
            raise ValueError("Ayar dosyasındaki parlaklık değeri geçersiz.")
        if reference is not None and (
            isinstance(reference, bool)
            or not isinstance(reference, (int, float))
            or not math.isfinite(reference)
            or reference < 0
        ):
            raise ValueError("Ayar dosyasındaki referans değeri geçersiz.")
        if not isinstance(pending, bool):
            raise ValueError("Ayar dosyasındaki kalibrasyon işareti geçersiz.")

        with self.lock:
            self.mode = mode
            self.multiplier = float(multiplier)
            self.desired_brightness = desired
            self.reference_brightness = reference
            self.pending_calibration = pending

    def save(self):
        temporary_path = self.settings_path.with_suffix(".json.tmp")
        with self.lock:
            data = {
                "mode": self.mode,
                "multiplier": self.multiplier,
                "desired_brightness": self.desired_brightness,
                "reference_brightness": self.reference_brightness,
                "pending_calibration": self.pending_calibration,
            }
            with temporary_path.open("w", encoding="utf-8") as settings_file:
                json.dump(data, settings_file, ensure_ascii=False, indent=2)
                settings_file.write("\n")
            temporary_path.replace(self.settings_path)

    def set_mode(self, mode):
        with self.lock:
            if self.mode == mode:
                return False
            self.mode = mode
            if mode == "custom" and self.reference_brightness is None:
                reference = self.current_base_brightness
                if reference is None:
                    self.pending_calibration = True
                else:
                    self.multiplier = multiplier_for_target(
                        self.desired_brightness, reference
                    )
                    self.reference_brightness = reference
                    self.pending_calibration = False
        self.save()
        return True

    def set_desired_brightness(self, desired):
        with self.lock:
            if desired == self.desired_brightness:
                return False
            self.desired_brightness = desired
            reference = self.current_base_brightness
            if reference is None:
                reference = self.reference_brightness
            if reference is None:
                self.pending_calibration = True
            else:
                self.multiplier = multiplier_for_target(desired, reference)
                self.reference_brightness = reference
                self.pending_calibration = False
        self.save()
        return True

    def apply_base_brightness(self, base_brightness):
        with self.lock:
            self.current_base_brightness = base_brightness
            if not self.pending_calibration:
                return False
            self.multiplier = multiplier_for_target(
                self.desired_brightness, base_brightness
            )
            self.reference_brightness = base_brightness
            self.pending_calibration = False
        self.save()
        return True

    def output_brightness(self, base_brightness):
        with self.lock:
            is_custom = self.mode == "custom"
            multiplier = self.multiplier if is_custom else 1.0
        if is_custom:
            # Allow a nonzero custom target when the default curve returns 0%.
            base_brightness = max(base_brightness, MIN_CUSTOM_BASE_BRIGHTNESS)
        return max(MIN_BRIGHTNESS, min(MAX_BRIGHTNESS, base_brightness * multiplier))

    def snapshot(self):
        with self.lock:
            return self.mode, self.desired_brightness, self.multiplier

    def settings_brightness(self):
        with self.lock:
            brightness = self.current_base_brightness
            if brightness is None:
                brightness = self.reference_brightness
            if brightness is None:
                brightness = self.desired_brightness

            if self.mode == "custom":
                brightness = max(brightness, MIN_CUSTOM_BASE_BRIGHTNESS)
                brightness *= self.multiplier
            return int(round(max(MIN_BRIGHTNESS, min(MAX_BRIGHTNESS, brightness))))


class SettingsTrayIcon(pystray.Icon):
    def __init__(self, *args, on_double_click, **kwargs):
        self._on_double_click_callback = on_double_click
        self._last_left_click = None
        super().__init__(*args, **kwargs)

    def _on_notify(self, wparam, lparam):
        if lparam == WM_LBUTTONDBLCLK:
            self._last_left_click = None
            self._on_double_click_callback()
            return

        if lparam == pystray._win32.win32.WM_LBUTTONUP:
            now = time.monotonic()
            double_click_interval = (
                ctypes.windll.user32.GetDoubleClickTime() / 1000
            )
            if (
                self._last_left_click is not None
                and now - self._last_left_click <= double_click_interval
            ):
                self._last_left_click = None
                self._on_double_click_callback()
            else:
                self._last_left_click = now
            return

        super()._on_notify(wparam, lparam)


class UiBridge(QObject):
    open_settings = pyqtSignal()
    quit_requested = pyqtSignal()
    brightness_updated = pyqtSignal()
    apply_brightness = pyqtSignal()
    error = pyqtSignal(str, str)


class SettingsDialog(QDialog):
    DEFAULT_MODE_DESCRIPTION = (
        "Otomatik parlaklık eğrisi kullanılıyor. "
        "Özel bir değer seçmek için Özel moduna geçin."
    )
    CUSTOM_MODE_DESCRIPTION = (
        "Seçtiğiniz parlaklık seviyesi ortam ışığı değiştikçe korunur."
    )

    def __init__(self, preferences, bridge):
        super().__init__()
        self.preferences = preferences
        self.bridge = bridge
        self.setWindowTitle("Auto Brightness Ayarları")
        self.setFixedWidth(380)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(18)

        title = QLabel("Parlaklık ayarları")
        title.setObjectName("title")
        layout.addWidget(title)

        description = QLabel(
            "\"Varsayılan\" seçeneği ortam ışığına göre ekran parlaklığını "
            "uygulamanın varsayılan ayarlarıyla otomatik düzenler. \"Özel\" "
            "seçeneğinde ise mevcut ortam için istediğiniz parlaklığı seçerek "
            "otomatik ayarı kişiselleştirebilirsiniz."
        )
        description.setObjectName("description")
        description.setWordWrap(True)
        description.setAlignment(Qt.AlignmentFlag.AlignJustify)
        layout.addWidget(description)

        modes = QHBoxLayout()
        modes.setSpacing(12)
        self.mode_group = QButtonGroup(self)
        self.default_radio = QRadioButton("Varsayılan")
        self.custom_radio = QRadioButton("Özel")
        self.mode_group.addButton(self.default_radio)
        self.mode_group.addButton(self.custom_radio)
        modes.addWidget(self.default_radio)
        modes.addWidget(self.custom_radio)
        modes.addStretch()
        layout.addLayout(modes)

        self.brightness_heading = QLabel("İstenen parlaklık")
        self.brightness_heading.setObjectName("sectionHeading")
        layout.addWidget(self.brightness_heading)

        slider_row = QHBoxLayout()
        slider_row.setSpacing(12)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(MIN_BRIGHTNESS, MAX_BRIGHTNESS)
        self.slider.setSingleStep(1)
        self.slider.setPageStep(10)
        self.value_label = QLabel()
        self.value_label.setObjectName("value")
        self.value_label.setMinimumWidth(58)
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        slider_row.addWidget(self.slider, 1)
        slider_row.addWidget(self.value_label)
        layout.addLayout(slider_row)

        self.mode_description = QLabel()
        self.mode_description.setObjectName("description")
        self.mode_description.setWordWrap(True)
        self.mode_description.setAlignment(Qt.AlignmentFlag.AlignJustify)
        layout.addWidget(self.mode_description)

        description_width = (
            self.width()
            - layout.contentsMargins().left()
            - layout.contentsMargins().right()
        )
        description_heights = []
        for text in (
            self.DEFAULT_MODE_DESCRIPTION,
            self.CUSTOM_MODE_DESCRIPTION,
        ):
            self.mode_description.setText(text)
            description_heights.append(
                self.mode_description.heightForWidth(description_width)
            )
        self.mode_description.setFixedHeight(max(description_heights))

        self.default_radio.clicked.connect(lambda: self._set_mode("default"))
        self.custom_radio.clicked.connect(lambda: self._set_mode("custom"))
        self.slider.valueChanged.connect(self._set_brightness)
        self.bridge.brightness_updated.connect(self.refresh)
        self.bridge.error.connect(self._show_error)
        self.refresh()
        self.setFixedHeight(self.sizeHint().height())

    def refresh(self):
        mode, _, _ = self.preferences.snapshot()
        value = self.preferences.settings_brightness()
        self.default_radio.setChecked(mode == "default")
        self.custom_radio.setChecked(mode == "custom")
        self.slider.blockSignals(True)
        self.slider.setValue(value)
        self.slider.blockSignals(False)
        self.slider.setEnabled(mode == "custom")
        self.value_label.setText(f"{value}%")
        if mode == "default":
            self.mode_description.setText(self.DEFAULT_MODE_DESCRIPTION)
        else:
            self.mode_description.setText(self.CUSTOM_MODE_DESCRIPTION)

    def _set_mode(self, mode):
        try:
            changed = self.preferences.set_mode(mode)
        except OSError as error:
            logging.exception("Could not save settings")
            self.bridge.error.emit(
                "Ayarlar kaydedilemedi",
                f"Ayarlar şu konuma kaydedilemedi:\n{SETTINGS_PATH}\n\n{error}",
            )
            changed = True
        if changed:
            self.bridge.apply_brightness.emit()
        self.refresh()

    def _set_brightness(self, value):
        if self.preferences.snapshot()[0] != "custom":
            self.refresh()
            return
        try:
            changed = self.preferences.set_desired_brightness(value)
        except OSError as error:
            logging.exception("Could not save settings")
            self.bridge.error.emit(
                "Ayarlar kaydedilemedi",
                f"Ayarlar şu konuma kaydedilemedi:\n{SETTINGS_PATH}\n\n{error}",
            )
            changed = True
        if changed:
            self.bridge.apply_brightness.emit()
        self.refresh()

    def _show_error(self, title, message):
        QMessageBox.critical(self, title, message)

    def closeEvent(self, event):
        event.ignore()
        self.hide()


def lux_to_brightness(lux):
    normalized = min(max(lux / MAX_LUX, 0.0), 1.0)
    return MIN_BRIGHTNESS + normalized**GAMMA * (
        MAX_BRIGHTNESS - MIN_BRIGHTNESS
    )


def smooth(previous, target):
    if previous is None:
        return target
    return previous * (1 - SMOOTHING) + target * SMOOTHING


def apply_requested_brightness(requested, base_brightness, preferences, setter):
    if base_brightness is None or not requested.is_set():
        return None

    requested.clear()
    final = int(round(preferences.output_brightness(base_brightness)))
    setter(final)
    return final


def make_tray_image():
    image = Image.new("RGB", (64, 64), (35, 45, 60))
    draw = ImageDraw.Draw(image)
    draw.ellipse((20, 20, 44, 44), fill=(255, 210, 70))
    for start, end in (
        ((32, 5), (32, 15)),
        ((32, 49), (32, 59)),
        ((5, 32), (15, 32)),
        ((49, 32), (59, 32)),
        ((13, 13), (20, 20)),
        ((44, 44), (51, 51)),
        ((13, 51), (20, 44)),
        ((44, 20), (51, 13)),
    ):
        draw.line((start, end), fill=(255, 235, 160), width=3)
    return image


def make_window_icon():
    image = make_tray_image().convert("RGBA")
    qt_image = QImage(
        image.tobytes("raw", "RGBA"),
        image.width,
        image.height,
        QImage.Format.Format_RGBA8888,
    ).copy()
    return QIcon(QPixmap.fromImage(qt_image))


def main():
    logging.basicConfig(
        filename=str(LOG_PATH),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    preferences = Preferences(SETTINGS_PATH)
    settings_error = None
    try:
        preferences.load()
    except (OSError, json.JSONDecodeError, OverflowError, ValueError) as error:
        settings_error = str(error)
        logging.exception("Could not load settings")

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")
    window_icon = make_window_icon()
    app.setWindowIcon(window_icon)
    app.setStyleSheet(
        """
        QWidget {
            background-color: #111827;
            color: #e5e7eb;
            font-family: "Segoe UI";
            font-size: 10pt;
        }
        QDialog {
            background-color: #111827;
        }
        QLabel#title {
            color: #f9fafb;
            font-size: 19pt;
            font-weight: 600;
        }
        QLabel#description {
            color: #9ca3af;
        }
        QLabel#sectionHeading {
            color: #d1d5db;
            font-weight: 600;
        }
        QLabel#value {
            color: #93c5fd;
            font-size: 15pt;
            font-weight: 600;
        }
        QRadioButton {
            spacing: 8px;
            padding: 8px 12px;
            border: 1px solid #374151;
            border-radius: 8px;
            background: #1f2937;
        }
        QRadioButton:checked {
            border-color: #3b82f6;
            background: #1e3a5f;
        }
        QRadioButton::indicator {
            width: 16px;
            height: 16px;
        }
        QSlider::groove:horizontal {
            height: 6px;
            border-radius: 3px;
            background: #374151;
        }
        QSlider {
            background-color: transparent;
            border: none;
        }
        QSlider::sub-page:horizontal {
            border-radius: 3px;
            background: #3b82f6;
        }
        QSlider::handle:horizontal {
            width: 18px;
            margin: -7px 0;
            border: 2px solid #ffffff;
            border-radius: 9px;
            background: #3b82f6;
        }
        QSlider:disabled::sub-page:horizontal {
            background: #4b5563;
        }
        QSlider:disabled::handle:horizontal {
            background: #6b7280;
        }
        """
    )
    running = threading.Event()
    running.set()
    bridge = UiBridge()
    settings_window = SettingsDialog(preferences, bridge)
    settings_window.setWindowIcon(window_icon)
    brightness_update_requested = threading.Event()
    bridge.apply_brightness.connect(brightness_update_requested.set)

    def open_settings():
        settings_window.refresh()
        settings_window.show()
        settings_window.raise_()
        settings_window.activateWindow()

    bridge.open_settings.connect(open_settings)
    bridge.quit_requested.connect(app.quit)

    icon = SettingsTrayIcon(
        "autobr",
        make_tray_image(),
        "Auto Brightness",
        on_double_click=bridge.open_settings.emit,
        menu=pystray.Menu(
            pystray.MenuItem("Ayarlar", lambda icon, item: bridge.open_settings.emit()),
            pystray.MenuItem("Çıkış", lambda icon, item: bridge.quit_requested.emit()),
        ),
    )

    if settings_error:
        QMessageBox.warning(
            None,
            "Auto Brightness",
            "Ayar dosyası okunamadı, varsayılan modda başlanıyor. "
            f"Ayrıntılar:\n{settings_error}",
        )

    def sensor_loop():
        history = deque(maxlen=8)
        smoothed = None
        last_output = None
        last_update = 0

        while running.is_set():
            try:
                with serial.Serial(PORT, BAUD, timeout=0.05) as connection:
                    logging.info("%s connected", PORT)
                    while running.is_set():
                        line = connection.readline().decode(errors="ignore").strip()
                        match = re.search(r"([0-9]+\.?[0-9]*)", line)
                        if match:
                            lux = float(match.group(1))
                            history.append(lux)
                            average_lux = sum(history) / len(history)
                            base_target = lux_to_brightness(average_lux)
                            smoothed = smooth(smoothed, base_target)
                            preferences.apply_base_brightness(smoothed)
                            bridge.brightness_updated.emit()

                        forced_output = apply_requested_brightness(
                            brightness_update_requested,
                            smoothed,
                            preferences,
                            sbc.set_brightness,
                        )
                        if forced_output is not None:
                            last_output = forced_output
                            last_update = time.monotonic()
                            continue

                        if not match:
                            continue

                        final = int(round(preferences.output_brightness(smoothed)))
                        now = time.monotonic()
                        should_update = (
                            last_output is None
                            or abs(final - last_output) >= MIN_CHANGE
                        )
                        if should_update and now - last_update > UPDATE_INTERVAL:
                            sbc.set_brightness(final)
                            last_output = final
                            last_update = now

                        logging.debug(
                            "Lux: %.2f | Target: %.1f | Final: %d",
                            average_lux,
                            base_target,
                            final,
                        )
            except serial.SerialException:
                if running.is_set():
                    logging.exception("Serial connection failed; reconnecting")
                    time.sleep(RECONNECT_DELAY)
            except (OSError, ValueError):
                if running.is_set():
                    logging.exception("Brightness update failed")
                    bridge.error.emit(
                        "Auto Brightness",
                        "Parlaklık veya ayar dosyası işlemi başarısız oldu. "
                        f"Ayrıntılar için {LOG_PATH} dosyasına bakın.",
                    )
                    time.sleep(0.5)
            except Exception:
                if running.is_set():
                    logging.exception("Unexpected brightness client error")
                    bridge.error.emit(
                        "Auto Brightness",
                        "Beklenmeyen bir hata oluştu. "
                        f"Ayrıntılar için {LOG_PATH} dosyasına bakın.",
                    )
                    time.sleep(0.5)

    sensor_thread = threading.Thread(target=sensor_loop, daemon=True)
    sensor_thread.start()
    icon.run_detached()
    try:
        return app.exec()
    finally:
        running.clear()
        icon.stop()


if __name__ == "__main__":
    main()
