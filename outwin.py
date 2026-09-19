"""A TUI window that collects the output of the program being debugged.

In TUI the program writes to the same terminal as gdb, and it writes there
directly: gdb never sees those bytes, so it cannot place them. A program that
prints a few dozen lines therefore scrolls the command window away, and a line
that crosses a window border leaves the screen corrupted.

This window gives the program a terminal of its own -- a pty opened here and
handed to "set inferior-tty" -- and draws what arrives on it inside a window
whose height the layout controls. The command window then holds gdb's own
output alone.

    out             program output, tracked expressions, commands
    focus out       then PageUp / PageDown walk back through the output
    out send TEXT   type a line into the running program
    out off / on    give the program's terminal back, and take it again

The pty is opened when this file is imported, not when the window appears,
and it stays open for the whole session. Two reasons, both learned the hard
way.

"set inferior-tty" is read once, when gdb launches the program, and never
again: gdb wires the child's stdin, stdout and stderr to that terminal at
exec time. Opening the window later cannot reach a process that is already
running, so the output of that run was gone before the window existed --
"out" after a dozen "next" steps showed an empty board. Capturing from the
start makes the window a view onto a buffer that is always being filled, so
it can be opened at any point in a run and still show everything printed so
far.

Closing the pty while the program holds the other end hangs it up, and the
program takes a SIGHUP. gdb destroys a window when the layout stops using it,
so "out" then "vars" used to kill the process being debugged. Nothing closes
the pty now while a program is alive.

The cost is stdin. "set inferior-tty" replaces the whole terminal, not just
the output half, so a program that reads from the keyboard no longer hears
it: those keystrokes belong to gdb. "out send TEXT" writes a line into that
terminal instead, and "out off" gives the real one back for a session spent
typing at the program rather than reading it.

A pty rather than a pipe or a file, because a pty is a terminal: the C library
keeps stdout line-buffered on it, as it does on the real one. Redirecting to a
pipe switches stdout to fully buffered, and the last partial buffer is then
lost when the program crashes -- which is exactly the output that says how far
it got.

stdout and stderr both arrive here, because "set inferior-tty" replaces the
terminal rather than one stream. glibc's own last words ("free(): invalid
pointer", "*** stack smashing detected ***") go to stderr and so land in this
window too, in their true position among the program's own lines. gdb's report
of the signal stays in the command window.

The buffer is emptied when a new process starts, so the window always shows
the current run and nothing older.
"""

import collections
import fcntl
import os
import pty
import re
import struct
import termios
import threading

import gdb

_MAX_LINES = 5000     # how far back "focus out" + PageUp can reach

# With no window on screen the output still has to reach the user somehow.
# A short burst is simply printed in the command window, the way it arrived
# there before this file existed; a long one would scroll that window away,
# which is the problem the window was written to solve, so it is announced
# in one line instead.
_ECHO_MAX = 20

_lines = collections.deque(maxlen=_MAX_LINES)
_partial = ""         # bytes since the last newline: a line still being written
_lock = threading.Lock()
_total = 0            # lines ever collected; _lines forgets, this does not
_echoed = 0           # how many of those the command window has already shown

_window = None
_master = None
_slave = None
_thread = None
_saved_tty = ""       # what "inferior-tty" held before this window took it
_note = None          # why the window is not capturing, when it is not
_winsize = None       # the size last pushed onto the pty
_pid = None           # the process the buffer belongs to

_DIM = "\033[90m"
_OFF = "\033[0m"

_TABSTOP = 8
_WRAP_INDENT = 4

# Everything the program writes is drawn inside a window, so control bytes that
# move the cursor have to go: they were what corrupted the screen in the first
# place. Newline and tab survive; "\r" does not, because a pty in its ordinary
# mode turns every "\n" the program writes into "\r\n".
_CONTROL = re.compile(r"\033\[[0-9;?]*[ -/]*[@-~]|\033[@-_]|[\x00-\x08\x0b-\x1f\x7f]")


