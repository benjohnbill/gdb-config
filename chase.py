"""Turn one expression into the expressions for everything it reaches.

Two commands need the same traversal and disagree only about when to stop:

    chain(EXPR, DEPTH[, FIELD])   follow one self-referential pointer field,
                                  DEPTH counts nodes
    deep(EXPR, DEPTH)             follow every pointer that leads to a struct,
                                  DEPTH counts levels

Both return a list of (expression, label) pairs. The expression is what gdb
evaluates and the label is what a window shows, because the precise form gets
unreadable quickly: "*((*((s).items[0])).vtbl)" is the expression whose label
is "s.items[0].vtbl".

Expressions, not values, are the product. The vars window stores expressions
and re-reads them at every stop, so a chain expression keeps reporting
whatever now sits at that position. That is what makes one expansion enough:
nothing has to re-run when the program moves.

The units differ and the difference is not cosmetic. On a list one level is
one node, so the two agree. On a binary tree depth 10 is 1023 nodes, which is
why a caller that has a row budget must count what deep() returned rather
than trust the number it passed in.
"""

import re

import gdb

__all__ = ["chain", "deep", "node_type", "named_fields", "points_at",
           "self_pointer_fields", "choose_field",
           "ChaseError", "NoChainField", "AmbiguousChainField", "NotAStruct"]


class ChaseError(gdb.GdbError):
    """Anything this module refuses to do."""


class NotAStruct(ChaseError):
    """The expression is not a struct and not a pointer to one."""


class NoChainField(ChaseError):
    """No field points at the node's own type, so there is no chain here.

    The caller decides what to suggest instead: "walk" has nothing to offer,
    while "tk walk" can send the user to "tk deep". Both need the type name,
    so it travels with the error rather than only inside the message."""

    def __init__(self, message, type_name):
        super().__init__(message)
        self.type_name = type_name


class AmbiguousChainField(ChaseError):
    """Several fields point at the node's own type. One has to be named."""

    def __init__(self, message, type_name, candidates):
        super().__init__(message)
        self.type_name = type_name
        self.candidates = candidates


_IDENT = re.compile(r"^[A-Za-z_]\w*$")

_STRUCTS = (gdb.TYPE_CODE_STRUCT, gdb.TYPE_CODE_UNION)

# deep() counts levels, and on a branching type a level multiplies. A binary
# tree at depth 20 is a million nodes, so a caller that says "20" by mistake
# would wait for a million reads before being told the answer does not fit.
# Expansion therefore stops as soon as it has produced more than anyone could
# accept; the caller still sees more than its budget and still refuses.
_DEFAULT_LIMIT = 4096


def _atom(expr):
    """EXPR wrapped in parentheses unless it is already a single name.

    "head" stays "head" so the common case reads well; "*pp" becomes "(*pp)"
    so that appending "->next" cannot rebind to the wrong operand."""
    return expr if _IDENT.match(expr) else "(%s)" % expr


def _evaluate(expr, cmd):
    """EXPR's value, with a bad expression reported rather than raised.

    gdb.parse_and_eval raises gdb.error, which is not a GdbError. Letting one
    escape a command prints "Python Exception <class 'gdb.error'>" and, inside
    a define, stops every command after it. walk.py learned this before the
    traversal moved here, so the guard had to move with it."""
    try:
        return gdb.parse_and_eval(expr)
    except gdb.error as err:
        raise ChaseError("%s: %s" % (cmd, err))


def _require_depth(depth, cmd):
    if not isinstance(depth, int) or isinstance(depth, bool) or depth < 1:
        raise ChaseError("%s: depth must be at least 1" % cmd)


# ── type questions ───────────────────────────────────────────────────────

def type_display_name(value):
    """The name a person would use for this value's struct type.

    A tag-less "typedef struct { ... } Screen" leaves the struct type itself
    without a name, so printing it gives "struct {...}" and an error message
    built from it tells the reader nothing. The typedef is what carries the
    name, which is why this looks before strip_typedefs() rather than after."""
    candidate = value.type
    if candidate.strip_typedefs().code == gdb.TYPE_CODE_PTR:
        candidate = candidate.strip_typedefs().target()
    if candidate.name:
        return candidate.name
    stripped = candidate.strip_typedefs()
    return stripped.name or str(stripped)


def named_fields(struct_type):
    """The fields that have a name, in declaration order."""
    for field in struct_type.fields():
        if field.name is None or field.is_base_class:
            continue
        yield field


def points_at(field, struct_type):
    """True when this field is a pointer to struct_type itself."""
    field_type = field.type.strip_typedefs()
    if field_type.code != gdb.TYPE_CODE_PTR:
        return False
    target = field_type.target().strip_typedefs()
    if target.code == gdb.TYPE_CODE_VOID:
        return False
    return target == struct_type


def self_pointer_fields(struct_type):
    """Field names that point at struct_type itself."""
    return [f.name for f in named_fields(struct_type)
            if points_at(f, struct_type)]


def node_type(value, cmd="walk"):
    """The struct type behind a value, and a pointer to it.

    Accepts a pointer to a struct or a struct that has an address."""
    stripped = value.type.strip_typedefs()
    if stripped.code == gdb.TYPE_CODE_PTR:
        target = stripped.target().strip_typedefs()
        if target.code not in _STRUCTS:
            raise NotAStruct(
                "%s needs a pointer to a struct. this points to %s"
                % (cmd, target))
        return target, value
    if stripped.code in _STRUCTS:
        if value.address is None:
            raise NotAStruct("%s needs a struct that has an address" % cmd)
        return stripped, value.address
    raise NotAStruct(
        "%s needs a struct or a pointer to one. this is %s" % (cmd, value.type))


