import serial
import re
import screen_brightness_control as sbc
import time
from collections import deque

PORT = "COM5"
BAUD = 115200
ser = serial.Serial(PORT, BAUD, timeout=1)
print("Smart adaptive brightness started...")

# ==================================================
# 🎛 TUNING
# ==================================================
MIN_BRIGHTNESS = 0
MAX_BRIGHTNESS = 100

# kaç lux'ta max brightness olsun
MAX_LUX = 800

# curve shape
# 0.5 = daha agresif parlaklaşır
# 1.0 = lineer
# 2.0 = yavaş artar
GAMMA = 0.55

# smoothing
SMOOTHING = 0.18

# kaç brightness farkında update yapılsın
MIN_CHANGE = 2

# update spam engelle
UPDATE_INTERVAL = 0.20

# ==================================================
history = deque(maxlen=8)
smoothed = None
last_output = None
last_update = 0

def lux_to_brightness(lux):

    # normalize
    normalized = min(max(lux / MAX_LUX, 0.0), 1.0)

    # gamma curve
    curved = normalized**GAMMA
    brightness = MIN_BRIGHTNESS + curved * (MAX_BRIGHTNESS - MIN_BRIGHTNESS)
    return brightness

def smooth(prev, target):
    if prev is None:
        return target

    return prev * (1 - SMOOTHING) + target * SMOOTHING

while True:
    try:
        line = ser.readline().decode(errors="ignore").strip()
        match = re.search(r"([0-9]+\.?[0-9]*)", line)

        if not match:
            continue

        lux = float(match.group(1))
        history.append(lux)
        avg_lux = sum(history) / len(history)

        # brightness hesapla
        target = lux_to_brightness(avg_lux)

        # smooth
        smoothed = smooth(smoothed, target)
        final = int(round(smoothed))
        final = max(MIN_BRIGHTNESS, min(MAX_BRIGHTNESS, final))
        now = time.time()

        # gereksiz DDC spam engelle
        should_update = last_output is None or abs(final - last_output) >= MIN_CHANGE

        if should_update and (now - last_update > UPDATE_INTERVAL):
            sbc.set_brightness(final)
            last_output = final
            last_update = now

        print(f"Lux: {avg_lux:.2f} | " f"Target: {target:.1f} | " f"Final: {final}")
        time.sleep(0.05)

    except Exception as e:
        print("Error:", e)
        time.sleep(0.5)
