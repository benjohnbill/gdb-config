"""Turn one pattern into the expressions for each element or member it names.

"walk" and "deep", tracked or printed, choose how far to follow pointers. This
module answers
a different question: which elements of an array, or which members of a
struct, one at a time, the way a for loop would visit them.

    each [/FMT] PATTERN             print once, one line per expansion
    track each [-l] [/FMT] PATTERN  one row per expansion in the vars window
    untrack each PATTERN            remove what "track each PATTERN" added

"e" is the alias gdbinit gives "each", and the subcommands answer to it as
well, so "tk e tri[0..5]" is "track each tri[0..5]".

A pattern is an ordinary expression with any number of these tokens in it:

    [A..B]    indices A to B, both included. A and B are expressions and are
              evaluated once, when the command runs: tri[0..rows-1]
    [..]      the whole range the array declares. [A..] and [..B] fill in the
              missing side from the declaration. [] means the same as [..]
    .*  ->*   every named member of the struct at that position
    (none)    no token at all: an array is stepped through element by
              element, a struct (or a pointer to one) member by member.
              Anything else has no parts to walk and stands for itself, so
              that "each x" shows what "print x" shows. An explicit [..] on
              a pointer is still refused: there the length was asked for.

Tokens nest and combine, outer first: s.items[0..1]->* is the members of
s.items[0], then the members of s.items[1].

expand() returns a list of (expression, label) pairs, like chase.chain() and
chase.deep(). Here the two are the same string, because "s.items[3]" is
already what a person would type: no rewriting is needed to keep the label
short. Expressions, not values, are the product, for the reason chase.py
gives: the vars window stores them and re-reads them at every stop.

The text before a token is left as written when it is a plain postfix chain
("s.items", "p->buf"), and wrapped in parentheses otherwise, so that "*p"
becomes "(*p).vtbl" rather than "*p.vtbl", which gdb would read as
"*(p.vtbl)".

Imported, never sourced. "source x.py" runs a file inside gdb's shared
__main__, where the helper names of every sourced file overwrite each other;
see the comment above "import outwin" in ~/.config/gdb/gdbinit. varwin.py
imports this module, and that import is what registers the "each" command.
"""

import re

import gdb

import chase


class EachError(gdb.GdbError):
    """Anything this module refuses to do."""


class TooMany(EachError):
    """The pattern expands to more entries than the caller can take."""


_STRUCTS = (gdb.TYPE_CODE_STRUCT, gdb.TYPE_CODE_UNION)

# A pattern such as buf[0..100000] is a mistake more often than a request. The
# expansion stops as soon as it has produced more than anyone could accept; a
# caller with a row budget passes that budget as the limit and stops sooner.
_DEFAULT_LIMIT = 4096

# The single-letter formats that Value.format_string accepts. varwin.py holds
# the same string; it is repeated here because this module must not import
# varwin (varwin imports it), and the letters are gdb's, not ours.
_VALUE_FORMATS = "xduotacfsz"

# A prefix made only of these is a postfix chain such as "s.items[0]" or
# "p->buf", which binds tighter than anything that could follow it, so it
# needs no parentheses. "->" is removed before the test because "-" and ">"
# on their own would be operators.
_POSTFIX = re.compile(r"[\w.\[\]()]*")

# What may follow ".*" or "->*" for it to be a token: the end of the text or
# the start of the next postfix step. "1.*2" is a float literal times two.
_AFTER_MEMBER = ".-[])"


# ── the scanner ──────────────────────────────────────────────────────────