def choose_field(struct_type, requested, cmd="walk", name=None):
    """The field to follow: the one named, or the only one that can be.

    Callers pass name when they still hold the value, because a typedef over
    a tag-less struct knows a name that the struct type does not."""
    name = name or struct_type.name or str(struct_type)
    if requested:
        by_name = {f.name: f for f in named_fields(struct_type)}
        if requested not in by_name:
            raise ChaseError(
                "%s has no field '%s'. fields: %s"
                % (name, requested, ", ".join(by_name)))
        # Being a pointer is not enough. A field pointing at some other
        # struct, or a void *, would follow one node and then fail deep
        # inside the loop where the error is far from its cause.
        if not points_at(by_name[requested], struct_type):
            raise ChaseError(
                "'%s' does not point at %s, so %s cannot follow it. "
                "candidates: %s"
                % (requested, name, cmd,
                   ", ".join(self_pointer_fields(struct_type)) or "none"))
        return requested

    candidates = self_pointer_fields(struct_type)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise NoChainField(
            "no field of %s points at %s. name the field: %s EXPR FIELD"
            % (name, name, cmd), name)
    raise AmbiguousChainField(
        "%s has %d fields that could be followed: %s. name one: %s EXPR FIELD"
        % (name, len(candidates), ", ".join(candidates), cmd),
        name, candidates)


# ── chain: one field, counted in nodes ───────────────────────────────────

def chain(expr, depth, field=None, cmd="walk"):
    """The first DEPTH nodes of the chain that starts at EXPR.

    Stops early at a NULL pointer, at a node already visited, and at memory
    that cannot be read, so a broken list produces the part that is sound
    instead of an error."""
    _require_depth(depth, cmd)
    value = _evaluate(expr, cmd)
    struct_type, pointer = node_type(value, cmd)
    follow = choose_field(struct_type, field, cmd, type_display_name(value))

    stripped = value.type.strip_typedefs()
    if stripped.code == gdb.TYPE_CODE_PTR:
        ptr_expr = _atom(expr)
    else:
        # "&" binds looser than "->", so the parentheses here are what keeps
        # "(&x)->next" from becoming "&(x->next)" one step later.
        ptr_expr = "(&%s)" % _atom(expr)

    out = []
    seen = set()
    for index in range(depth):
        try:
            address = int(pointer)
        except gdb.error:
            break
        if address == 0 or address in seen:
            break
        seen.add(address)
        out.append(("*(%s)" % ptr_expr, "%s[%d]" % (expr, index)))
        try:
            pointer = pointer.dereference()[follow]
        except gdb.error:
            break
        ptr_expr = "%s->%s" % (ptr_expr, follow)
    return out


# ── deep: every pointer, counted in levels ───────────────────────────────

def _leads_to_struct(pointer_type):
    """True when this pointer type points at a struct or union.

    A void *, a function pointer and an int * are all skipped. Their value is
    already on the parent's own row, and none of them has hidden members that
    a row of their own would reveal."""
    if pointer_type.code != gdb.TYPE_CODE_PTR:
        return False
    return pointer_type.target().strip_typedefs().code in _STRUCTS


def _children(parent_expr, parent_label, parent_value):
    """Every struct one pointer away from this one.

    A member that is not a pointer is skipped on purpose: a nested struct, an
    int and a char array are all printed inside the parent already, so giving
    them a row of their own would only spend the window's height twice."""
    struct_type = parent_value.type.strip_typedefs()
    for field in named_fields(struct_type):
        field_type = field.type.strip_typedefs()

        if _leads_to_struct(field_type):
            member = parent_value[field.name]
            yield ("*((%s).%s)" % (parent_expr, field.name),
                   "%s.%s" % (parent_label, field.name),
                   member)

        elif field_type.code == gdb.TYPE_CODE_ARRAY:
            if not _leads_to_struct(field_type.target().strip_typedefs()):
                continue
            low, high = field_type.range()
            for i in range(low, high + 1):
                member = parent_value[field.name][i]
                yield ("*((%s).%s[%d])" % (parent_expr, field.name, i),
                       "%s.%s[%d]" % (parent_label, field.name, i),
                       member)


def deep(expr, depth, cmd="deep", limit=None):
    """EXPR and every struct reachable from it within DEPTH levels.

    DEPTH counts levels, not entries. A branching type turns a small depth
    into a large result, so a caller with a row budget must measure what came
    back, and pass that budget as limit so the walk can stop once the answer
    is certain to be "too many"."""
    _require_depth(depth, cmd)
    if limit is None:
        limit = _DEFAULT_LIMIT
    value = _evaluate(expr, cmd)
    stripped = value.type.strip_typedefs()

    if stripped.code == gdb.TYPE_CODE_PTR:
        node_type(value, cmd)           # raises if it points at a non-struct
        root_expr = "*(%s)" % _atom(expr)
        root_value = value.dereference()
    elif stripped.code in _STRUCTS:
        root_expr = expr
        root_value = value
    else:
        raise NotAStruct(
            "%s needs a struct or a pointer to one. this is %s"
            % (cmd, value.type))

    out = [(root_expr, expr)]
    seen = set()
    if root_value.address is not None:
        seen.add(int(root_value.address))

    frontier = [(root_expr, expr, root_value)]
    for _ in range(depth - 1):
        following = []
        for parent_expr, parent_label, parent_value in frontier:
            for child_expr, child_label, member in _children(
                    parent_expr, parent_label, parent_value):
                try:
                    address = int(member)
                except gdb.error:
                    continue
                if address == 0 or address in seen:
                    continue
                seen.add(address)
                try:
                    child_value = member.dereference()
                except gdb.error:
                    continue
                out.append((child_expr, child_label))
                following.append((child_expr, child_label, child_value))
                if len(out) > limit:
                    return out
        if not following:
            break
        frontier = following
    return out
