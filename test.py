import time
import gpiod

CHIP = "gpiochip4"
LINE = 17

chip = gpiod.Chip(CHIP)
line = chip.get_line(LINE)

line.request(
    consumer="grlrr-switch-test",
    type=gpiod.LINE_REQ_DIR_IN,
    flags=gpiod.LINE_REQ_FLAG_BIAS_PULL_UP
)

print("Reading GPIO17 for 10 seconds. Flip the latch and watch 1/0 change.")
try:
    for _ in range(40):
        print(line.get_value())
        time.sleep(0.25)
finally:
    line.release()
