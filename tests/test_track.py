"""track walk / track deep: the guards, the group key, and removal."""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gdb
import harness
import varwin

harness.start()
check = harness.check


def raises(name, fragment, fn, *args):
    """Every refusal in this file arrives through gdb.execute(), which turns a
    GdbError into a plain gdb.error. The class therefore carries no signal
    here and only the leak marker does, so the class check is waived."""
    harness.check_raises(name, fragment, fn, *args, clean=False)


class FakeWin:
    """Enough of a gdb TUI window for the row budget and the redraw."""

    def __init__(self, height):
        self.height = height
        self.width = 80
        self.title = ""

    def erase(self):
        pass

    def write(self, text):
        pass


class FakeWindow:
    def __init__(self, height):
        self.win = FakeWin(height)

    def render(self):
        pass


def reset(height=20):
    varwin._exprs.clear()
    varwin._labels.clear()
    varwin._groups.clear()
    varwin._pinned_groups.clear()
    varwin._previous.clear()
    varwin._last.clear()
    varwin._good.clear()
    varwin._window = FakeWindow(height) if height else None


def run(command):
    gdb.execute(command, to_string=True)


# ── the window has to be open ────────────────────────────────────────────
reset(height=None)
raises("closed window: walk is refused", "the vars window is not open",
       run, "track walk head 3")
raises("closed window: deep is refused", "the vars window is not open",
       run, "track deep root 3")
check("closed window: nothing was added", varwin._exprs, [])

# ── the depth argument ───────────────────────────────────────────────────
reset()
raises("no depth: usage is shown", "usage: tk walk EXPR DEPTH",
       run, "track walk head")
raises("bad depth: a word is refused", "positive integer",
       run, "track walk head two")
raises("bad depth: zero is refused", "positive integer",
       run, "track walk head 0")
raises("no depth: deep shows its own usage", "usage: tk deep EXPR DEPTH",
       run, "track deep root")

# ── the row budget ───────────────────────────────────────────────────────
reset(height=20)                       # room = 19 rows
raises("walk: too deep for the window", "depth 40 is too large",
       run, "track walk head 40")
raises("walk: the message names the real room", "room for 19 more rows",
       run, "track walk head 40")
check("walk: the refusal added nothing", varwin._exprs, [])

reset(height=4)                        # room = 3 rows, the tree needs 4
raises("deep: a branching type overruns the budget", "expands past",
       run, "track deep root 9")
check("deep: the refusal added nothing", varwin._exprs, [])

reset(height=2)                        # room = 1 row, filled by hand
run("track scalar")
raises("full window: the reason is the window, not the depth",
       "the vars window is full", run, "track walk head 3")
check("full window: the hand-added row survived", varwin._exprs, ["scalar"])

# ── the happy path ───────────────────────────────────────────────────────
reset()
run("track walk head 3")
check("walk: three rows went in",
      [varwin._labels[e] for e in varwin._exprs],
      ["head[0]", "head[1]", "head[2]"])
check("walk: every row carries the group",
      sorted({varwin._groups[e] for e in varwin._exprs}),
      ["walk head"])

run("track deep root 2")
check("both groups live side by side",
      [varwin._labels[e] for e in varwin._exprs],
      ["head[0]", "head[1]", "head[2]", "root", "root.left", "root.right"])

# ── removal ──────────────────────────────────────────────────────────────
run("untrack walk head")
check("untrack by group removes exactly that group",
      [varwin._labels[e] for e in varwin._exprs],
      ["root", "root.left", "root.right"])
check("untrack by group forgets the labels too",
      [e for e in varwin._labels if varwin._labels[e].startswith("head[")],
      [])
raises("untrack: an unknown group is named", "no group named",
       run, "untrack walk head")

# ── re-running a group replaces it ───────────────────────────────────────
reset()
run("track walk head 2")
run("track walk head 4")
check("re-running replaces rather than duplicates",
      [varwin._labels[e] for e in varwin._exprs],
      ["head[0]", "head[1]", "head[2]", "head[3]"])

