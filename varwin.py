"""A TUI window that shows tracked expressions, redrawn in place.

"display" appends its output to the command window at every stop, so a few
expressions and a few steps bury the screen in scrollback. This window keeps
one row per expression and overwrites it, so the rows never move and a value
that changed is easy to find.

Commands follow gdb's own shape, where "display" is the closest relative:

    track EXPR      add an expression          (compare: display EXPR)
    track -l EXPR   add it at its address      (compare: watch -location)
    info track      list what is tracked       (compare: info display)
    untrack N       remove expression N        (compare: undisplay N)
    delete track    remove every expression    (compare: delete display)
    vars            switch to the layout that shows the window

A whole structure goes in with one command, and comes back out with one:

    track walk EXPR DEPTH [FIELD]   the first DEPTH nodes of a chain
    track deep EXPR DEPTH           EXPR and what it reaches, DEPTH levels
    untrack walk EXPR               remove what "track walk EXPR" added
    untrack deep EXPR               remove what "track deep EXPR" added

Both add one row per struct rather than one per member, because a struct that
did not move folds to "= {...}" while the one that did opens up and shows
which member moved. A row per member would spend the window's height on the
quiet ones and hide the answer. Both refuse to run when the window is closed,
since that is where the rows go and where their budget comes from. "-l" works
here too: "track walk -l EXPR DEPTH" pins the nodes at the addresses they hold
right now, which is what you want when a node is about to leave the list.

A row whose value moved since the previous stop carries "*" in the first
column and shows "old -> new". A multi-line value (a struct under "set print
pretty on") is compared member by member, and a memory row such as
"track /4xg p" is compared word by word, so only the part that moved carries
the arrow. A line too long for the window wraps onto the next row instead of
being cut off. When the rows do not all fit, unchanged struct bodies fold
before any changed line is dropped.

Two readings are honest about what the window can see. In a frame other than
the innermost the rows show the values as of the last stop, without arrows,
because the expressions cannot be evaluated where the program is standing.
A row added with "-l" is rewritten to the address its expression referred to
at that moment, so it keeps reporting the same object from any frame.
"""

import os
import sys

import gdb

# gdb runs this file with "source", which does not put its directory on the
# import path. chase.py holds the traversal that "track walk" and the "walk"
# command both need, so make it importable before asking for it.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import chase

_exprs = []      # the expressions, in the order they were added
_labels = {}     # expression -> what the window calls it
_groups = {}     # expression -> the "walk EXPR" whose expansion put it here
_previous = {}   # values as of the stop before last
_last = {}       # values as of the most recent stop
_good = {}       # the last value that was readable at all
_window = None
_pid = None      # the inferior the snapshots belong to


# A single-letter format goes through Value.format_string, which leaves the
# value history alone. Anything with a repeat count or a size letter is a
# memory examine, so it has to go through the "x" command.
_VALUE_FORMATS = "xduotacfsz"

# Unit letters, as "x" uses them.
_SIZES = {"b": 1, "h": 2, "w": 4, "g": 8}


def _split_format(text):
    """Split "/4xg cur1" into ("4xg", "cur1"). No slash means no format."""
    if text.startswith("/"):
        head, _, rest = text.partition(" ")
        return head[1:], rest.strip()
    return None, text


def _dump_format(fmt):
    """(count, format letter, unit size) for a plain memory dump, else None.

    Only a format with an explicit unit letter qualifies, which keeps "/s"
    and "/i" out: those are text, not words, and only the "x" command knows
    how to render them."""
    if not fmt:
        return None
    digits = ""
    i = 0
    while i < len(fmt) and fmt[i].isdigit():
        digits += fmt[i]
        i += 1
    letter = size = None
    for char in fmt[i:]:
        if char in _SIZES:
            size = _SIZES[char]
        elif char in "xduot":
            letter = char
        else:
            return None
    if letter is None or size is None:
        return None
    return int(digits) if digits else 1, letter, size


def _target_address(expr):
    """The address a dump of EXPR should start at.

    A pointer or an integer is the address itself; anything else is dumped
    where it lives."""
    value = gdb.parse_and_eval(expr)
    code = value.type.strip_typedefs().code
    if code in (gdb.TYPE_CODE_PTR, gdb.TYPE_CODE_INT):
        return int(value)
    if value.address is None:
        raise gdb.error("%s has no address to dump" % expr)
    return int(value.address)


