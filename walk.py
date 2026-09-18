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
"""

import gdb


# A chain longer than this is garbage, not data. The cycle check below catches
# a real loop first; this only stops a walk through unrelated memory that
# happens to stay readable.
_MAX_NODES = 200

# How much of one field's value to show, so a wide field cannot push the
# pointers off the row. Values are formatted on one line, see _payload.
_VALUE_WIDTH = 40


def _node_type(value):
    """The struct type of a node, given a pointer to one or the node itself.

    Returns (type, pointer_value). Raises GdbError if the expression is
    neither of those."""
    stripped = value.type.strip_typedefs()
    if stripped.code == gdb.TYPE_CODE_PTR:
        target = stripped.target().strip_typedefs()
        if target.code != gdb.TYPE_CODE_STRUCT:
            raise gdb.GdbError(
                "walk needs a pointer to a struct. this points to %s"
                % target)
        return target, value
    if stripped.code == gdb.TYPE_CODE_STRUCT:
        # "walk a" where a is a node, not a pointer to one. Take its address,
        # so the caller does not have to write "&a".
        if value.address is None:
            raise gdb.GdbError("walk needs a node that has an address")
        return stripped, value.address
    raise gdb.GdbError(
        "walk needs a node or a pointer to one. this is %s" % value.type)


def _named_fields(node_type):
    """The fields that have a name, in declaration order."""
    for field in node_type.fields():
        if field.name is None or field.is_base_class:
            continue
        yield field


def _points_at(field, node_type):
    """True when this field is a pointer to node_type itself."""
    field_type = field.type.strip_typedefs()
    if field_type.code != gdb.TYPE_CODE_PTR:
        return False
    target = field_type.target().strip_typedefs()
    if target.code == gdb.TYPE_CODE_VOID:
        return False
    return target == node_type


def _self_pointer_fields(node_type):
    """Field names that point at node_type itself."""
    return [f.name for f in _named_fields(node_type) if _points_at(f, node_type)]


def _choose_field(node_type, requested):
    if requested:
        by_name = {f.name: f for f in _named_fields(node_type)}
        if requested not in by_name:
            raise gdb.GdbError(
                "%s has no field '%s'. fields: %s"
                % (node_type.name or node_type, requested,
                   ", ".join(by_name)))
        # "Is it a pointer" is not enough. A field pointing at some OTHER
        # struct, or a void *, walks one node and then fails deep inside the
        # loop, where the error escapes as a Python traceback and, inside a
        # define, kills the rest of the commands.
        if not _points_at(by_name[requested], node_type):
            raise gdb.GdbError(
                "'%s' does not point at %s, so walk cannot follow it. "
                "candidates: %s"
                % (requested, node_type.name or "the node",
                   ", ".join(_self_pointer_fields(node_type)) or "none"))
        return requested

    candidates = _self_pointer_fields(node_type)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise gdb.GdbError(
            "no field of %s points at %s. name the field: walk EXPR FIELD"
            % (node_type.name or node_type, node_type.name or "the node"))
    raise gdb.GdbError(
        "%s has %d fields that could be followed: %s. "
        "name one: walk EXPR FIELD"
        % (node_type.name or node_type, len(candidates),
           ", ".join(candidates)))


def _payload(node, follow_field):
    """Every field except the one being followed, as "name=value" text."""
    parts = []
    for field in _named_fields(node.type):
        if field.name == follow_field:
            continue
        try:
            # "set print pretty on" in ~/.gdbinit breaks a nested struct over
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
        expr = tokens[0]
        requested = tokens[1] if len(tokens) == 2 else None

        try:
            pointer = gdb.parse_and_eval(expr)
        except gdb.error as err:
            # Report a bad expression the way gdb does, as an error message.
            # Letting it escape prints a Python traceback and, inside a
            # define, stops the remaining commands.
            raise gdb.GdbError(str(err))
        node_type, pointer = _node_type(pointer)
        follow_field = _choose_field(node_type, requested)

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
