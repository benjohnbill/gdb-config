"""vars / layout vars: which layout each spelling opens, and the cmd height.

The layouts themselves cannot be drawn here. gdb refuses to enable the TUI
when its output is not a terminal, and --batch never gives it one, so what
this file checks is the decision rather than the picture: the layout name
the command asks for, and the winheight it runs beside it. The picture is
checked by hand against a real terminal; the measured rows are recorded in
the table in ~/.config/gdb/gdbinit.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gdb
import harness
import varwin

check = harness.check


class Recorder:
    """A stand-in for the gdb module that writes down what was executed.

    Everything but execute() is the real module, so gdb.error and the rest
    keep working. The commands that need a terminal are recorded and then
    dropped, because running them here would only raise "Cannot enable the
    TUI" and that refusal says nothing about which layout was asked for."""

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
# A fixed terminal, so the restored height is a number this file can name.
# 50 rows: the fraction gives int(50 * 0.33) = 16.
varwin._terminal_rows = lambda: 50


def run(command):
    """Type a command, and return only what this module executed for it."""
    mark = len(recorder.calls)
    gdb.execute(command)
    return recorder.calls[mark:]


def raises(name, fragment, fn, *args):
    """Refusals arrive through gdb.execute(), which flattens the class."""
    harness.check_raises(name, fragment, fn, *args, clean=False)


# --- which layout each spelling opens ---------------------------------------

varwin._cmd_minimised = False

check("vars opens the even layout",
      run("vars")[0], "layout vars-even")
check("vars src opens the tall-source layout",
      run("vars src")[0], "layout src-vars")
check("vars full opens the full layout",
      run("vars full")[0], "layout vars-full")
check("layout vars reaches the same command",
      run("layout vars")[:2], ["vars", "layout vars-even"])
check("layout vars full keeps the rest of the line",
      run("layout vars full")[:2], ["vars full", "layout vars-full"])
check("layout vars src keeps the rest of the line",
      run("layout vars src")[:2], ["vars src", "layout src-vars"])

raises("vars refuses a name it does not have",
       'no argument, "src" or "full"', gdb.execute, "vars tall")
raises("layout vars refuses it too, with the same message",
       'no argument, "src" or "full"', gdb.execute, "layout vars tall")

# --- the command window's height --------------------------------------------

varwin._cmd_minimised = False

check("an even layout leaves gdb's own split alone",
      [c for c in run("vars") if c.startswith("winheight")], [])
check("the full layout shrinks the command window to gdb's floor",
      [c for c in run("vars full") if c.startswith("winheight")],
      ["winheight cmd 3"])
check("leaving the full layout hands the rows back",
      [c for c in run("vars") if c.startswith("winheight")],
      ["winheight cmd 16"])
check("and only once: the next even layout asks for nothing",
      [c for c in run("vars src") if c.startswith("winheight")], [])
check("the full layout twice over shrinks it twice, harmlessly",
      [c for c in run("vars full") + run("vars full")
       if c.startswith("winheight")],
      ["winheight cmd 3", "winheight cmd 3"])

# A terminal too short to hold three rows and the seven of margin keeps the
# layout weights, and must not leave the flag set: there is nothing to undo.
varwin._cmd_minimised = False
varwin._terminal_rows = lambda: 9
check("a terminal too short to shrink is left alone",
      [c for c in run("vars full") if c.startswith("winheight")], [])
check("and nothing is owed back afterwards",
      [c for c in run("vars") if c.startswith("winheight")], [])
varwin._terminal_rows = lambda: 50

# --- the focus, which is what makes the arrow keys walk the history ---------

varwin._cmd_minimised = False
check("every layout ends with the command window focused",
      [run("vars")[-2], run("vars src")[-2], run("vars full")[-2]],
      ["focus cmd"] * 3)

# --- the "... N more" note, which is where the full layout is offered ------
#
# The hint is the only place the window says what to type, so it has to name
# something that works. A 50-row terminal gives the full layout 44 content
# rows (see _full_layout_height), and every number below is read against
# that.

check("a few rows short: the full layout is offered, winheight beside it",
      varwin._more_hint(20, 14), "vars full, or winheight vars 22")
check("exactly the full layout's height: still offered",
      varwin._more_hint(44, 14), "vars full, or winheight vars 46")
check("one row past it: no command is named, because none would work",
      varwin._more_hint(45, 14), "needs 45 rows, the screen holds 44")
check("already the full height: the hint stops offering the layout",
      varwin._more_hint(80, 44), "needs 80 rows, the screen holds 44")

was = varwin._terminal_rows
varwin._terminal_rows = lambda: 0
check("an unmeasurable terminal promises nothing and falls back to winheight",
      varwin._more_hint(20, 14), "winheight vars 22")
varwin._terminal_rows = was

# The note as the window actually writes it. render() counts the rows, so a
# hint that reads well in isolation can still be given the wrong number.
# This drives the real VarWindow against a real chain, the way test_track
# does, because the arithmetic between the two is exactly what can drift.

class RecordingWin:
    """A window that keeps what render() wrote to it."""

    def __init__(self, height):
        self.height = height
        self.width = 100
        self.title = ""
        self.chunks = []

    def erase(self):
        self.chunks = []

    def write(self, text):
        self.chunks.append(text)


harness.start()
recorder = RecordingWin(44)
varwin.VarWindow(recorder)             # registers itself as varwin._window
# Tracked against the tall window, because "track walk" refuses a chain the
# window has no room for. The shrink afterwards is what a third of a small
# terminal does to the same rows.
gdb.execute("track walk head 6", to_string=True)
recorder.height = 4
varwin._window.render()
drawn = "".join(recorder.chunks)
check("a truncated window offers the full layout",
      "vars full, or winheight vars" in drawn, True)
check("and says how many rows are hidden", "more (" in drawn, True)

# The same rows in a window the size the full layout would give: the note
# has to be gone, or the hint was a promise the layout does not keep.
recorder.height = 44
varwin._window.render()
check("at the full layout's height the rows all fit",
      "more (" in "".join(recorder.chunks), False)

# --- the same offer at the other end --------------------------------------
#
# A chain too tall to add never reaches the window, so it never gets a
# "... N more" line. Without a word here the refusal only says how few rows
# are left, and the reader goes looking for rows to drop when one command
# would have given them the rows.

recorder.height = 8
raises("a chain too tall to add names the layout that would hold it",
       '"vars full" has room for', gdb.execute, "track walk head 40")
raises("and still says what this window itself has left",
       "room for 7 more rows", gdb.execute, "track walk head 40")

recorder.height = 44
raises("at the full height the refusal says only what is left",
       "room for 43 more rows", gdb.execute, "track walk head 400")
check("because there is no taller layout to name",
      varwin._taller_hint(), "")

harness.report("layout")