def _matching_bracket(text, start):
    """The index of the "]" that closes the "[" at start, or -1."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _split_range(inner):
    """("A", "B") when inner is "A..B" at its own top level, else None."""
    depth = 0
    for k in range(len(inner) - 1):
        char = inner[k]
        if char in "[(":
            depth += 1
        elif char in "])":
            depth -= 1
        elif depth == 0 and inner.startswith("..", k):
            return inner[:k].strip(), inner[k + 2:].strip()
    return None


def _ends_step(text, i):
    return i == len(text) or text[i] in _AFTER_MEMBER


def _first_token(text):
    """The leftmost token in text as (start, end, kind, payload), or None.

    kind is "range" with payload (A, B) as written ("" for an omitted side),
    or "member" with payload "." or "->". A token inside an index is found
    too, so a[b[0..2]] yields the inner one with "a[b" in front of it. That
    prefix is not an expression on its own, which is why an omitted bound
    there cannot be filled in (see _bounds)."""
    i = 0
    while i < len(text):
        if text[i] == "[":
            j = _matching_bracket(text, i)
            if j < 0:
                return None
            inner = text[i + 1:j]
            if not inner.strip():
                return i, j + 1, "range", ("", "")
            bounds = _split_range(inner)
            if bounds is not None:
                return i, j + 1, "range", bounds
        elif text.startswith("->*", i) and _ends_step(text, i + 3):
            return i, i + 3, "member", "->"
        elif text.startswith(".*", i) and _ends_step(text, i + 2):
            return i, i + 2, "member", "."
        i += 1
    return None


def has_pattern(text):
    """True when text holds a token, so "track" can route it to "each"."""
    return _first_token(text) is not None



# ── the type questions ───────────────────────────────────────────────────

def _operand(prefix):
    """prefix as the left operand of "[", "." or "->"."""
    if _POSTFIX.fullmatch(prefix.replace("->", "")):
        return prefix
    return "(%s)" % prefix


def _type_of(expr, cmd):
    return chase.evaluate(expr, cmd).type.strip_typedefs()


def _unknown_length(prefix, declared, cmd):
    return EachError(
        "%s: %s is a pointer (%s), so its length is not known. "
        "give both bounds: %s[0..N]" % (cmd, prefix, declared, prefix))


def _bound(text, cmd):
    """The integer an explicit bound evaluates to."""
    value = chase.evaluate(text, cmd)
    try:
        return int(value)
    except gdb.error:
        raise EachError("%s: the bound %s is not an integer." % (cmd, text))


def _bounds(prefix, a_text, b_text, cmd):
    """(low, high) for prefix[A..B], both ends included."""
    low = high = None
    if not a_text or not b_text:
        declared = _type_of(prefix, cmd)
        if declared.code == gdb.TYPE_CODE_ARRAY:
            low, high = declared.range()
        elif declared.code == gdb.TYPE_CODE_PTR:
            raise _unknown_length(prefix, declared, cmd)
        else:
            raise EachError(
                "%s: [..] needs an array, and %s is %s."
                % (cmd, prefix, declared))
    a = _bound(a_text, cmd) if a_text else low
    b = _bound(b_text, cmd) if b_text else high
    if b < a:
        raise EachError(
            "%s: [%s..%s] is [%d..%d], which runs backwards. "
            "give the low bound first." % (cmd, a_text, b_text, a, b))
    return a, b


def _struct_behind(prefix, token, cmd):
    """The struct type that prefix.* or prefix->* steps through.

    Read from the static type, never from memory, so a NULL pointer still
    expands: its rows then report the failed read, which is a finding."""
    declared = _type_of(prefix, cmd)
    behind = declared
    if behind.code == gdb.TYPE_CODE_PTR:
        behind = behind.target().strip_typedefs()
    if behind.code not in _STRUCTS:
        raise EachError(
            "%s: %s needs a struct or a pointer to one, and %s is %s."
            % (cmd, token, prefix, declared))
    return behind


def token_for_type(declared):
    """The token a value of this type is stepped through with.

    "" when the type has no parts to step through. Such a value is one entry,
    itself, rather than a refusal: "each" is meant to be the only word needed
    to look at a value, and refusing the scalars would send the user back to
    "print" for half of them. A pointer to a non-struct lands here too. Its
    length is unknown, but so is its owner's intent, and the honest reading
    of a bare pointer is the pointer, exactly as "print" reads it. Asking for
    the length explicitly, with [..], is still refused by _bounds."""
    if declared.code == gdb.TYPE_CODE_ARRAY:
        return "[..]"
    if declared.code in _STRUCTS:
        return ".*"
    if (declared.code == gdb.TYPE_CODE_PTR
            and declared.target().strip_typedefs().code in _STRUCTS):
        return "->*"
    return ""


def _default_token(pattern, cmd):
    """The token a pattern without one is given, chosen by its type."""
    return token_for_type(_type_of(pattern, cmd))


# ── the expansion ────────────────────────────────────────────────────────

def _expand_into(text, cmd, limit, out):
    token = _first_token(text)
    if token is None:
        if not out:
            # Checked once, on the first leaf, so a misspelt name or member
            # is reported as such, and before any count has grown.
            chase.evaluate(text, cmd)
        out.append((text, text))
        if len(out) > limit:
            raise TooMany(cmd)
        return
    start, end, kind, payload = token
    prefix, rest = text[:start].strip(), text[end:]
    if not prefix:
        raise EachError(
            "%s: nothing stands before %s in %s. "
            "put the expression to index in front of it."
            % (cmd, text[start:end], text))
    prefix = _operand(prefix)
    if kind == "range":
        low, high = _bounds(prefix, payload[0], payload[1], cmd)
        for n in range(low, high + 1):
            _expand_into("%s[%d]%s" % (prefix, n, rest), cmd, limit, out)
    else:
        for field in chase.named_fields(_struct_behind(prefix, payload, cmd)):
            _expand_into("%s%s%s%s" % (prefix, payload, field.name, rest),
                         cmd, limit, out)


def expand(pattern, cmd="each", limit=None):
    """The (expression, label) pairs a pattern stands for, outer index first.

    A pattern with no token is given one by its type, at the top level only:
    s.items[0..3] is four elements, not four elements' worth of members.
    Raises TooMany once more than limit entries have been produced."""
    if limit is None:
        limit = _DEFAULT_LIMIT
    pattern = pattern.strip()
    if _first_token(pattern) is None:
        pattern += _default_token(pattern, cmd)
    out = []
    try:
        _expand_into(pattern, cmd, limit, out)
    except TooMany:
        raise TooMany(
            "%s: %s expands to more than %d entries. narrow the range."
            % (cmd, pattern, limit))
    return out


# ── the printing command ─────────────────────────────────────────────────

def print_entry(value, label, options):
    """One line, numbered the way gdb numbers "print", and kept in history.

    The number comes first, as "$1 = ..." does, so a line can be read back
    with "$N" and used in the next expression. The label sits between the
    number and the value; it is left out when it would only repeat the text
    the user typed.

    Public because "deep" prints the same kind of list from a different
    traversal, and one numbering habit has to serve both."""
    text = value.format_string(**options)
    number = gdb.add_history(value)
    return "$%d%s = %s" % (number, " " + label if label else "", text)


class EachCommand(gdb.Command):
    """Print each element or member a pattern names, one per line.