def _format_word(number, letter, size):
    if letter == "x":
        return "0x%0*x" % (size * 2, number)
    if letter == "o":
        return "0%o" % number
    if letter == "t":
        return "%0*d" % (size * 8, int(bin(number)[2:]))
    return str(number)


def _read_dump(expr, count, letter, size):
    """Words read straight from the inferior, space separated.

    The "x" command would do this too, but it also moves gdb's own
    last-examined cursor and overwrites $_ and $__ at every stop, so a bare
    "x" afterwards continues where this window looked rather than where the
    user did."""
    address = _target_address(expr)
    raw = bytes(gdb.selected_inferior().read_memory(address, count * size))
    words = []
    for k in range(count):
        chunk = raw[k * size:(k + 1) * size]
        number = int.from_bytes(chunk, "little", signed=(letter == "d"))
        words.append(_format_word(number, letter, size))
    return " ".join(words)


def _read(stored):
    fmt, expr = _split_format(stored)
    try:
        if not fmt:
            return str(gdb.parse_and_eval(expr))
        dump = _dump_format(fmt)
        if dump:
            return _read_dump(expr, *dump)
        if len(fmt) == 1 and fmt in _VALUE_FORMATS:
            return gdb.parse_and_eval(expr).format_string(format=fmt)
        # "/s" and "/i" only. Keep each line of the output as it came, so the
        # spacing inside a string or an instruction survives.
        out = gdb.execute("x/%s %s" % (fmt, expr), to_string=True)
        lines = []
        for line in out.strip().splitlines():
            lines.append(line.split(":", 1)[1].strip() if ":" in line else line)
        return "\n".join(lines)
    except gdb.error as err:
        return "<%s>" % _short_error(err)


def _short_error(err):
    """gdb's own words, on one line.

    Why a row cannot be read is often the finding itself: "Cannot access
    memory at address 0x..." says the target is unmapped, which the old
    blanket "<not in scope>" hid."""
    text = " ".join(str(err).split()).rstrip(".")
    if len(text) > 58:
        text = text[:57] + "…"
    return text


def _is_error(text):
    return text.startswith("<") and text.endswith(">")


# gdb says this when the name is not visible from where the program stands:
# another frame, or a block the program has left. The object itself may be
# perfectly alive, so this is not a change in the value and must not be drawn
# as one. "Cannot access memory at address ..." is the opposite case and stays
# a finding.
_INVISIBLE = ("No symbol", "No frame selected", "No registers")


def _is_invisible(text):
    return _is_error(text) and text[1:].startswith(_INVISIBLE)


def _read_stable(expr):
    """(value, stale). When the name is merely out of sight, the last value
    that could be read is returned and marked stale, so the row keeps saying
    what the object was instead of claiming it changed into an error."""
    value = _read(expr)
    if _is_invisible(value):
        keep = _good.get(expr)
        if keep is not None:
            return keep, True
        return value, False
    _good[expr] = value
    return value, False


# --- 색 ---
# gdb TUI 는 ncurses 로 그린다. xterm-256color 터미널에서는 24비트(38;2;R;G;B)가
# 근사 매핑되며 엉뚱한 색이 되므로, 터미널이 테마 값으로 그려 주는 ANSI 16색
# 인덱스만 쓴다. Zed 테마 XY-Zed Orchid Light 기준:
#   39 기본 전경 #564454 · 90 bright_black #745d71 · 35 magenta #974787
_OFF   = "\033[0m"
_NAME  = "\033[39m"   # 표현식 이름  — 기본 전경
_VALUE = "\033[39m"   # 값
_PUNCT = "\033[90m"   # = 기호
_DIM   = "\033[90m"   # 번호 · 안내문
_MARK  = "\033[35m"   # 바뀐 값      — 강조
_OLD   = "\033[90m"   # 이전 값

_ELLIPSIS = "…"
_WRAP_INDENT = 4
_WRAP_MAX = 4          # physical rows one logical line may occupy


def _plain(text):
    """The text without its ANSI escapes."""
    out = []
    i = 0
    while i < len(text):
        if text[i] == "\033":
            j = text.find("m", i)
            if j == -1:
                break
            i = j + 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _vlen(text):
    """Visible width: ANSI escapes take no columns."""
    return len(_plain(text))


def _budget(width):
    """Columns a line may fill.

    One short of the window: a line that reaches the last column makes
    ncurses wrap on its own, and the newline that follows then costs a
    second, empty screen row."""
    return max(1, width - 1)


