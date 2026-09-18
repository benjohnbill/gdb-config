"""The printed "walk" command: it must survive the move to the shared engine."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gdb
import harness
import walk                 # noqa: F401  (registers the command)

harness.start()
check = harness.check


def raises(name, fragment, fn, *args):
    """These refusals arrive through gdb.execute(), which turns a GdbError
    into a plain gdb.error, so only the leak marker distinguishes them."""
    harness.check_raises(name, fragment, fn, *args, clean=False)


def out(command):
    return gdb.execute(command, to_string=True)


def rows(text):
    return [line for line in text.splitlines() if line.lstrip().startswith("[")]


text = out("walk head")
check("walk: one row per node", len(rows(text)), 5)
check("walk: the payload field is named", "item=1" in text, True)
check("walk: the follow field is reported", "next 를 따라갔다" in text, True)
check("walk: the count is reported", "총 5 개" in text, True)

text = out("walk loop")
check("walk: a cycle is called out", "고리다" in text, True)
check("walk: the cycle names the row it returns to", "[1] 로" in text, True)

text = out("walk root left")
check("walk: a named field is followed", len(rows(text)), 3)

text = out("walk dhead next")
check("walk: the doubly linked list walks forward", len(rows(text)), 2)

raises("walk: two candidates are refused", "next, prev", out, "walk dhead")
raises("walk: a chainless struct is refused", "no field of Plain", out, "walk plain")
raises("walk: a scalar is refused", "struct or a pointer", out, "walk scalar")
raises("walk: the usage is shown", "usage: walk", out, "walk")

harness.report("walk command")