Usage: each [/FMT] PATTERN
       e [/FMT] PATTERN

    each tri[0..5]              tri[0] to tri[5], one line each
    each s.items[..]            every slot the array declares ([] works too)
    each s.items[0..3]->id      one member across several elements
    each s                      every member of a struct, or of *pointer
    each n                      a value with no parts: what "print n" shows
    each /x tri[0..rows-1]      one format letter, as "print /x" takes it

Every line is numbered as "print" numbers its own, so "$3" reads one back.
A value with nothing to step through is printed as it stands, which makes
"each" a "print" that also expands; "e" is the short name for it.

"tk e PATTERN" puts the same lines in the vars window and keeps them
current; see "help track". A memory format such as /4xg belongs to "x".
Tab completion works up to the first token."""

    def __init__(self):
        super().__init__("each", gdb.COMMAND_DATA, gdb.COMPLETE_EXPRESSION)

    def invoke(self, arg, from_tty):
        arg = arg.strip()
        fmt = None
        if arg.startswith("/"):
            head, _, arg = arg.partition(" ")
            fmt, arg = head[1:], arg.strip()
            if len(fmt) != 1 or fmt not in _VALUE_FORMATS:
                raise gdb.GdbError(
                    'each: /%s is not a single format letter. each takes one '
                    'of "%s"; a memory format like /4xg belongs to x.'
                    % (fmt, _VALUE_FORMATS))
        if not arg:
            raise gdb.GdbError("usage: each [/FMT] PATTERN")
        # "each deep s 3" is the keyword from "tk deep" carried over to a
        # command that takes no keyword. Caught here so the reader hears
        # that, rather than gdb's report of a symbol named "deep".
        chase.reject_command_word(arg.split(), "each")
        # "set print pretty on" would break a struct element over several
        # lines and split one entry across rows, as walk.py already found.
        options = {"pretty_structs": False}
        if fmt:
            options["format"] = fmt
        # A value with no parts is its own answer, and it is evaluated here,
        # once, instead of going through expand(). expand() would ask for its
        # type and then the caller would read it again, which "each i++" must
        # not do: it has to move the counter exactly as far as "print i++"
        # moves it. An array or struct takes the second read, but reading the
        # name of one has no effect to repeat.
        if _first_token(arg) is None:
            value = chase.evaluate(arg, "each")
            if not token_for_type(value.type.strip_typedefs()):
                print(print_entry(value, None, options))
                return
        for expression, label in expand(arg, "each"):
            try:
                line = print_entry(gdb.parse_and_eval(expression), label,
                                  options)
            except gdb.error as err:
                # A NULL slot or freed memory is one bad entry, not a reason
                # to stop the others; the reason goes where the value would.
                # There is no value to keep, so the entry gets no number.
                line = "%s = <%s>" % (label, err)
            print(line)


EachCommand()