def _wrap(text, width):
    """Break a line that does not fit onto continuation rows.

    The colour in force at the break carries over, so a value split across
    rows keeps one colour. The last row is cut with an ellipsis rather than
    letting one value take the whole window."""
    budget = _budget(width)
    if _vlen(text) <= budget:
        return [text + _OFF]
    lines = []
    out = []
    seen = 0
    colour = ""
    space_at = None      # index in out just after the last space
    space_seen = 0       # visible columns used up to that point
    i = 0
    while i < len(text):
        if text[i] == "\033":
            j = text.find("m", i)
            if j == -1:
                break
            code = text[i:j + 1]
            out.append(code)
            colour = "" if code == _OFF else code
            i = j + 1
            continue
        if seen >= budget:
            if len(lines) + 1 >= _WRAP_MAX:
                out.append(_DIM)
                out.append(_ELLIPSIS)
                break
            # Break at the last space rather than mid-token, so an address or
            # a word is never split across two rows. A line with no space
            # near its end is broken where it ran out of room.
            carry = ""
            if space_at is not None and seen - space_seen <= budget // 2:
                carry = "".join(out[space_at:])
                del out[space_at:]
            lines.append("".join(out) + _OFF)
            out = [" " * _WRAP_INDENT, colour, carry]
            seen = _WRAP_INDENT + _vlen(carry)
            space_at = None
            space_seen = 0
            continue
        out.append(text[i])
        seen += 1
        if text[i] == " ":
            space_at = len(out)
            space_seen = seen
        i += 1
    lines.append("".join(out) + _OFF)
    return lines


def _clip(text, width):
    """Cut to the window width, ending in an ellipsis when anything was lost."""
    budget = _budget(width)
    if _vlen(text) <= budget:
        return text + _OFF
    out = []
    seen = 0
    i = 0
    while i < len(text) and seen < budget - 1:
        if text[i] == "\033":
            j = text.find("m", i)
            if j == -1:
                break
            out.append(text[i:j + 1])
            i = j + 1
            continue
        out.append(text[i])
        seen += 1
        i += 1
    out.append(_DIM)
    out.append(_ELLIPSIS)
    out.append(_OFF)
    return "".join(out)


def _shrink(old):
    """Give up the part of the old value that carries the least: the symbol
    printed after an address. The rest is left to wrapping."""
    if old.endswith(">") and " <" in old:
        return old[:old.index(" <")] + " <%s>" % _ELLIPSIS
    return old


def _delta(prefix, old, new, width):
    """One "old -> new" line. The new value is never given up."""
    line = (prefix + _OLD + old + _OFF + " " + _MARK + "->" + _OFF
            + " " + _MARK + new + _OFF)
    if _vlen(line) <= _budget(width):
        return line
    return (prefix + _OLD + _shrink(old) + _OFF + " " + _MARK + "->" + _OFF
            + " " + _MARK + new + _OFF)


def _dump_lines(head, before, value, width):
    """A memory row, arrows only on the words that moved.

    As many words to a line as the window holds, so the row never relies on
    wrapping to fit: a word split across two rows cannot be read as a number.
    Continuation lines align under the first word, which puts the words in
    columns and makes a changed one easy to spot."""
    olds = before.split()
    news = value.split()
    cells = []
    for k, word in enumerate(news):
        old = olds[k] if k < len(olds) else None
        if old is not None and old != word:
            cells.append((_OLD + old + _OFF + _MARK + "->" + _OFF
                          + _MARK + word + _OFF, True))
        else:
            cells.append((_VALUE + word + _OFF, False))
    indent = _vlen(head)
    room = max(8, _budget(width) - indent)
    # Unchanged words are padded to one width, so they stand in columns. A
    # changed word carries its arrow and is wider, so the line is packed by
    # the width it really takes rather than by a fixed count.
    word = max((len(w) for w in news), default=1)
    out = []
    pad = " " * indent
    row, used = [], 0
    for cell, mark in cells:
        text = cell if mark else cell.replace(_OFF, " " * (word - _vlen(cell))
                                              + _OFF, 1)
        size = _vlen(text) + (2 if row else 0)
        if row and used + size > room:
            out.append(((head if not out else pad)
                        + "  ".join(t for t, _ in row),
                        any(m for _, m in row)))
            row, used = [], 0
            size = _vlen(text)
        row.append((text, mark))
        used += size
    if row:
        out.append(((head if not out else pad)
                    + "  ".join(t for t, _ in row),
                    any(m for _, m in row)))
    return out or [(head, False)]


