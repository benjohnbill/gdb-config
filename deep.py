"""A "deep" command that prints one expression and everything it reaches.

    deep EXPR DEPTH     EXPR and every struct within DEPTH levels of it

This is what "tk deep EXPR DEPTH" puts in the vars window, printed once
instead. The traversal is chase.deep(), shared with that command, so the two
agree about what a level is: one pointer hop, which a branching type
multiplies. On a list depth 5 is five nodes; on a binary tree it is 31.

Every line is numbered as "print" numbers its own, so "$3" reads one back.
That numbering is each.py's, and this file borrows the helper rather than
growing a second one. walk.py prints a different kind of row, because a
chain has a follow pointer to show at the end of each line and this has none.

Imported, never sourced. "source x.py" runs a file inside gdb's shared
__main__, where the helper names of every sourced file overwrite each other;
see the comment above "import outwin" in ~/.config/gdb/gdbinit. This file
imports each.py, so it has to stay out of there.
"""

import gdb

import chase
import each


# A depth that fills the screen was typed by mistake, not asked. The listing
# stops here and says so, as walk.py stops at 200 nodes. "tk deep" takes the
# window's height for a budget; printing has no such limit of its own, which
# is why this number exists.
_MAX_ENTRIES = 200


def _no_start(err, expr):
    """The refusal to word when EXPR cannot be a starting point.

    chase reports the type and nothing more, on purpose. Only here is it
    known that "deep" starts at one struct and that "each" is the command
    for a run of them."""
    if not err.elements_are_structs:
        return err
    return gdb.GdbError(
        "deep: %s is %s. deep starts at one struct. "
        'try "deep %s[0] DEPTH", or "each %s" for a line per element.'
        % (expr, err.declared, expr, expr))


class DeepCommand(gdb.Command):
    """Print EXPR and every struct it reaches, DEPTH levels out.

Usage: deep EXPR DEPTH

    deep s 1        s on its own
    deep s 2        s, and every struct one pointer away from it
    deep root 4     four levels of a tree: the root and its descendants

DEPTH counts levels, not entries, so a branching type grows fast. A pointer
that leads to no struct (a void *, a function pointer, an int *) gets no line
of its own: its value already sits inside its owner's. A struct reached twice
is expanded once, and a NULL slot is skipped.

Every line is numbered as "print" numbers its own, so "$3" reads one back.
"tk deep EXPR DEPTH" puts the same list in the vars window and keeps it
current; see "help track". For one line per element or member, see
"help each"."""

    def __init__(self):
        super().__init__("deep", gdb.COMMAND_DATA, gdb.COMPLETE_EXPRESSION)

    def invoke(self, arg, from_tty):
        tokens = arg.split()
        if len(tokens) != 2:
            raise gdb.GdbError("usage: deep EXPR DEPTH")
        # "deep deep s" and "deep each s" are the keyword typed twice, which
        # gdb would answer with a missing symbol named after the command.
        chase.reject_command_word(tokens, "deep")
        expr, depth_text = tokens
        try:
            depth = int(depth_text)
        except ValueError:
            raise gdb.GdbError(
                "deep: depth must be a positive integer, not %r. "
                "usage: deep EXPR DEPTH" % depth_text)

        try:
            entries = chase.deep(expr, depth, cmd="deep", limit=_MAX_ENTRIES)
        except chase.NotAStruct as err:
            raise _no_start(err, expr)

        # "set print pretty on" would break a struct over several lines and
        # split one entry across them, as walk.py already found.
        options = {"pretty_structs": False}
        for expression, label in entries[:_MAX_ENTRIES]:
            try:
                line = each.print_entry(gdb.parse_and_eval(expression), label,
                                        options)
            except gdb.error as err:
                # A NULL slot or freed memory is one bad entry, not a reason
                # to stop the others; the reason goes where the value would.
                # There is no value to keep, so the entry gets no number.
                line = "%s = <%s>" % (label, err)
            print(line)
        if len(entries) > _MAX_ENTRIES:
            print("  ... stopped at %d entries. try a smaller depth."
                  % _MAX_ENTRIES)


DeepCommand()