# ── a type with no chain is sent to the other command ────────────────────
reset()
raises("walk on a chainless struct points at deep", 'use "tk deep"',
       run, "track walk plain 3")
raises("walk on a branching type asks for a field", "left, right",
       run, "track walk root 3")
run("track walk root 3 left")
check("walk: a named field is accepted",
      [varwin._labels[e] for e in varwin._exprs],
      ["root[0]", "root[1]", "root[2]"])

# ── -l pins the group at its addresses ───────────────────────────────────
reset()
run("track walk -l head 2")
check("walk -l: the labels are marked as pinned",
      [varwin._labels[e] for e in varwin._exprs],
      ["@head[0]", "@head[1]"])
check("walk -l: the expressions are addresses, not names",
      all("head" not in e for e in varwin._exprs),
      True)

# ── the plain commands still behave ──────────────────────────────────────
reset()
run("track scalar")
run("track walk head 2")
check("a plain expression and a group coexist",
      len(varwin._exprs), 3)
run("untrack 1")
check("untrack by number still works",
      [varwin._labels.get(e, e) for e in varwin._exprs],
      ["head[0]", "head[1]"])
run("delete track")
check("delete track clears the groups as well", varwin._groups, {})


# ── the point of the whole window: a value that moved ────────────────────
# This drives the real VarWindow.render() rather than a stand-in, because the
# folding and the "old -> new" line are the reason the rows are grouped one
# struct at a time. A fake render would have proved nothing about either.

class RecordingWin(FakeWin):
    """A window that keeps what render() wrote to it."""

    def __init__(self, height):
        FakeWin.__init__(self, height)
        self.chunks = []

    def erase(self):
        self.chunks = []

    def write(self, text):
        self.chunks.append(text)


reset(height=None)                     # clears every dict, drops the window
recorder = RecordingWin(30)
varwin.VarWindow(recorder)             # registers itself as varwin._window

run("break ready")
run("track walk head 3")
varwin._window.render()
first = "".join(recorder.chunks)
check("first stop: the head node reads 1", "item = 1" in first, True)
check("first stop: nothing is marked as moved", "->" in first, False)

run("continue")                        # the fixture sets item to 99 and 88
varwin._window.render()
second = "".join(recorder.chunks)
check("second stop: the moved member shows old -> new",
      "1" in second and "99" in second and "->" in second, True)
check("second stop: the second node moved too", "88" in second, True)
check("second stop: the untouched node is still there",
      "item = 3" in second, True)
check("second stop: the rows kept their labels",
      all(("head[%d]" % i) in second for i in range(3)), True)

reset()
raises("track walk: a bad expression is a message, not an exception",
       "No symbol", run, "track walk no_such_name_here 3")
raises("track deep: a bad expression is a message, not an exception",
       "No symbol", run, "track deep no_such_name_here 3")
check("a bad expression added nothing", varwin._exprs, [])

# ── the budget has to agree with what render() actually leaves ───────────
# render() spends a row on the "reading frame ^N" banner whenever a frame
# other than the innermost is selected. The budget once forgot that and was
# a row too generous in exactly that case.
reset(height=20)
check("budget: the innermost frame has the whole room",
      varwin._available_rows(), 19)
run("up")
check("budget: an outer frame pays for its banner",
      varwin._available_rows(), 18)
run("down")

# ── a repeat goes back where the old one stood ───────────────────────────
reset()
run("track walk head 2")
run("track scalar")
run("track walk head 3")
check("a replaced group keeps its place in the list",
      [varwin._labels.get(e, e) for e in varwin._exprs],
      ["head[0]", "head[1]", "head[2]", "scalar"])

# ── a row added by hand is not swallowed by a group ──────────────────────
reset()
run("track *(head)")                   # the very text an expansion produces
run("track walk head 2")
check("the hand-added row was not relabelled",
      varwin._labels.get("*(head)"), None)