def _row_lines(i, name, stored, before, value, changed, width, stale=False):
    """Logical lines for one tracked row, each paired with whether that line
    is itself a change."""
    mark = "*" if changed else ("?" if stale else " ")
    head = "%s%s%2d%s %s%s%s %s=%s " % (
        _MARK if changed else _DIM, mark, i, _OFF,
        _NAME, name, _OFF, _PUNCT, _OFF)
    dump = (_dump_format(_split_format(stored)[0])
            and not _is_error(value)
            and not (changed and _is_error(before)))
    if dump:
        return _dump_lines(head, before if changed else value, value, width)
    if not changed:
        body = value.splitlines() or [""]
        return [(head + _VALUE + body[0] + _OFF, False)] + [
            (_VALUE + line + _OFF, False) for line in body[1:]]
    old_lines = before.splitlines() or [""]
    new_lines = value.splitlines() or [""]
    if len(old_lines) != len(new_lines):
        # The shape changed, typically into or out of scope. Show the new
        # value whole, with the old one folded to a placeholder.
        old_head = "{%s}" % _ELLIPSIS if len(old_lines) > 1 else old_lines[0]
        return [(_delta(head, old_head, new_lines[0], width), True)] + [
            (_MARK + line + _OFF, True) for line in new_lines[1:]]
    out = []
    for k, (old, new) in enumerate(zip(old_lines, new_lines)):
        prefix = head if k == 0 else ""
        if old == new:
            out.append((prefix + _VALUE + new + _OFF, False))
            continue
        # A member line is "  name = value,". Keep the name once and put the
        # arrow between the two values.
        lead, sep, old_val = old.partition(" = ")
        if sep and new.startswith(lead + " = "):
            new_val = new.partition(" = ")[2]
            out.append((_delta(prefix + _VALUE + lead + " = " + _OFF,
                               old_val.rstrip(","), new_val, width), True))
        else:
            out.append((_delta(prefix, old, new, width), True))
    return out


def _fold(head):
    """A struct head with its body folded away: "= {...}"."""
    if _plain(head).rstrip().endswith("{"):
        return head + _DIM + "%s}" % _ELLIPSIS + _OFF
    return head + _DIM + " " + _ELLIPSIS + _OFF


def _fit(groups, room):
    """Choose what to show when the rows do not all fit.

    A changed line is the whole point of the window, so it is the last thing
    dropped: unchanged struct bodies fold first, then the unchanged members
    of a changed struct, and only then does the tail get cut."""
    total = sum(len(lines) for _, lines in groups)
    if total <= room:
        return [text for _, lines in groups for text, _ in lines], 0, 0
    budget = max(1, room - 1)       # one row for the "... N more" note
    work = []
    for changed, lines in groups:
        if not changed and len(lines) > 1:
            work.append([(_fold(lines[0][0]), False)])
        else:
            work.append(list(lines))
    if sum(len(g) for g in work) > budget:
        for k, lines in enumerate(work):
            if len(lines) > 1 and any(c for _, c in lines[1:]):
                work[k] = [lines[0]] + [t for t in lines[1:] if t[1]]
    flat = [t for g in work for t in g]
    shown, cut = flat[:budget], flat[budget:]
    return ([text for text, _ in shown],
            len(cut) + (total - len(flat)),
            sum(1 for _, c in cut if c))


def _selected_depth():
    """How many frames up the selected frame sits, or 0 when it is innermost.

    None when there is no process to ask."""
    try:
        selected = gdb.selected_frame()
    except gdb.error:
        return None
    depth = 0
    frame = gdb.newest_frame()
    while frame is not None:
        if frame == selected:
            return depth
        frame = frame.older()
        depth += 1
    return 0


