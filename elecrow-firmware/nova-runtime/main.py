# Nova runtime - start the board layer, then (maybe) her autostart app.
# An exception here must end at the REPL, never in a reboot loop.
import sys

try:
    import nova
    nova.start()
except Exception as e:
    sys.print_exception(e)