def _sanitize(text):
    return _CONTROL.sub("", text)


def _budget(width):
    """Columns a line may fill.

    One short of the window: a line that reaches the last column makes ncurses
    wrap on its own, and the newline that follows then costs an empty row."""
    return max(1, width - 1)


def _wrap(text, width):
    """Break a long line onto continuation rows, losing nothing.

    varwin's _wrap stops after four rows and ends in an ellipsis, which is
    right for one expression among many. Here the text is the program's own
    output, where a dropped tail reads as a program that stopped early."""
    budget = _budget(width)
    if len(text) <= budget:
        return [text]
    indent = _WRAP_INDENT if _WRAP_INDENT < budget // 2 else 0
    rows = []
    while len(text) > budget:
        # Break after the last space that fits, so a number is never split
        # across two rows; a stretch with no space is broken where it ran out.
        cut = text.rfind(" ", indent, budget + 1)
        if cut <= indent:
            cut = budget
        rows.append(text[:cut].rstrip())
        text = " " * indent + text[cut:].lstrip()
    rows.append(text)
    return rows


def _clip(text, width):
    budget = _budget(width)
    return text if len(text) <= budget else text[:budget - 1] + "…"


def _reader(master):
    """Drain the pty. Runs on its own thread and touches no gdb object.

    A thread is needed rather than a drain at the prompt: the pty holds about
    19 KB (measured), and a program that fills it blocks inside write() while
    gdb is blocked waiting for that same program. Nothing here calls into gdb,
    whose Python API is not thread-safe -- the thread only reads a file
    descriptor and appends text. Drawing stays on gdb's own thread."""
    global _partial, _total
    pending = ""
    while True:
        try:
            chunk = os.read(master, 65536)
        except OSError:
            return          # the window closed the descriptor
        if not chunk:
            return
        pending += _sanitize(chunk.decode("utf-8", "replace"))
        if "\n" in pending:
            *done, pending = pending.split("\n")
            with _lock:
                _lines.extend(done)
                _total += len(done)
        _partial = pending


def _alive():
    """True while a program being debugged has a process of its own."""
    try:
        inferior = gdb.selected_inferior()
    except gdb.error:
        return False
    return bool(inferior and inferior.pid)


def _set_winsize(width, height):
    """Tell the pty how wide it is.

    A pty starts at zero columns, and a program that asks its terminal for a
    width would believe that. Give it the window it is actually drawn in."""
    global _winsize
    size = (max(1, height), max(20, width))
    if _slave is None or size == _winsize:
        return
    try:
        fcntl.ioctl(_slave, termios.TIOCSWINSZ,
                    struct.pack("HHHH", size[0], size[1], 0, 0))
    except OSError:
        return
    _winsize = size


def _open():
    """Open the pty and point the program at it.

    Every failure leaves "inferior-tty" alone, so the output falls back to the
    command window: messy, but never silently gone."""
    global _master, _slave, _thread, _saved_tty, _note, _winsize
    if _master is not None:
        return
    _note = None
    _winsize = None
    try:
        master, slave = pty.openpty()
    except OSError as err:
        _note = ("could not open a pty (%s); output goes to the "
                 "command window" % err)
        return
    try:
        saved = gdb.parameter("inferior-tty") or ""
    except gdb.error:
        saved = ""
    try:
        gdb.execute("set inferior-tty %s" % os.ttyname(slave), to_string=True)
    except gdb.error as err:
        os.close(master)
        os.close(slave)
        _note = ("set inferior-tty was refused (%s); output goes to the "
                 "command window" % err)
        return
    _master, _slave, _saved_tty = master, slave, saved
    # A window will refine this on its first render. Until then the program
    # would otherwise ask its terminal for a width and be told zero.
    _set_winsize(gdb.parameter("width") or 80, gdb.parameter("height") or 24)
    _thread = threading.Thread(target=_reader, args=(master,), daemon=True)
    _thread.start()


