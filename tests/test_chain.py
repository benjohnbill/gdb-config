"""chase.chain: follow one self-referential pointer field."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gdb
import harness
import chase

harness.start()
check, raises = harness.check, harness.check_raises


# A plain pointer start: the expression is parenthesis-free and the label
# carries the position, exactly as the printed "walk" numbers its rows.
check("chain: three of five",
      chase.chain("head", 3),
      [("*(head)", "head[0]"),
       ("*(head->next)", "head[1]"),
       ("*(head->next->next)", "head[2]")])

check("chain: stops at NULL before the depth runs out",
      [label for _, label in chase.chain("head", 10)],
      ["head[%d]" % i for i in range(5)])

check("chain: stops when the chain loops back",
      [label for _, label in chase.chain("loop", 10)],
      ["loop[0]", "loop[1]", "loop[2]"])

check("chain: depth 1 gives the head alone",
      chase.chain("head", 1),
      [("*(head)", "head[0]")])

# A struct start, not a pointer to one.
check("chain: a node value is taken by address",
      chase.chain("*head", 2),
      [("*((&(*head)))", "*head[0]"),
       ("*((&(*head))->next)", "*head[1]")])

# Field choice.
raises("chain: two candidates are refused", "next, prev",
       chase.chain, "dhead", 5)

check("chain: a named field is followed",
      [label for _, label in chase.chain("dhead", 5, "next")],
      ["dhead[0]", "dhead[1]"])

raises("chain: a field that leads elsewhere is refused", "does not point at",
       chase.chain, "head", 5, "item")

raises("chain: an unknown field is named", "has no field",
       chase.chain, "head", 5, "nope")

# Types that have no chain at all.
raises("chain: a struct with no self pointer is refused", "no field",
       chase.chain, "plain", 5)

raises("chain: a scalar is refused", "struct or a pointer",
       chase.chain, "scalar", 5)

# gdb.parse_and_eval raises gdb.error, which is not a GdbError: letting one
# out prints "Python Exception" instead of a message a user can act on.
raises("chain: an unknown name is reported, not raised", "No symbol",
       chase.chain, "no_such_name_here", 3)

try:
    chase.chain("no_such_name_here", 3)
except gdb.GdbError as err:
    harness.check("chain: the refusal is a GdbError",
                  isinstance(err, gdb.GdbError), True)

harness.report("chain")
