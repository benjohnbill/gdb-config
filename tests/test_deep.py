"""chase.deep on the widget fixture: arrays of pointers, NULL slots, vtables."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import harness
import chase

harness.start()
check, raises = harness.check, harness.check_raises


labels = harness.labels

check("deep: a struct start is used as written",
      chase.deep("s", 1),
      [("s", "s")])

check("deep: every live slot of the array becomes an entry",
      chase.deep("s", 2),
      [("s", "s"),
       ("*((s).items[0])", "s.items[0]"),
       ("*((s).items[2])", "s.items[2]"),
       ("*((s).items[3])", "s.items[3]")])

check("deep: the NULL slot is absent",
      "s.items[1]" in labels(chase.deep("s", 4)),
      False)

check("deep: a vtable shared by two widgets is expanded once",
      labels(chase.deep("s", 3)),
      ["s", "s.items[0]", "s.items[2]", "s.items[3]",
       "s.items[0].vtbl", "s.items[2].vtbl"])

check("deep: the char array is not expanded into elements",
      [name for name in labels(chase.deep("s", 4)) if "label" in name],
      [])

check("deep: an int member makes no entry",
      [name for name in labels(chase.deep("s", 4)) if name.endswith(".id")],
      [])

# The whole point of the exercise: the vtable pointer is on the board.
check("deep: the corruptible vtable is reachable at depth 3",
      chase.deep("s", 3)[4],
      ("*((*((s).items[0])).vtbl)", "s.items[0].vtbl"))

# A tag-less "typedef struct { ... } Screen" is what the exercise files use,
# so the refusal has to find the name on the typedef or say nothing useful.
raises("chain: the refusal names the typedef, not the anonymous struct",
       "no field of Screen", chase.chain, "s", 3)

try:
    chase.chain("s", 3)
except chase.NoChainField as err:
    check("chain: the error carries the typedef name", err.type_name, "Screen")

check("deep: a tag-less typedef still expands",
      len(chase.deep("s", 2)), 4)

harness.report("deep/widgets")