def _close():
    """Give the terminal back, then drop the pty.

    Closing the master is also how the reader thread is stopped: its read()
    fails and it returns.

    It also hangs up the other end, and the program holding it takes a
    SIGHUP, so a live process is left alone here. Only "out off" reaches this
    function now, and it refuses while a program is running."""
    global _master, _slave, _thread, _winsize
    if _master is None or _alive():
        return
    try:
        gdb.execute("set inferior-tty %s" % _saved_tty, to_string=True)
    except gdb.error:
        pass
    for fd in (_master, _slave):
        try:
            os.close(fd)
        except OSError:
            pass
    _master = _slave = _thread = _winsize = None


def _clear():
    global _partial, _total, _echoed
    with _lock:
        _lines.clear()
        _total = 0
    _echoed = 0
    _partial = ""
    if _window is not None:
        _window.offset = 0


def _rows(width):
    with _lock:
        lines = list(_lines)
    partial = _partial
    if partial:
        lines.append(partial)
    rows = []
    for line in lines:
        rows.extend(_wrap(line.expandtabs(_TABSTOP), width))
    return rows


def _sync_echo():
    """Mark everything collected so far as already shown.

    Called as the window opens and as it closes. The rows are on screen in
    the window, so the command window must not print them as well -- once
    while the window is up, or again the moment it goes away."""
    global _echoed
    _echoed = _total


def _drain_to_cmd():
    """Put the program's output in the command window while no window shows it.

    Without this the output of a run made outside the "out" layout would
    reach nothing the user can see: the pty collects it either way."""
    global _echoed
    with _lock:
        pending = _total - _echoed
        recent = list(_lines)[-pending:] if 0 < pending <= len(_lines) else []
    if pending <= 0:
        return
    _echoed = _total
    if recent and len(recent) <= _ECHO_MAX:
        gdb.write("".join(line + "\n" for line in recent))
    else:
        gdb.write('%s[out] %d lines of program output; "out" reads them%s\n'
                  % (_DIM, pending, _OFF))


class OutWindow:
    def __init__(self, win):
        global _window
        self.win = win
        self.win.title = "out"
        self.offset = 0      # rows held back from the bottom; 0 is the live tail
        _window = self
        _sync_echo()
        # Normally open already, from the import. This covers "out off"
        # followed by the layout, where taking the terminal back is the
        # obvious intent and no running process can be hung up by it.
        if _master is None and not _alive():
            _open()

    def close(self):
        """The layout stopped using this window, so gdb is destroying it.

        The pty stays open. Closing it here is what used to send SIGHUP to
        the program whenever the layout changed while it was running."""
        global _window
        _window = None
        _sync_echo()

    def vscroll(self, num):
        """PageUp / PageDown when this window has the focus.

        gdb passes a negative amount for backwards, which is the direction that
        raises the offset.

        Two steps, and both are needed. render() puts the new rows in gdb's
        window buffer; gdb does not paint that buffer onto the terminal when
        the drawing was not its idea, so the screen keeps the rows it had and
        shows two frames spliced together. "refresh", the command Ctrl-L runs,
        is what paints it. Calling refresh without the render() before it
        paints the buffer unchanged and nothing moves."""
        self.offset = max(0, self.offset - num)
        self.render()
        gdb.execute("refresh", to_string=True)

    def render(self):
        width = self.win.width
        height = self.win.height
        _set_winsize(width, height)
        room = max(1, height - 1)        # the last row belongs to the footer
        rows = _rows(width)
        total = len(rows)
        self.offset = min(self.offset, max(0, total - room))
        end = total - self.offset
        start = max(0, end - room)
        view = rows[start:end]
        if not view:
            view = ["%s no output yet; it collects here when the program runs%s"
                    % (_DIM, _OFF)]
        body = list(view)
        body.extend([""] * max(0, room - len(view)))
        body.append(_clip(self._footer(start, total), width))
        # One write for the whole window, with full_window set: gdb clears the
        # window first, so a row that is now shorter cannot leave the tail of
        # the row that was there before.
        self.win.write("\n".join(body), True)

    def _footer(self, start, total):
        if _note is not None:
            return "%s %s%s" % (_DIM, _note, _OFF)
        if self.offset:
            text = "%d/%d lines, PageDown for the latest" % (
                total - self.offset, total)
        elif start:
            text = "%d lines, %d more above: focus out then PageUp" % (
                total, start)
        else:
            text = "%d lines" % total
        return "%s %s%s" % (_DIM, text, _OFF)


