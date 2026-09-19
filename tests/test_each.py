"""each: the scanner, expand(), the printing command, and "track each"."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gdb
import harness

import each

harness.start()
check, raises = harness.check, harness.check_raises
labels = harness.labels


def run(command):
    return gdb.execute(command, to_string=True)


def run_raises(name, fragment, command):
    """Through gdb.execute() every refusal is a plain gdb.error, so only the
    leak marker carries signal; the class check is waived."""
    harness.check_raises(name, fragment, run, command, clean=False)


# ── the scanner needs no inferior ────────────────────────────────────────
first = each._first_token
check("scanner: a range inside an index is found, with the outer text as prefix",
      first("a[b[0..2]]"), (3, 9, "range", ("0", "2")))
check("scanner: a bound may be an expression",
      first("x[0..n-1]"), (1, 9, "range", ("0", "n-1")))
check("scanner: [] is the whole range",
      first("a[]"), (1, 3, "range", ("", "")))
check("scanner: [..] is the whole range too",
      first("a[..]"), (1, 5, "range", ("", "")))
check("scanner: an ordinary index is not a token", first("a[b]"), None)
check("scanner: a float times two is not a member token", first("1.*2"), None)
check("scanner: a plain name has no token", first("s.items"), None)
check("scanner: an unclosed bracket yields nothing", first("a[0..3"), None)
check("scanner: ->* is a member token", first("p->*"), (1, 4, "member", "->"))
check("scanner: .* is a member token", first("s.*"), (1, 3, "member", "."))
check("scanner: the leftmost token wins",
      first("a[b..c].*"), (1, 7, "range", ("b", "c")))
check("has_pattern: a format prefix does not hide the token",
      each.has_pattern("/x tri[0..5]"), True)
check("has_pattern: a plain expression has none", each.has_pattern("tri"), False)


check("operand: a dereference is wrapped", each._operand("*p"), "(*p)")
check("operand: a postfix chain is left alone",
      each._operand("s.items[0]"), "s.items[0]")
check("operand: an arrow chain is left alone",
      each._operand("p->buf"), "p->buf")
check("operand: an open index prefix is left alone", each._operand("a[b"), "a[b")

# ── expand() on the widget fixture ───────────────────────────────────────
def same(e):
    return (e, e)


check("expand: an explicit range, both ends included",
      each.expand("s.items[0..3]"),
      [same("s.items[%d]" % i) for i in range(4)])
check("expand: [..] takes the declared range",
      labels(each.expand("s.items[..]")),
      ["s.items[%d]" % i for i in range(8)])
check("expand: [] means the same as [..]",
      each.expand("s.items[]"), each.expand("s.items[..]"))
check("expand: a bare array means every element",
      each.expand("s.items"), each.expand("s.items[..]"))
check("expand: [A..] fills in the high end",
      labels(each.expand("s.items[6..]")), ["s.items[6]", "s.items[7]"])
check("expand: [..B] fills in the low end",
      labels(each.expand("s.items[..1]")), ["s.items[0]", "s.items[1]"])
check("expand: a bare struct means every member",
      each.expand("s"), [same("s.items"), same("s.count")])
check("expand: .* spells the same thing", each.expand("s.*"), each.expand("s"))
check("expand: a bare pointer to a struct means every member through ->",
      labels(each.expand("s.items[0]")),
      ["s.items[0]->vtbl", "s.items[0]->id", "s.items[0]->closed",
       "s.items[0]->label"])
check("expand: ->* spells the same thing",
      each.expand("s.items[0]->*"), each.expand("s.items[0]"))
check("expand: a NULL slot still expands, from its static type",
      len(each.expand("s.items[1]->*")), 4)
check("expand: nested tokens go outer first",
      labels(each.expand("s.items[0..1]->*")),
      ["s.items[0]->vtbl", "s.items[0]->id", "s.items[0]->closed",
       "s.items[0]->label",
       "s.items[1]->vtbl", "s.items[1]->id", "s.items[1]->closed",
       "s.items[1]->label"])
check("expand: a bound is evaluated once",
      len(each.expand("s.items[0..s.count-1]")), 4)
check("expand: a member across elements",
      labels(each.expand("s.items[0..3]->id")),
      ["s.items[%d]->id" % i for i in range(4)])
check("expand: a char array gives one entry per char",
      len(each.expand("s.items[0]->label")), 24)
check("expand: a range inside a char array",
      labels(each.expand("s.items[0]->label[0..3]")),
      ["s.items[0]->label[%d]" % i for i in range(4)])
check("expand: a dereferenced prefix is parenthesised",
      labels(each.expand("*s.items[0]"))[:2],
      ["(*s.items[0]).vtbl", "(*s.items[0]).id"])
check("expand: the default token applies at the top level only",
      labels(each.expand("s.items[0..1]")), ["s.items[0]", "s.items[1]"])

# ── what expand() refuses ────────────────────────────────────────────────
raises("refuse: [..] on a pointer asks for both bounds", "give both bounds",
       each.expand, "s.items[0][..]")
raises("refuse: a bare pointer to a scalar asks for both bounds",
       "give both bounds", each.expand, "&s.count")
raises("refuse: a bare scalar has nothing to step through",
       "nothing to step through", each.expand, "s.count")
raises("refuse: .* on a scalar", "needs a struct", each.expand, "s.count.*")
raises("refuse: [..] on a scalar", "needs an array", each.expand, "s.count[..]")
raises("refuse: a backwards range", "runs backwards",
       each.expand, "s.items[3..0]")
raises("refuse: the message shows the numbers", "[3..0]",
       each.expand, "s.items[3..0]")
raises("refuse: a struct as a bound", "not an integer",
       each.expand, "s.items[0..s]")
raises("refuse: a token with nothing in front of it", "nothing stands before",
       each.expand, "[0..3]")
raises("refuse: a misspelt name is reported as such", "No symbol",
       each.expand, "nosuch[0..3]")
raises("refuse: a misspelt member is reported as such", "no member named",
       each.expand, "s.items[0..3]->nosuch")
raises("refuse: the limit is applied while expanding", "more than 8",
       each.expand, "s.items[0..100000]", "each", 8)
raises("refuse: the message names the command", "tk each:",
       each.expand, "s.count", "tk each")

# ── the printing command ─────────────────────────────────────────────────
out = run("each s.items[0..3]->id").splitlines()
check("each: one line per entry", len(out), 4)
check("each: a line is label = value", out[0], "s.items[0]->id = 10")
check("each: a NULL slot is reported in place and the rest go on",
      out[1].startswith("s.items[1]->id = <Cannot access memory"), True)
check("each: the last entry is printed", out[3], "s.items[3]->id = 13")
check("each: a format letter is applied",
      run("each /x s.items[0..3]->id").splitlines()[0], "s.items[0]->id = 0xa")
check("each: a bare struct prints its members",
      len(run("each s").splitlines()), 2)
run_raises("each: a memory format is sent to x", "single format letter",
           "each /4xg s.items[0..1]")
run_raises("each: no pattern shows the usage", "usage: each", "each")
run_raises("each: a misspelt name is a message, not a traceback", "No symbol",
           "each nosuch")


# ── "track each": the guards, the group key, removal, sugar ──────────────
import varwin


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


def shown():
    return [varwin._labels.get(e, e) for e in varwin._exprs]


def groups():
    return sorted({g for g in varwin._groups.values()})


reset(height=None)
run_raises("tk each: a closed window is refused", "the vars window is not open",
           "track each s.items[0..3]")
check("tk each: a closed window: nothing was added", varwin._exprs, [])

reset()
run_raises("tk each: no pattern shows the usage",
           "give a pattern. usage: tk each [/FMT] PATTERN", "track each -l")

run("track each s.items[0..3]")
check("tk each: four rows went in", shown(), ["s.items[%d]" % i for i in range(4)])
check("tk each: every row carries the group", groups(), ["each s.items[0..3]"])
run("untrack each s.items[0..3]")
check("untrack each: the group is gone", varwin._exprs, [])
run_raises("untrack each: an unknown group is named", "no group named",
           "untrack each s.items[0..3]")

reset()
run("track each s.items[0..1]")
run("track s.count")
run("track each s.items[0..1]")
check("tk each: the same pattern again replaces in place",
      shown(), ["s.items[0]", "s.items[1]", "s.count"])
run("track each s.items[2..3]")
check("tk each: a different range is a second group, added beside",
      shown(), ["s.items[0]", "s.items[1]", "s.count", "s.items[2]", "s.items[3]"])
check("tk each: the groups are named by the pattern as typed",
      groups(), ["each s.items[0..1]", "each s.items[2..3]"])
run("untrack each s.items[0..1]")
check("untrack each: takes back exactly its own rows",
      shown(), ["s.count", "s.items[2]", "s.items[3]"])

reset(height=4)                        # room = 3 rows, the array has 8
run_raises("tk each: past the budget", "expands past the 3 rows",
           "track each s.items[..]")
check("tk each: the refusal added nothing", varwin._exprs, [])
run_raises("tk each: a misspelt name is reported before the budget",
           "No symbol", "track each nosuch[0..100000]")

reset(height=2)                        # room = 1 row, filled by hand
run("track s.count")
run_raises("tk each: a full window says so", "the vars window is full",
           "track each s.items[0..1]")
check("tk each: the hand-added row survived", varwin._exprs, ["s.count"])

reset()
run("track each -l s.items[0..1]")
check("tk each -l: the labels are marked as pinned",
      shown(), ["@s.items[0]", "@s.items[1]"])
check("tk each -l: the expressions are addresses, not names",
      all("s.items" not in e for e in varwin._exprs), True)

reset()
run("track each /x s.items[0..3]->id")
check("tk each /x: the format is stored on every row",
      [e.startswith("/x ") for e in varwin._exprs], [True] * 4)
check("tk each /x: the label stays bare",
      varwin._labels[varwin._exprs[0]], "s.items[0]->id")
check("tk each /x: the row reads in hex", varwin._read(varwin._exprs[0]), "0xa")
run("track each /d s.items[0..3]->id")
check("tk each /d: a new format replaces rather than doubles",
      [e.startswith("/d ") for e in varwin._exprs], [True] * 4)

reset()
run("track s.items[0..1]")
check("sugar: a pattern needs no keyword", groups(), ["each s.items[0..1]"])
run("track s.items")
check("sugar: a bare expression stays one plain row",
      varwin._groups.get("s.items"), None)
check("sugar: a bare expression is tracked as itself",
      "s.items" in varwin._exprs, True)
run("track s.items[]")
check("sugar: [] is a pattern too", "each s.items[]" in groups(), True)
check("sugar: rows another group already owns are not adopted",
      [e for e in varwin._exprs if varwin._groups.get(e) == "each s.items[]"],
      ["s.items[%d]" % i for i in range(2, 8)])
run("untrack s.items[0..1]")
check("sugar: untrack takes the pattern too", "each s.items[0..1]" in groups(), False)
run("untrack s.items[]")
check("sugar: untrack left the plain row alone", varwin._exprs, ["s.items"])
run("track -l /x s.items[2..3]")
check("sugar: -l and a format travel with the pattern",
      [varwin._labels[e] for e in varwin._exprs if e.startswith("/x *(")],
      ["@s.items[2]", "@s.items[3]"])
run("untrack /x s.items[2..3]")
check("sugar: untrack drops the format from the key",
      "each s.items[2..3]" in groups(), False)

reset()
run_raises("sugar: a misspelt pattern is a message, not an exception",
           "No symbol", "track nosuch[0..3]")
run_raises("tk each: a misspelt pattern is a message, not an exception",
           "No symbol", "track each nosuch[0..3]")
check("a bad pattern added nothing", varwin._exprs, [])

reset()
run("track each s")
check("tk each: a bare struct gives its members", shown(), ["s.items", "s.count"])
check("tk each: the group key is the pattern as typed", groups(), ["each s"])
check("info track: a label equal to its expression is not repeated",
      "   is   " in run("info track"), False)
reset()
run("track each -l s.items[0..0]")
check("info track: a pinned row still says what it stands for",
      "   is   " in run("info track"), True)

reset()
run("track each s.items[0..3]")
dead = {e: ("<Cannot access memory at address 0x0>", False) for e in varwin._exprs}
check("dead tails: an each group is never collapsed",
      varwin._dead_tails(dead, live=True), ({}, set()))

reset()
run_raises("walk: its usage is unchanged", "usage: tk walk EXPR DEPTH",
           "track walk s")

harness.report("each")
