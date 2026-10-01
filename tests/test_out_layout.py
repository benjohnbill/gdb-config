"""out: the command window gets its rows back after "vars full".

Like test_layout.py this checks the decision, not the picture: gdb will not
draw a TUI without a terminal, so the layout and winheight commands are
recorded and dropped, and the test reads what "out" asked for.

The bug it pins: "vars full" shrinks the command window to three rows and
leaves a flag in varwin for "vars" and "vars src" to read. "out" never read
it, so "vars full" then "out" kept the three rows (measured at 40 rows: out
18, vars 19, cmd 3, against 13, 14, 13 for a fresh "out").
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import __main__
import gdb
import harness
import outwin
import varwin

check = harness.check


class Recorder:
    """A stand-in for the gdb module that writes down what was executed.

    The same one as test_layout.py's: everything but execute() is the real
    module, and the commands that need a terminal are recorded and dropped."""

    _DROPPED = ("layout ", "focus ", "winheight ")

    def __init__(self, module):
        self._module = module
        self.calls = []

    def __getattr__(self, name):
        return getattr(self._module, name)

    def execute(self, command, **kwargs):
        self.calls.append(command)
        if command.startswith(self._DROPPED):
            return ""
        return self._module.execute(command, **kwargs)


recorder = Recorder(gdb)
varwin.gdb = recorder
outwin.gdb = recorder
# 50 rows: the fraction gives int(50 * 0.33) = 16, as in test_layout.py.
varwin._terminal_rows = lambda: 50
# In a session varwin.py is sourced, which puts its names in gdb's shared
# __main__, and that is where outwin looks. Importing it as a module here
# leaves them out of __main__, so put the one name outwin needs back.
__main__._size_cmd_window = varwin._size_cmd_window


def run(command):
    """Type a command, and return only what the two modules executed for it."""
    mark = len(recorder.calls)
    gdb.execute(command)
    return recorder.calls[mark:]


def heights(calls):
    return [c for c in calls if c.startswith("winheight")]


# --- a fresh "out" is gdb's own split, untouched ---------------------------

varwin._cmd_minimised = False
check("a fresh out opens the layout and focuses the command window",
      run("out"), ["layout out", "focus cmd"])

# --- "vars full" then "out" hands the three rows back -----------------------

check("vars full shrinks the command window to gdb's floor",
      heights(run("vars full")), ["winheight cmd 3"])
check("out then hands the rows back, between the layout and the focus",
      run("out"), ["layout out", "winheight cmd 16", "focus cmd"])
check("and only once: the next out asks for nothing",
      heights(run("out")), [])
check("and the flag is cleared, so vars src owes nothing either",
      heights(run("vars src")), [])

# --- the other order, and the two commands sharing one flag -----------------

check("out then vars full still shrinks it",
      heights(run("out")) + heights(run("vars full")), ["winheight cmd 3"])
check("vars gives the rows back as before",
      heights(run("vars")), ["winheight cmd 16"])
check("and out has nothing left to give after that",
      heights(run("out")), [])

# --- a verb is not a layout, so it leaves the rows where they are -----------

run("vars full")
check("out send does not hand the rows back",
      heights(run("out send hello")), [])
check("and the flag is still set for the next layout",
      varwin._cmd_minimised, True)
check("so out still gives them back afterwards",
      heights(run("out")), ["winheight cmd 16"])

# --- varwin absent: out must still open its layout --------------------------

del __main__._size_cmd_window
varwin._cmd_minimised = True
check("without varwin, out opens the layout and nothing else",
      run("out"), ["layout out", "focus cmd"])
__main__._size_cmd_window = varwin._size_cmd_window

# --- a terminal too short to divide: nothing to hand back -------------------

varwin._cmd_minimised = False
varwin._terminal_rows = lambda: 9
check("a terminal too short to shrink leaves nothing owed to out",
      heights(run("vars full")) + heights(run("out")), [])

harness.report("out layout")