def _current_pid():
    try:
        inferior = gdb.selected_inferior()
    except gdb.error:
        return None
    return inferior.pid if inferior is not None else None


def _on_cont(_event=None):
    """Empty the buffer when a different process starts.

    This runs before the new program has written anything -- the event carries
    the new pid and fires as execution resumes -- so clearing here cannot take
    the output it is meant to show. "continue" and "step" keep the same pid and
    so keep the buffer."""
    global _pid
    pid = _current_pid()
    if pid is not None and pid != _pid:
        _clear()
    _pid = pid


def _redraw(_event=None):
    if _window is not None:
        _window.render()
    else:
        _drain_to_cmd()


class OutLayoutCommand(gdb.Command):
    """Open the layout that shows the program's output.

Usage: out
       out send TEXT
       out off
       out on

The output window sits on top, tracked expressions below it, the command
window under both, in the same thirds as "vars". Compare "vars", which gives
that top third to the source instead.

The command window takes the focus, so the arrow keys walk the command
history. "focus out" moves the focus up, where PageUp and PageDown walk back
through what the program printed; "focus cmd" brings it back.

The output is collected from the moment gdb starts, so this layout can be
opened in the middle of a run and still shows everything printed so far.

"out send TEXT" writes one line into the program's terminal. The program
reads from the terminal this window collects, not from the keyboard, so a
program that calls scanf waits for this command rather than for a keystroke.
gdb prints no prompt while the program runs, so there are two moments to send
a line: before "run", where the pty holds it until the program reads it, or
any time after "run &", which starts the program in the background and keeps
the prompt. A program that reads more than a line or two is easier with
"out off", or with gdb's own "run < answers.txt".

"out off" hands the real terminal back, for a session spent typing at the
program rather than reading it; the output then lands in the command window,
where it used to. "out on" takes it again. Neither is allowed while a
program is running: swapping its terminal mid-run either does nothing or
hangs it up. Both take effect at the next run."""

    def __init__(self):
        super().__init__("out", gdb.COMMAND_USER)

    def invoke(self, arg, from_tty):
        words = arg.split(None, 1)
        if not words:
            gdb.execute("layout out")
            gdb.execute("focus cmd")
            return
        verb, rest = words[0], (words[1] if len(words) > 1 else "")
        if verb == "send":
            if _master is None:
                raise gdb.GdbError(
                    "out is off, so the program does not read from this "
                    "window. see: out on")
            os.write(_master, (rest + "\n").encode())
        elif verb == "on":
            if _master is not None:
                raise gdb.GdbError("out is already on. see: help out")
            if _alive():
                raise gdb.GdbError(
                    "a program is running and keeps the terminal it started "
                    "with. kill it first, or let it finish.")
            _open()
            if _note is not None:
                raise gdb.GdbError(_note)
        elif verb == "off":
            if _master is None:
                raise gdb.GdbError("out is already off. see: help out")
            if _alive():
                raise gdb.GdbError(
                    "closing this window's terminal would hang the running "
                    "program up. kill it first, or let it finish.")
            _close()
        else:
            raise gdb.GdbError(
                "out takes no argument, or send TEXT / off / on. "
                "see: help out")


gdb.register_window_type("out", OutWindow)
OutLayoutCommand()
gdb.events.cont.connect(_on_cont)
gdb.events.before_prompt.connect(_redraw)

# Before any window exists and before the first "run": see the module
# docstring. A failure here only sets _note, and the output falls back to the
# command window.
_open()
