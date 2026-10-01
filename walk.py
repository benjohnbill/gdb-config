"""A "walk" command that follows a chain of nodes and prints each one.

The gdb-script version this replaces hard-coded two field names, "item" and
"next", so it only worked on one week's exercise files. Nothing here knows a
field name in advance: the follow pointer is named on the command line or
found by looking at the type, and every other field is printed by whatever
name it has. So the same command works on a ListNode, on a Pintos
list_elem, and on a malloc-lab free list.

    walk EXPR           follow the one self-referential pointer field
    walk EXPR FIELD     follow FIELD

EXPR is the first node and must not contain spaces. It can be any expression
that yields a node pointer, or a node itself:

    walk cur            walk ll->head        walk *ptrhead
    walk root left      walk qn nextPtr

Auto-detection needs exactly one field that points at the node's own type.
A binary tree node has two, "left" and "right", so there the field has to be
named. The error message lists the candidates.

The type questions this asks are in chase.py, because "track walk" asks the
same ones. This command prints a chain once; that one turns a chain into rows
the vars window keeps re-reading.
"""

import os
import sys

import gdb

# gdb runs this file with "source", which does not put its directory on the
# import path. The type questions below used to live here; "track walk" needs
# the same answers, so they moved to chase.py and both callers ask it.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import chase


# A chain longer than this is garbage, not data. The cycle check below catches
# a real loop first; this only stops a walk through unrelated memory that
# happens to stay readable.
_MAX_NODES = 200

# How much of one field's value to show, so a wide field cannot push the
# pointers off the row. Values are formatted on one line, see _payload.
_VALUE_WIDTH = 40


def _payload(node, follow_field):
    """Every field except the one being followed, as "name=value" text."""
    parts = []
    for field in chase.named_fields(node.type):
        if field.name == follow_field:
            continue
        try:
            # "set print pretty on" in ~/.config/gdb/gdbinit breaks a nested struct over
            # several lines, which would split one node across four rows.
            text = node[field.name].format_string(pretty_structs=False)
        except gdb.error as err:
            text = "<%s>" % err
        if len(text) > _VALUE_WIDTH:
            text = text[:_VALUE_WIDTH - 1] + "…"
        parts.append("%s=%s" % (field.name, text))
    return "  ".join(parts)


class WalkCommand(gdb.Command):
    """Follow a chain of nodes and print each one.

Usage: walk EXPR [FIELD]

EXPR is the first node. FIELD is the pointer to follow; leave it out when the
node has exactly one field that points at its own type."""

    def __init__(self):
        super().__init__("walk", gdb.COMMAND_DATA, gdb.COMPLETE_EXPRESSION)

    def invoke(self, arg, from_tty):
        tokens = arg.split()
        if not tokens or len(tokens) > 2:
            raise gdb.GdbError("usage: walk EXPR [FIELD]")
        # "walk deep s" is another command's name in the expression slot.
        chase.reject_command_word(tokens, "walk")
        expr = tokens[0]
        requested = tokens[1] if len(tokens) == 2 else None

        try:
            pointer = gdb.parse_and_eval(expr)
        except gdb.error as err:
            # Report a bad expression the way gdb does, as an error message.
            # Letting it escape prints a Python traceback and, inside a
            # define, stops the remaining commands.
            raise gdb.GdbError(str(err))
        # The name is read before node_type() replaces the value, because a
        # typedef over a tag-less struct is the only place the name lives.
        type_name = chase.type_display_name(pointer)
        node_type, pointer = chase.node_type(pointer)
        follow_field = chase.choose_field(node_type, requested, name=type_name)

        seen = {}          # address -> index, so a loop names its own target
        index = 0
        while True:
            address = int(pointer)
            if address == 0:
                break
            if address in seen:
                print("  [%d] %#x  <- 여기서 [%d] 로 되돌아간다. 고리다."
                      % (index, address, seen[address]))
                return
            if index >= _MAX_NODES:
                print("  ... %d 개에서 멈췄다. 사슬이 아닐 수 있다."
                      % _MAX_NODES)
                return
            seen[address] = index

            try:
                node = pointer.dereference()
                payload = _payload(node, follow_field)
                nxt = node[follow_field]
                # int() is what actually reads the memory: a gdb.Value is
                # lazy, so forcing it here keeps the failure inside the try.
                next_address = int(nxt)
            except gdb.MemoryError:
                print("  [%d] %#x  <- 읽을 수 없는 주소다. 여기서 끊는다."
                      % (index, address))
                return
            except gdb.error as err:
                # Anything else that goes wrong mid-chain stops the walk with
                # a message, never with a traceback.
                print("  [%d] %#x  <- 여기서 멈춘다: %s" % (index, address, err))
                return

            print("  [%d] %#x  %s  %s=%#x"
                  % (index, address, payload, follow_field, next_address))
            pointer = nxt
            index += 1

        if index == 0:
            print("  빈 사슬이다. (%s 가 NULL)" % expr)
        else:
            print("  -> 총 %d 개, %s 를 따라갔다." % (index, follow_field))


WalkCommand()
