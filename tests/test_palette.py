"""The dim grey is one colour index in gdbinit, varwin.py and outwin.py.

gdb 17 gives every (colour space, index) pair its own curses slot, and the
terminal receives the slot number, not the index. `set style ... foreground 8`
and the ANSI escape that means colour 8 are two different pairs, so one of them
lands in the slot that reads as bright red. From 16 up both paths make the same
pair, so the greys must share one number of 16 or more; see the comment above
the style lines in gdbinit. Nothing here needs a terminal: it reads the three
files.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import harness

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
        return handle.read()


gdbinit = read("gdbinit")
varwin = read("varwin.py")
outwin = read("outwin.py")

# Numeric foregrounds only: "foreground magenta" is a basic colour and is fine.
styles = re.findall(r"^\s*set style [\w -]+? foreground (\d+)\s*$", gdbinit, re.M)
# The assignments, not the comments that explain them.
varwin_greys = re.findall(r'^_(?:PUNCT|DIM|OLD)\s*=\s*"\\033\[38;5;(\d+)m"', varwin, re.M)
outwin_greys = re.findall(r'^_DIM\s*=\s*"\\033\[38;5;(\d+)m"', outwin, re.M)

check = harness.check
check("gdbinit: four numeric grey styles", len(styles), 4)
check("varwin: punctuation, dim and old value are set", len(varwin_greys), 3)
check("outwin: the dim colour is set", len(outwin_greys), 1)
check("one grey number everywhere", sorted(set(styles + varwin_greys + outwin_greys)), [styles[0]])
check("the grey is 16 or more", int(styles[0]) >= 16, True)
check("varwin writes no bright ANSI colour as text",
      re.findall(r'^_\w+\s*=\s*"\\033\[9[0-7]m"', varwin, re.M), [])
check("outwin writes no bright ANSI colour as text",
      re.findall(r'^_\w+\s*=\s*"\\033\[9[0-7]m"', outwin, re.M), [])

harness.report("palette")
