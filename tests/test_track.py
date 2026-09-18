"""track walk / track deep: the guards, the group key, and removal."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gdb
import harness
import varwin

harness.start()
check, raises = harness.check, harness.check_raises


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
raises("no depth: usage is shown", "Usage: tk walk EXPR DEPTH",
       run, "track walk head")
raises("bad depth: a word is refused", "positive integer",
       run, "track walk head two")
raises("bad depth: zero is refused", "positive integer",
       run, "track walk head 0")
raises("no depth: deep shows its own usage", "Usage: tk deep EXPR DEPTH",
       run, "track deep root")

# ── the row budget ───────────────────────────────────────────────────────
reset(height=20)                       # room = 19 rows
raises("walk: too deep for the window", "depth 40 is too large",
       run, "track walk head 40")
raises("walk: the message names the real room", "room for 19 more rows",
       run, "track walk head 40")
check("walk: the refusal added nothing", varwin._exprs, [])

reset(height=4)                        # room = 3 rows, the tree needs 4
raises("deep: a branching type overruns the budget", "expands to",
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
raises("walk on a chainless struct points at deep", 'Use "tk deep"',
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


for name in ("_exprs",):
    getattr(varwin, name).clear()
for name in ("_labels", "_groups", "_previous", "_last", "_good"):
    getattr(varwin, name).clear()

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

harness.report("track")
