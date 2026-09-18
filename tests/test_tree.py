"""chase.deep on the chains fixture: branching, cycles, pointerless structs."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gdb
import harness
import chase

harness.start()
check, raises = harness.check, harness.check_raises


labels = harness.labels

check("deep: a pointer start is dereferenced once",
      chase.deep("root", 1),
      [("*(root)", "root")])

check("deep: both branches are followed",
      chase.deep("root", 2),
      [("*(root)", "root"),
       ("*((*(root)).left)", "root.left"),
       ("*((*(root)).right)", "root.right")])

check("deep: a NULL branch is skipped, a live one is not",
      labels(chase.deep("root", 3)),
      ["root", "root.left", "root.right", "root.left.left"])

check("deep: depth past the leaves adds nothing",
      labels(chase.deep("root", 9)),
      ["root", "root.left", "root.right", "root.left.left"])

# deep reaches what chain reaches, by a different route.
check("deep: a list is a chain with one child each",
      labels(chase.deep("head", 3)),
      ["head", "head.next", "head.next.next"])

check("deep: a back pointer is not followed twice",
      labels(chase.deep("dhead", 5)),
      ["dhead", "dhead.next"])

# Members that are not pointers stay inside their parent's own rendering.
check("deep: a struct with no pointer members is one entry",
      chase.deep("plain", 5),
      [("plain", "plain")])

raises("deep: a scalar is refused", "struct",
       chase.deep, "scalar", 3)

raises("deep: depth must be at least 1", "depth",
       chase.deep, "root", 0)

# A depth that would multiply out of control has to stop being paid for as
# soon as the answer is certainly "too many", not after it is all built.
check("deep: the limit stops the expansion early",
      len(chase.deep("root", 9, limit=1)) <= 2, True)

check("deep: a limit above the real size changes nothing",
      labels(chase.deep("root", 9, limit=100)),
      ["root", "root.left", "root.right", "root.left.left"])

raises("deep: an unknown name is reported, not raised", "No symbol",
       chase.deep, "no_such_name_here", 3)

harness.report("deep/tree")
