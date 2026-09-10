from machine import Pin, I2C
import sys
import time

i2c = I2C(0, sda=Pin(0), scl=Pin(1))
ADDR = 0x23

def init_sensor():
    i2c.writeto(ADDR, b"\x01")
    time.sleep(0.1)
    i2c.writeto(ADDR, b"\x10")

init_sensor()

while True:
    try:
        data = i2c.readfrom(ADDR, 2)
        raw = (data[0] << 8) | data[1]
        lux = raw / 1.2

        # print() alone can be silently swallowed if stdout write fails;
        # write explicitly so a failure raises and gets caught below.
        sys.stdout.write("{}\n".format(lux))

    except OSError:

        # I2C glitch (e.g. noise, bad contact) - reset the sensor and keep going
        # instead of letting the exception kill the loop and go silent forever.

        try:
            init_sensor()

        except OSError:
            pass

    time.sleep(0.5)