class VarWindow:
    def __init__(self, win):
        global _window
        self.win = win
        self.win.title = "vars"
        _window = self

    def close(self):
        global _window
        _window = None

    def render(self):
        self.win.erase()
        width = self.win.width
        height = self.win.height
        # 표현식 폭을 맞춰 '=' 이 한 열에 정렬되게 한다.
        names = [_labels.get(e, e) for e in _exprs]
        namew = max((len(n) for n in names), default=0)
        namew = min(namew, max(8, width // 3))
        depth = _selected_depth()
        groups = []
        for i, expr in enumerate(_exprs, 1):
            name = _labels.get(expr, expr).ljust(namew)
            if depth:
                # Another frame is selected. The expressions belong to the
                # innermost one, so re-reading them here would report them
                # missing and mark that as a change. Show the last reading
                # instead, and say nothing moved.
                value = _last.get(expr, "")
                groups.append((False, _row_lines(i, name, expr, None,
                                                 value, False, width)))
                continue
            value, stale = _read_stable(expr)
            before = _previous.get(expr)
            changed = (not stale) and before is not None and before != value
            groups.append((changed, _row_lines(i, name, expr, before,
                                               value, changed, width,
                                               stale=stale)))
        physical = []
        for changed, logical in groups:
            rows = []
            for text, line_changed in logical:
                rows.extend((piece, line_changed) for piece in _wrap(text, width))
            physical.append((changed, rows))
        room = max(1, height - 1)   # the last line belongs to the footer
        head = []
        if depth:
            head = ["%s reading frame ^%d: values are from the last stop%s"
                    % (_DIM, depth, _OFF)]
            room = max(1, room - 1)
        lines, hidden, hidden_changed = _fit(physical, room)
        if not lines:
            lines = ["%s nothing tracked yet%s" % (_DIM, _OFF)]
        if hidden:
            note = " ... %d more" % hidden
            if hidden_changed:
                note += ", %d changed" % hidden_changed
            # winheight counts the two border rows as well as the footer.
            note += " (winheight vars %d)" % (
                sum(len(rows) for _, rows in physical) + 3 + len(head))
            lines.append("%s%s%s" % (_DIM, note, _OFF))
        lines = head + lines
        room += len(head)
        footer = ("%s track <expr>   untrack N   delete track   "
              "* moved   ? not visible here%s" % (_DIM, _OFF))
        for line in lines:
            self.win.write(_clip(line, width) + "\n")
        self.win.write("\n" * max(0, room - len(lines)))
        self.win.write(_clip(footer, width))


def _redraw(_event=None):
    if _window is not None:
        _window.render()


def _current_pid():
    try:
        inferior = gdb.selected_inferior()
    except gdb.error:
        return None
    return inferior.pid if inferior is not None else None


def _snapshot(_event=None):
    """Remember the values as of the previous stop, so render() can mark what
    changed. render() must not touch this, because the TUI redraws far more
    often than the program stops."""
    global _pid
    pid = _current_pid()
    if pid != _pid:
        # A different process. Values from the previous run are not a delta,
        # they are another program's memory, and an arrow between the two
        # reads as a variable healing itself.
        _previous.clear()
        _last.clear()
        _good.clear()
        _pid = pid
    else:
        _previous.clear()
        _previous.update(_last)
        _last.clear()
    _last.update((expr, _read_stable(expr)[0]) for expr in _exprs)


def _on_exit(_event=None):
    global _pid
    _previous.clear()
    _last.clear()
    _good.clear()
    _pid = None


def _forget(expr):
    _previous.pop(expr, None)
    _last.pop(expr, None)
    _good.pop(expr, None)
    _labels.pop(expr, None)
    _groups.pop(expr, None)


def _pinned(expr):
    """EXPR rewritten as the address it refers to right now.

    This is what "watch -location" does, and for the same reason: an
    expression built from local names dies with its frame, while an address
    keeps naming the same object from libc, from a caller, and after the
    variable that pointed at it has moved on."""
    value = gdb.parse_and_eval(expr)
    if value.address is None:
        raise gdb.GdbError(
            "track -l needs something that lives in memory: %s does not" % expr)
    return "*(%s)%#x" % (value.type.pointer(), int(value.address))


# "track walk EXPR DEPTH" and "track deep EXPR DEPTH" put a whole structure
# on the board at once. Both are expansions: they turn one expression into the
# expressions for everything it reaches, and the window then re-reads those at
# every stop the way it re-reads a hand-typed one. So nothing here has to run
# again when the program moves.
#
# An expansion owns its rows. The name of the command that made them is the
# group, which is what lets "untrack walk s" take back exactly what
# "track walk s" put in, and what makes a second run replace rather than
# double.
_GROUP_MODES = ("walk", "deep")


def _available_rows():
    """How many more rows the window can take.

    render() gives its last line to the footer, so the usable height is one
    less. The number counts folded rows: a struct that changes opens up and
    takes more, and _fit() is what handles that. This is therefore a guard
    against an absurd request, not an exact limit."""
    room = max(1, _window.win.height - 1)
    return room - len(_exprs)


def _group_members(group):
    return [expr for expr in _exprs if _groups.get(expr) == group]


def _drop_group(group):
    for expr in _group_members(group):
        _exprs.remove(expr)
        _forget(expr)


def _expand(mode, name, expr, depth, field, limit):
    """The (expression, label) pairs for one expansion.

    The two chain refusals are rewritten here rather than in chase, because
    only this layer knows that the other command exists and that the field
    goes after the depth."""
    try:
        if mode == "walk":
            return chase.chain(expr, depth, field, cmd=name)
        return chase.deep(expr, depth, cmd=name, limit=limit)
    except chase.NoChainField as err:
        raise gdb.GdbError(
            '%s: %s has no field that points to %s. '
            'Use "tk deep" for this type.'
            % (name, err.type_name, err.type_name))
    except chase.AmbiguousChainField as err:
        raise gdb.GdbError(
            "%s: %s has %d fields that could be followed: %s. "
            "Name one: %s EXPR DEPTH FIELD"
            % (name, err.type_name, len(err.candidates),
               ", ".join(err.candidates), name))


def _track_group(tokens):
    """track walk|deep [-l] EXPR DEPTH [FIELD]"""
    mode = tokens[0]
    name = "tk %s" % mode
    rest = tokens[1:]
    pin = rest[0] in ("-l", "-location")
    if pin:
        rest = rest[1:]

    # The window is where the rows go and where the budget comes from. Adding
    # to a window that is not there would be a silent no-op at best.
    if _window is None:
        raise gdb.GdbError(
            '%s: the vars window is not open. Run "vars" first.' % name)
    usage = "Usage: %s EXPR DEPTH" % name
    if len(rest) < 2:
        raise gdb.GdbError("%s: give a depth. %s" % (name, usage))
    if mode == "deep" and len(rest) > 2:
        raise gdb.GdbError(
            "%s: deep follows every pointer, so it takes no field. %s"
            % (name, usage))
    if len(rest) > 3:
        raise gdb.GdbError("%s: too many arguments. %s [FIELD]" % (name, usage))

    expr, depth_text = rest[0], rest[1]
    field = rest[2] if len(rest) > 2 else None
    try:
        depth = int(depth_text)
    except ValueError:
        raise gdb.GdbError(
            "%s: depth must be a positive integer, not %r." % (name, depth_text))
    if depth < 1:
        raise gdb.GdbError("%s: depth must be a positive integer." % name)

    group = "%s %s" % (mode, expr)
    # A repeat replaces, so the rows this group already owns are its own to
    # spend again.
    available = _available_rows() + len(_group_members(group))
    if available < 1:
        raise gdb.GdbError(
            "%s: the vars window is full. Remove rows with utk first." % name)
    # walk counts nodes, so its depth is its row count and can be judged
    # before any memory is read. deep counts levels, and a branching type
    # turns a small depth into a large result, so it can only be measured
    # after the fact.
    if mode == "walk" and depth > available:
        raise gdb.GdbError(
            "%s: depth %d is too large. "
            "The vars window has room for %d more rows."
            % (name, depth, available))

    entries = _expand(mode, name, expr, depth, field, available)
    # Pinning is done here and not in the loop below, because _pinned() can
    # refuse a value that has no address. Doing it after the old rows were
    # dropped would leave the window holding half a group.
    if pin:
        entries = [(_pinned(expression), "@" + label)
                   for expression, label in entries]
    if len(entries) > available:
        # The count is "more than" rather than exact: deep stops expanding
        # once the answer cannot fit, so the true total may be far larger.
        raise gdb.GdbError(
            "%s: this expands to more than %d rows. "
            "The vars window has room for %d. Try a smaller depth."
            % (name, available, available))

    # Every refusal is above this line, so a rejected expansion leaves the
    # window exactly as it was instead of half filled.
    _drop_group(group)
    for expression, label in entries:
        if expression not in _exprs:
            _exprs.append(expression)
            _last[expression] = _previous[expression] = _read_stable(expression)[0]
        # A row that was already there by hand joins the group, so that one
        # "untrack walk s" still leaves the window in a state the user can
        # predict.
        _labels[expression] = label
        _groups[expression] = group
    _redraw()


class TrackCommand(gdb.Command):
    """Track an expression in the vars window.

Usage: track [-l|-location] EXPR

The expression is re-evaluated at every stop and shown on its own row, which
is overwritten rather than appended to. Compare "display", which scrolls.

With -l the expression is evaluated once and the row is rewritten to the
address it referred to, so it survives leaving the frame. Compare
"watch -location"."""

    def __init__(self):
        # The third argument is the completer. Without it a gdb.Command
        # defaults to COMPLETE_NONE, so Tab in the argument position offers
        # nothing. COMPLETE_EXPRESSION is what "print" uses, so "track" now
        # completes symbols and struct members the same way.
        super().__init__("track", gdb.COMMAND_USER, gdb.COMPLETE_EXPRESSION)

    def invoke(self, arg, from_tty):
        arg = arg.strip()
        # "walk" and "deep" are also ordinary variable names, so a single
        # token stays an expression: only "track walk EXPR ..." is the
        # subcommand. This is the same bargain the "print"/"p" guard below
        # already makes.
        tokens = arg.split()
        if len(tokens) >= 2 and tokens[0] in _GROUP_MODES:
            _track_group(tokens)
            return
        pin = False
        for flag in ("-location", "-l"):
            if arg == flag or arg.startswith(flag + " "):
                pin = True
                arg = arg[len(flag):].strip()
                break
        if not arg:
            raise gdb.GdbError("track takes an expression. see: info track")
        # Only a two-token form like "track print x" is the mistake this
        # catches. A single token IS the expression, and "p" is a common
        # variable name in list code, so "track p" must go through.
        tokens = arg.split(None, 1)
        if len(tokens) > 1 and tokens[0] in (
                "track", "tk", "display", "print", "p"):
            raise gdb.GdbError(
                "track takes an expression, not a command: try 'track %s'"
                % tokens[1])
        label = None
        if pin:
            fmt, expr = _split_format(arg)
            stored = _pinned(expr)
            if fmt:
                stored = "/%s %s" % (fmt, stored)
            label = "@" + arg
            arg = stored
        else:
            try:
                gdb.parse_and_eval(_split_format(arg)[1])
            except gdb.error as err:
                # Out of scope is normal, a typo is not. Warn either way and
                # add the row anyway, so the reason is visible at once.
                print("warning: %s" % err)
        if arg not in _exprs:
            _exprs.append(arg)
            # Seed both snapshots, so a new row is not marked as "changed"
            # until the program actually moves it.
            _last[arg] = _previous[arg] = _read_stable(arg)[0]
        if label:
            _labels[arg] = label
        _redraw()


class InfoTrackCommand(gdb.Command):
    """List the expressions tracked in the vars window."""

    def __init__(self):
        super().__init__("info track", gdb.COMMAND_STATUS)

    def invoke(self, arg, from_tty):
        if not _exprs:
            print("No tracked expressions now.")
            return
        print("Num  Expression")
        for i, expr in enumerate(_exprs, 1):
            label = _labels.get(expr)
            if label:
                print("%-4d %s   is   %s" % (i, label, expr))
            else:
                print("%-4d %s" % (i, expr))


class UntrackCommand(gdb.Command):
    """Stop tracking expression N. With no argument, stop tracking all."""

    def __init__(self):
        super().__init__("untrack", gdb.COMMAND_USER)

    def invoke(self, arg, from_tty):
        arg = arg.strip()
        if not arg:
            _clear()
            return
        # "untrack walk s" takes back exactly what "track walk s" put in.
        # Row numbers cannot do this: an expansion of twelve rows would need
        # twelve numbers, and they shift as soon as one is removed.
        tokens = arg.split()
        if len(tokens) == 2 and tokens[0] in _GROUP_MODES:
            group = " ".join(tokens)
            if not _group_members(group):
                raise gdb.GdbError(
                    'untrack: no group named "%s". see: info track' % group)
            _drop_group(group)
            _redraw()
            return
        for token in arg.split():
            try:
                index = int(token) - 1
            except ValueError:
                raise gdb.GdbError(
                    "untrack takes numbers. see: info track")
            if not 0 <= index < len(_exprs):
                raise gdb.GdbError(
                    "no tracked expression %s. see: info track" % token)
        # Remove from the back, so the earlier indices stay valid.
        for index in sorted((int(t) - 1 for t in arg.split()), reverse=True):
            _forget(_exprs.pop(index))
        _redraw()


def _clear():
    for expr in list(_exprs):
        _forget(expr)
    _exprs.clear()
    _groups.clear()
    _redraw()


class DeleteTrackCommand(gdb.Command):
    """Stop tracking expressions N. With no argument, stop tracking all.

This mirrors "delete display", so either half of the pair works:
"untrack 2" and "delete track 2" do the same thing."""

    def __init__(self):
        super().__init__("delete track", gdb.COMMAND_USER)

    def invoke(self, arg, from_tty):
        if arg.strip():
            gdb.execute("untrack %s" % arg.strip())
        else:
            _clear()


# gdb refuses a command window taller than the terminal height minus six,
# and only says "Invalid window height specified". A fixed number is
# therefore wrong for a panel the user resizes: it works in a tall terminal
# and silently does nothing in a short one. Measured on gdb 17.1: a 14-row
# terminal accepts 8, a 20-row one accepts 14, a 30-row one accepts 24.
#
# "tui new-layout" ignores the weight given to cmd: gdb always hands that
# window one third of the terminal, whatever the number says. Measured on
# gdb 17.1 with weights 1, 3, 6 and 12, all four give the same height:
#   64-row terminal -> 21   48 -> 16   36 -> 12   24 -> 8
# winheight is the only way past that, and the layouts in ~/.gdbinit want
# exactly the third gdb already gives, so the "vars" command no longer calls
# it. The fraction below is what a bare "cmdwin" resets to; "cmdwin ROWS"
# still takes any height the terminal will accept.
_CMD_ROWS_FRACTION = 0.33
_CMD_ROWS_MIN = 4
_TERMINAL_RESERVE = 7   # one more than the measured limit, as margin


def _terminal_rows():
    """Rows in the terminal, not in any one window.

    Once the TUI is on, gdb's own "height" parameter reports the command
    window instead of the screen: measured 15 on a 45-row terminal. Asking
    the operating system is the only reading that stays true either way."""
    try:
        return os.get_terminal_size(1).lines
    except OSError:
        pass
    try:
        rows = gdb.parameter("height")
    except gdb.error:
        rows = None
    return rows or 0


def _fit_cmd_window(wanted=None):
    """Size the command window against the terminal.

    "wanted" of None means the layout default: a fraction of the terminal,
    so the balance holds when the panel is dragged to another size."""
    rows = _terminal_rows()
    if not rows:
        return None
    if wanted is None:
        wanted = max(_CMD_ROWS_MIN, int(rows * _CMD_ROWS_FRACTION))
    target = min(wanted, rows - _TERMINAL_RESERVE)
    if target < _CMD_ROWS_MIN:
        # Too short to divide. Leave the layout weights to it.
        return None
    try:
        gdb.execute("winheight cmd %d" % target, to_string=True)
    except gdb.error:
        return None
    return target


class CmdWinCommand(gdb.Command):
    """Resize the command window to fit the terminal.

Usage: cmdwin [ROWS]

ROWS is a wish, not a demand: gdb caps it at the terminal height minus six,
so a short terminal gets a shorter command window instead of a warning."""

    def __init__(self):
        super().__init__("cmdwin", gdb.COMMAND_TUI)

    def invoke(self, arg, from_tty):
        arg = arg.strip()
        try:
            wanted = int(arg) if arg else None
        except ValueError:
            raise gdb.GdbError("cmdwin takes a number of rows. see: help cmdwin")
        if _fit_cmd_window(wanted) is None and from_tty:
            print("터미널이 낮아서 명령 창 높이는 그대로 둔다.")


class VarsLayoutCommand(gdb.Command):
    """Open a tracked-expression layout.

Usage: vars [src]

No argument gives the "vars" layout: a source window just tall enough to
show the arrow, and the rest split between tracked expressions and the
command window. "vars src" gives the "src-vars" layout, where the source
window is the large one, for reading code inside gdb rather than in the
editor. Both layouts are defined in ~/.gdbinit."""

    _LAYOUTS = {"": "vars", "src": "src-vars"}

    def __init__(self):
        super().__init__("vars", gdb.COMMAND_USER)

    def invoke(self, arg, from_tty):
        arg = arg.strip()
        try:
            layout = self._LAYOUTS[arg]
        except KeyError:
            raise gdb.GdbError(
                "vars takes no argument or \"src\". see: help vars")
        gdb.execute("layout %s" % layout)
        # Focus on the command window, so the arrow keys walk the command
        # history instead of scrolling the source. PageUp/PageDown still
        # scroll the source, and "focus src" puts the arrows back.
        gdb.execute("focus cmd")
        active = gdb.execute("info display", to_string=True)
        if "There are no auto-display expressions now" not in active:
            print("note: display expressions are still active and will keep "
                  "scrolling the command window. 'delete display' stops them.")


gdb.register_window_type("vars", VarWindow)
TrackCommand()
InfoTrackCommand()
UntrackCommand()
DeleteTrackCommand()
CmdWinCommand()
VarsLayoutCommand()
gdb.events.stop.connect(_snapshot)
gdb.events.exited.connect(_on_exit)
gdb.events.before_prompt.connect(_redraw)