run("untrack walk head")
check("untracking the group leaves the hand-added row alone",
      varwin._exprs, ["*(head)"])


# ── a chain that shrinks under the window ────────────────────────────────
# Once a chain row cannot be read, every row below it in that group reports
# the same break, once each. They collapse into one line. The rule is taken
# from the end backwards and applies to chain groups only, so a live row is
# never hidden and deep's breadth-first rows are left alone.

reset(height=None)
recorder = RecordingWin(40)
varwin.VarWindow(recorder)
run("track walk shrink 5")
run("track deep root 3")
varwin._window.render()
whole = "".join(recorder.chunks)
check("whole chain: all five rows are drawn",
      all(("shrink[%d]" % i) in whole for i in range(5)), True)
check("whole chain: nothing is collapsed yet",
      "does not reach" in whole, False)

run("continue")                        # the fixture cuts the list to two
varwin._window.render()
cut = "".join(recorder.chunks)
note = [line for line in cut.splitlines() if "does not reach" in line]

check("cut chain: the rows the chain still reaches stay",
      "shrink[0]" in cut and "shrink[1]" in cut, True)
check("cut chain: the dead run becomes one line", len(note), 1)
check("cut chain: the line names the range it stands for",
      "shrink[2] to shrink[4]" in note[0] if note else False, True)
# Scoped to the chain's own rows on purpose: the deep group in this window
# still holds a dead row, and that one is meant to keep showing its error.
check("cut chain: no chain row still shows a raw memory error",
      [line for line in cut.splitlines()
       if re.search(r"shrink\[\d\] +=", line)
       and "Cannot access memory" in line], [])
check("cut chain: the collapse is marked as a change",
      varwin._MARK in note[0] if note else False, True)
check("cut chain: the rows are still tracked, only undrawn",
      len([e for e in varwin._exprs if varwin._groups.get(e) == "walk shrink"]), 5)

# deep lays its rows out breadth first, so "below" is not "downstream".
check("deep is left alone: its dead row is still drawn",
      "root.left.left" in cut, True)


# ── what the collapse must refuse to do ──────────────────────────────────
# These drive _dead_tails directly with made-up readings. The rules they hold
# are about which runs qualify, and building a real program state for each
# one would test the fixture rather than the rule.

DEAD = "<Cannot access memory at address 0x0>"
chain = [e for e in varwin._exprs if varwin._groups.get(e) == "walk shrink"]

alive = {e: ("{...}", False) for e in varwin._exprs}

one_dead = dict(alive)
one_dead[chain[-1]] = (DEAD, False)
notes, undrawn = varwin._dead_tails(one_dead, live=True)
check("one dead row keeps its own line: nothing collapses", notes, {})
check("one dead row keeps its own line: nothing is hidden", undrawn, set())

two_dead = dict(alive)
two_dead[chain[-1]] = (DEAD, False)
two_dead[chain[-2]] = (DEAD, False)
notes, undrawn = varwin._dead_tails(two_dead, live=True)
check("two dead rows do collapse", len(notes), 1)

# A stale reading is last-stop news, and the row path marks it "?" rather
# than "*". The note has to say the same thing.
stale = dict(alive)
for e in chain[-3:]:
    stale[e] = (DEAD, True)
notes, _ = varwin._dead_tails(stale, live=True)
check("a stale run is not called a change",
      list(notes.values())[0][0] if notes else None, False)
check("a stale run says it is stale",
      list(notes.values())[0][1] if notes else None, True)

# "-l" rewrites every row to its own absolute address, so the rows stop being
# one chain and the collapse has nothing true to say about them.
reset()
run("track walk -l shrink 4")
pinned = {e: (DEAD, False) for e in varwin._exprs}
notes, undrawn = varwin._dead_tails(pinned, live=True)
check("a pinned group is never collapsed", notes, {})
check("a pinned group hides nothing", undrawn, set())

harness.report("track")
