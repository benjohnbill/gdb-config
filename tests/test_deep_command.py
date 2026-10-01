"""The printed "deep" command: its lines, its numbering, and its refusals."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gdb
import harness
import varwin                # noqa: F401  (registers track, and _expand)
import deep                  # noqa: F401  (registers the command)

harness.start()
check = harness.check


def raises(name, fragment, fn, *args):
    """A refusal through gdb.execute(), which turns a GdbError into a plain
    gdb.error, so only the leak marker tells the two apart."""
    harness.check_raises(name, fragment, fn, *args, clean=False)


def out(command):
    return gdb.execute(command, to_string=True)


def lines(text):
    return [line for line in text.splitlines() if line.strip()]


def label_of(line):
    """"$2 s.items[0] = {...}" -> "s.items[0]"."""
    head = line.split(" = ", 1)[0]
    return head.split(None, 1)[1]


# ── the listing ──────────────────────────────────────────────────────────

rows = lines(out("deep s 2"))

check("deep: one line per struct reached", len(rows), 4)
check("deep: the labels are the ones chase.deep produced",
      [label_of(line) for line in rows],
      ["s", "s.items[0]", "s.items[2]", "s.items[3]"])
check("deep: every line carries a history number",
      [line.split()[0] for line in rows],
      ["$1", "$2", "$3", "$4"])
check("deep: the number reads the same value back",
      out("print $2").split(" = ", 1)[1].strip(),
      out("print *s.items[0]").split(" = ", 1)[1].strip())

text = out("deep s 3")
check("deep: a vtable appears one level further out",
      "s.items[0].vtbl" in text, True)
check("deep: the NULL slot makes no line", "s.items[1]" in text, False)
check("deep: a depth of one is the expression alone",
      len(lines(out("deep s 1"))), 1)
check("deep: a pointer start is dereferenced",
      label_of(lines(out("deep s.items[0] 1"))[0]), "s.items[0]")

# ── the refusals ─────────────────────────────────────────────────────────

raises("deep: an array start names the element form",
       'deep s.items[0] DEPTH', out, "deep s.items 2")
raises("deep: an array start offers each as well",
       '"each s.items"', out, "deep s.items 2")
raises("deep: a scalar start keeps chase's own wording",
       "needs a struct or a pointer to one", out, "deep s.count 2")
raises("deep: the keyword typed twice is caught",
       "deep is a command, not an expression", out, "deep deep s")
raises("deep: the usage is shown", "usage: deep EXPR DEPTH", out, "deep s")
raises("deep: a non-numeric depth is named",
       "depth must be a positive integer", out, "deep s x")
raises("deep: a depth of zero is refused", "depth must be at least 1",
       out, "deep s 0")

# ── the same array, refused by the tracking form ─────────────────────────
#
# Driven through the command, not through _expand, because the wording is
# only worth checking where a user would meet it: "track deep" reaches the
# refusal through the window check, the argument split and the budget, and a
# direct call would prove none of that wiring.

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


varwin._exprs.clear()
varwin._labels.clear()
varwin._groups.clear()
varwin._pinned_groups.clear()
varwin._previous.clear()
varwin._last.clear()
varwin._good.clear()
varwin._window = FakeWindow(20)

raises("tk deep: the array refusal offers the element form",
       'tk deep s.items[0] 2', out, "track deep s.items 2")
raises("tk deep: the array refusal offers each",
       '"tk each s.items"', out, "track deep s.items 2")
raises("tk deep: a scalar keeps chase's own wording",
       "needs a struct or a pointer to one", out, "track deep s.count 2")
check("tk deep: a refused expansion leaves the window untouched",
      varwin._exprs, [])
run = out("track deep s 2")
check("tk deep: the struct start still fills the window",
      len(varwin._exprs), 4)

harness.report("deep command")
