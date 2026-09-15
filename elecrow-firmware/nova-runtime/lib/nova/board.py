# Elecrow AI Camera Development Board (SAD00006D, ESP32-S3R8) - the pin map.
#
# Every value comes from firmware that ran on *this* board, not from a data
# sheet: rook-os/clients/rook-elecrow/rook_elecrow/board.h, board_io.h and
# lgfx_config.h (Rook 2.0 / Companion OS), which in turn follow Elecrow's
# example/V1.0/AICamera_demo. Where Elecrow's later V1.1 code disagrees (the
# amplifier on P1_5), the V1.0 schematic and Rook's board test win: this board
# is V1.0. Do not "tidy" these; each one cost a debugging session once.

# I2C: AW9523 port expander + CST816S touch share one bus
I2C_SDA = 16
I2C_SCL = 15
I2C_HZ = 400_000
AW9523_ADDR = 0x5A          # chip id register 0x10 answers 0x23
TOUCH_ADDR = 0x15

# AW9523 pins (Adafruit numbering: 0-7 = P0_0..P0_7, 8-15 = P1_0..P1_7)
AW_RGB_G = 4                # P0_4, LED/PWM
AW_RGB_R = 5                # P0_5, LED/PWM
AW_RGB_B = 6                # P0_6, LED/PWM
AW_POWER = 8                # P1_0, panel power - must be on BEFORE the LCD init
AW_FLASH = 9                # P1_1, camera flash lamp - never write OUTPUT1=0xFF
AW_BACKLIGHT = 11           # P1_3, LCD backlight, LED/PWM
AW_MUTE = 12                # P1_4, amplifier: HIGH = muted, LOW = sound

# LCD: ST7789, 240x284, SPI
LCD_SCK = 14
LCD_MOSI = 13
LCD_DC = 9
LCD_CS = 10
LCD_W = 240
LCD_H = 284
LCD_X_OFFSET = 0
LCD_Y_OFFSET = 0
LCD_INVERT = True           # Rook: "invert and rgb_order give an image that
LCD_BGR = True              #   looks almost right when wrong" - verify colours
LCD_SPI_HZ = 40_000_000     # measured 2026-09-12: a full frame takes 111 ms at
                            #   10 MHz, 29 ms at 40 MHz, and the picture is
                            #   identical - checked against a line pattern
LCD_SPI_ID = 1              # SPI(2) hangs the chip on MicroPython 1.29 (WDT reset)
TOUCH_INT = 11
TOUCH_ROTATED = True        # offset_rotation 2: touch reads upside down

# TF card: its own SPI bus, and no chip-select line on the schematic
SD_SCK = 7
SD_MISO = 6
SD_MOSI = 8
SD_HZ = 4_000_000           # SoftSPI, measured 2026-09-11 on 400 random blocks:
                            #   10 MHz 50 % read errors, 4 MHz none (66 KB/s)

# Speaker: NS4168 over I2S (standard, not PDM)
SPK_DATA = 3
SPK_WS = 4
SPK_BCK = 5

# Microphone: PDM. Stock MicroPython's machine.I2S has no PDM mode, and the
# legacy IDF driver returns zero bytes (Rook, audio.h) - so it is NOT offered
# here until a firmware with a PDM module exists.
MIC_CLK = 1
MIC_DATA = 2

# Camera: GC2145 (OV2640 footprint). No hardware JPEG; RGB565 + CIF, OPI PSRAM.
CAM_XCLK = 39
CAM_SCL = 40
CAM_SDA = 41
CAM_DATA = (20, 18, 17, 19, 21, 47, 38, 46)   # D0..D7
CAM_VSYNC = 42
CAM_HREF = 45
CAM_PCLK = 48
CAM_XCLK_HZ = 10_000_000

# Button: GPIO0, active low. Never calibrate its idle level at boot: the
# CH340 auto-reset circuit holds it low while flashing.
BUTTON = 0
