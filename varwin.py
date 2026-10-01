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
    untrack 1..4    remove rows 1 to 4, both included; "6.." runs to the end
    delete track    remove every expression    (compare: delete display)
    vars            switch to the layout that shows the window
    vars full       give that window the whole panel, minus a small prompt

A whole structure goes in with one command, and comes back out with one:

    track walk EXPR DEPTH [FIELD]   the first DEPTH nodes of a chain
    track deep EXPR DEPTH           EXPR and what it reaches, DEPTH levels
    track each [/FMT] PATTERN       one row per element or member: tri[0..5],
                                    s.items[], s.items[0..3]->id, s.*
    untrack walk EXPR               remove what "track walk EXPR" added
    untrack deep EXPR               remove what "track deep EXPR" added
    untrack each PATTERN            remove what "track each PATTERN" added

All three print once, without the window, under the names "walk EXPR
[FIELD]", "deep EXPR DEPTH" and "each [/FMT] PATTERN".

A pattern with a token in it needs no keyword: "track tri[0..5]" and
"untrack tri[0..5]" are the each forms. "each" answers to "e" here as well
as on its own, so "tk e tri[0..5]" is the same command. The group is named by the pattern as
typed, so "track tri[3..5]" adds a second group beside "track tri[0..2]"
rather than replacing it, and "untrack" takes the same text. The grammar is
in each.py.

walk and deep add one row per struct rather than one per member, because a
struct that did not move folds to "= {...}" while the one that did opens up
and shows which member moved. A row per member would spend the window's
height on the quiet ones and hide the answer. each is the deliberate
exception: it spends a row per element because the question it answers is
which element moved, and one folded row cannot say. All three refuse to run
when the window is closed, since that is where the rows go and where their
budget comes from. "-l" works here too: "track walk -l EXPR DEPTH" pins the
nodes at the addresses they hold right now, which is what you want when a
node is about to leave the list.

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
# command both need, so make it importable before asking for it. each.py is
# the second import-only module: it turns a pattern such as tri[0..5] into
# its rows for "track each", and importing it is also what registers the
# "each" command, so gdbinit needs no line for it.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import chase
import each

_exprs = []      # the expressions, in the order they were added
_labels = {}     # expression -> what the window calls it
_groups = {}     # expression -> the "walk EXPR" whose expansion put it here
_pinned_groups = set()   # the groups whose rows were rewritten to addresses
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
# 흐린 글씨는 \033[90m 이 아니라 38;5;244 로 쓴다. gdb 17 은 ANSI 의 8~15 를 AIXTERM_16
# 색으로 읽는데 `set style … foreground 8` 은 XTERM_256 이라, 같은 8 이 서로 다른 색으로
# 취급돼 둘째로 온 쪽이 curses 슬롯 9(SGR 91, bright red)를 받는다. 16 이상은 두 경로가
# 같은 색이다. gdbinit 의 회색 스타일도 244 이니 같이 바꿀 것.
_OFF   = "\033[0m"
_NAME  = "\033[39m"   # 표현식 이름  — 기본 전경
_VALUE = "\033[39m"   # 값
_PUNCT = "\033[38;5;244m"   # = 기호
_DIM   = "\033[38;5;244m"   # 번호 · 안내문
_MARK  = "\033[35m"   # 바뀐 값      — 강조
_OLD   = "\033[38;5;244m"   # 이전 값

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


def _tail_note(span, changed, stale):
    """The single line that stands for a run of rows a chain no longer reaches.

    Written in the note register rather than the row register, because it is
    not a value: it is the window saying it declined to repeat itself. It
    carries the same mark a row would, for the same reasons. A chain losing
    its tail is the kind of movement this window exists to catch, so
    collapsing the rows must not collapse the news; and a reading kept from
    an earlier stop is not current fact, so the note may not claim to be one
    while the rows above it admit they are not."""
    mark = "*" if changed else ("?" if stale else " ")
    return ("%s%s  %s %s%s: the chain does not reach here%s"
            % (_MARK if changed else _DIM, mark, _OFF, _DIM, span, _OFF))


def _dead_tails(readings, live):
    """Runs of rows a chain no longer reaches, and the note each one becomes.

    A chain row's expression is the row above it with one more "->field", so a
    row that cannot be read guarantees nothing below it in that group can be
    read either. Left alone they report the same break once per row, and the
    window is a third of a terminal.

    The run is counted from the end backwards, so a readable row is never
    hidden even when something above it fails for a reason of its own.

    Chain groups only. deep() lays its rows out breadth first, where the row
    below is a sibling at least as often as a child, so the same rule there
    would hide rows that are perfectly alive.

    A run of one is left alone. Collapsing it would save no height at all,
    and it would cost the row number the window is read for and the reason
    gdb gave for the failure. Losing only the last node is the commonest way
    a list shrinks, so that case has to stay a row.

    A group added with "-l" is left alone too. Pinning rewrites every row to
    its own absolute address, so the rows stop being one chain and the reason
    above no longer holds: each failure is its own finding about its own
    object, and "the chain does not reach here" would not be what happened.

    Returns ({first expression: (changed, stale, span)},
             {expressions the note stands for})."""
    notes = {}
    undrawn = set()
    for group in {g for g in _groups.values() if g.split(" ", 1)[0] == "walk"}:
        if group in _pinned_groups:
            continue
        members = _group_members(group)
        run = []
        for expr in reversed(members):
            value = readings[expr][0]
            # Out of scope is not a break in the chain. _read_stable already
            # answers that one by keeping the last reading, and it takes a
            # whole group at once rather than its tail.
            if not _is_error(value) or _is_invisible(value):
                break
            run.append(expr)
        if len(run) < 2:
            continue
        run.reverse()
        # Read exactly as a row reads itself, so the note cannot say "moved"
        # where the rows it replaced would have said "not current".
        stale = any(readings[e][1] for e in run)
        changed = live and any(
            (not readings[e][1])
            and _previous.get(e) is not None
            and _previous.get(e) != readings[e][0]
            for e in run)
        notes[run[0]] = (changed, stale,
                         "%s to %s" % (_labels.get(run[0], run[0]),
                                       _labels.get(run[-1], run[-1])))
        undrawn.update(run[1:])
    return notes, undrawn


def _more_hint(needed, height):
    """What to type to see the rows that did not fit.

    "vars full" is named whenever it would show all of them: it is one word,
    and it always gives this window every row the terminal has left. Once
    the window already has that height there is nothing left to name, so the
    hint says how tall the rows are instead and leaves the choice of what to
    untrack to the reader. winheight stays beside "vars full" for the middle
    case, where a few more rows are enough and the source window is worth
    keeping.

    "needed" and "height" are both content rows. winheight counts the two
    border rows as well, which is why the number it is given is two larger.
    The comparison is with the full layout rather than with the terminal,
    because the command window keeps three rows there and cannot go under
    them; see _full_layout_height."""
    full = _full_layout_height()
    if not full:
        # The terminal size could not be read, so "vars full" cannot be
        # promised to fit. winheight is the answer that needs no promise.
        return "winheight vars %d" % (needed + 2)
    if needed > full:
        return "needs %d rows, the screen holds %d" % (needed, full)
    if height < full:
        return "vars full, or winheight vars %d" % (needed + 2)
    return "winheight vars %d" % (needed + 2)


def _room(height, banner_rows):
    """The rows left for the tracked expressions themselves.

    The last line belongs to the footer, and a frame other than the innermost
    adds a banner above the rows. render() and the budget that "track walk"
    spends both read this, because two copies of the arithmetic drifted apart
    once already: the budget was one row too generous in exactly the case the
    banner covers."""
    room = max(1, height - 1)
    if banner_rows:
        room = max(1, room - banner_rows)
    return room


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
        # Every row is read once, before any of them is drawn. The collapse
        # below has to know what a whole group says before it can tell which
        # of its rows still deserve a line.
        #
        # Another frame selected means the expressions belong to the innermost
        # one, so re-reading them here would report them missing and mark that
        # as a change. The last reading is shown instead, and nothing moved.
        if depth:
            readings = {expr: (_last.get(expr, ""), False) for expr in _exprs}
        else:
            readings = {expr: _read_stable(expr) for expr in _exprs}
        # Not "hidden": _fit below binds that name to a row count, and the
        # two would be one name for two things inside one function.
        notes, undrawn = _dead_tails(readings, live=not depth)
        groups = []
        for i, expr in enumerate(_exprs, 1):
            if expr in undrawn:
                continue
            if expr in notes:
                note_changed, note_stale, span = notes[expr]
                groups.append((note_changed,
                               [(_tail_note(span, note_changed, note_stale),
                                 note_changed)]))
                continue
            name = _labels.get(expr, expr).ljust(namew)
            value, stale = readings[expr]
            if depth:
                groups.append((False, _row_lines(i, name, expr, None,
                                                 value, False, width)))
                continue
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
        head = []
        if depth:
            head = ["%s reading frame ^%d: values are from the last stop%s"
                    % (_DIM, depth, _OFF)]
        room = _room(height, len(head))
        lines, hidden, hidden_changed = _fit(physical, room)
        if not lines:
            lines = ["%s nothing tracked yet%s" % (_DIM, _OFF)]
        if hidden:
            note = " ... %d more" % hidden
            if hidden_changed:
                note += ", %d changed" % hidden_changed
            note += " (%s)" % _more_hint(
                sum(len(rows) for _, rows in physical) + 1 + len(head),
                height)
            lines.append("%s%s%s" % (_DIM, note, _OFF))
        lines = head + lines
        room += len(head)
        footer = ("%s track <expr>  tk walk|deep|each …  untrack N..M  "
              "* moved  ? not visible here%s" % (_DIM, _OFF))
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
#
# "track each PATTERN" is the third expansion: one pattern such as tri[0..5]
# or s.* becomes its elements or members, one row each. Its traversal lives
# in each.py. A pattern that carries a token needs no keyword, so
# "track tri[0..5]" arrives here too.
_GROUP_MODES = ("walk", "deep", "each")

# gdbinit gives "each" the short name "e", and a subcommand is matched by
# text, not by the command table, so the short name has to be spelled out
# here for "tk e tri[0..5]" to reach the same place as "tk each".
_MODE_ALIASES = {"e": "each"}


def _group_mode(token):
    """The group mode a first token names, or None when it names none."""
    mode = _MODE_ALIASES.get(token, token)
    return mode if mode in _GROUP_MODES else None


def _available_rows():
    """How many more rows the window can take.

    The number counts folded rows: a struct that changes opens up and takes
    more, and _fit() is what handles that. So this is a guard against an
    absurd request, not an exact limit."""
    return _room(_window.win.height,
                 1 if _selected_depth() else 0) - len(_exprs)


def _group_key(mode, expr):
    """The name an expansion answers to, for both halves of its life.

    Built in one place because "track walk s" and "untrack walk s" have to
    arrive at the same string or the second cannot find what the first
    added."""
    return "%s %s" % (mode, expr)


def _group_members(group):
    return [expr for expr in _exprs if _groups.get(expr) == group]


def _drop_group(group):
    _pinned_groups.discard(group)
    for expr in _group_members(group):
        _exprs.remove(expr)
        _forget(expr)


def _expand(mode, name, expr, depth, field, limit):
    """The (expression, label) pairs for one expansion.

    The refusals are rewritten here rather than in chase, because only this
    layer knows that the other commands exist and that the field goes after
    the depth."""
    try:
        if mode == "walk":
            return chase.chain(expr, depth, field, cmd=name)
        return chase.deep(expr, depth, cmd=name, limit=limit)
    except chase.NotAStruct as err:
        # An array cannot be a starting point, but each of its elements can,
        # and "tk each" takes the whole run. Anything else that is not a
        # struct has no second reading, so its refusal stands as chase wrote it.
        if not err.elements_are_structs:
            raise
        raise gdb.GdbError(
            '%s: %s is %s. %s starts at one struct. '
            'try "%s %s[0] %d", or "tk each %s" for a row per element.'
            % (name, expr, err.declared, mode, name, expr, depth, expr))
    except chase.NoChainField as err:
        raise gdb.GdbError(
            '%s: %s has no field that points to %s. '
            'use "tk deep" for this type.'
            % (name, err.type_name, err.type_name))
    except chase.AmbiguousChainField as err:
        raise gdb.GdbError(
            "%s: %s has %d fields that could be followed: %s. "
            "name one: %s EXPR DEPTH FIELD"
            % (name, err.type_name, len(err.candidates),
               ", ".join(err.candidates), name))


def _taller_hint():
    """The clause that names a layout with more room, or nothing at all.

    A refusal that only says how few rows are left sends the reader looking
    for rows to drop, when one command would have given them the rows
    instead. It is a separate clause rather than a rewrite of each message,
    because how many rows this window has left and how many another layout
    would have are two different facts and only the second names a command.

    Empty whenever there is nothing to offer: no window, no terminal to
    measure, or a window that already has the full layout's height."""
    if _window is None:
        return ""
    full = _full_layout_height()
    if not full or _window.win.height >= full:
        return ""
    room = _room(full, 1 if _selected_depth() else 0) - len(_exprs)
    if room <= _available_rows():
        return ""
    return ' "vars full" has room for %d.' % room


def _full(name):
    return gdb.GdbError(
        "%s: the vars window is full.%s remove rows first. see: info track"
        % (name, _taller_hint()))


def _expand_each(name, pattern, available):
    """The (expression, label) pairs for one "track each".

    The budget goes in as the limit, so a pattern that cannot fit stops
    expanding as soon as that is certain. The refusal is reworded here
    because only this layer knows where the rows were going."""
    try:
        return each.expand(pattern, cmd=name, limit=max(1, available))
    except each.TooMany:
        if available < 1:
            raise _full(name)
        raise gdb.GdbError(
            "%s: this expands past the %d rows the vars window has left. "
            "narrow the range.%s" % (name, available, _taller_hint()))


def _chain_args(mode, name, rest):
    """(expr, depth, field) for "track walk|deep", or the first refusal."""
    usage = "usage: %s EXPR DEPTH" % name
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
    # Evaluated before the budget is weighed, so that a misspelt name is
    # reported as a misspelt name rather than as a depth that does not fit.
    chase.evaluate(expr, name)
    return expr, depth, field


def _each_args(name, rest):
    """(format, pattern) for "track each", or the refusal.

    The pattern is the rest of the line: it has no DEPTH or FIELD after it
    to be confused with, and a plain "track a + b" takes spaces already."""
    usage = "usage: %s [/FMT] PATTERN" % name
    fmt, pattern = _split_format(" ".join(rest))
    if not pattern:
        raise gdb.GdbError("%s: give a pattern. %s" % (name, usage))
    return fmt, pattern


def _install_group(group, entries, existing, pin):
    """Put an expansion's rows in the window, replacing its previous run.

    Every refusal happens before this is called, so a rejected expansion
    leaves the window exactly as it was instead of half filled.

    A repeat goes back where the old one stood. Dropping and appending
    would shuffle the group past every row added since, and a list read
    top to bottom is the whole reason these rows are ordered at all."""
    position = _exprs.index(existing[0]) if existing else len(_exprs)
    _drop_group(group)
    for expression, label in entries:
        # A row the user added by hand keeps its own name and its own fate.
        # Adopting it would mean "untrack walk s" deleted something "track
        # walk s" never created.
        if expression in _exprs:
            continue
        _exprs.insert(position, expression)
        _labels[expression] = label
        _groups[expression] = group
        _last[expression] = _previous[expression] = _read_stable(expression)[0]
        position += 1
    # Recorded, not inferred from the rows: a pinned row is an address like
    # any other expression, and the collapse has to know the difference.
    if pin:
        _pinned_groups.add(group)
    _redraw()


def _track_group(tokens):
    """Put a whole chain, structure, or run of elements in the window at once.

        track walk|deep [-l] EXPR DEPTH [FIELD]
        track each [-l] [/FMT] PATTERN

    The refusals are ordered so that the reason a user hears is the first
    thing actually wrong, and every one of them happens before a single row
    is touched. An expansion that cannot fit therefore leaves the window
    exactly as it was rather than half filled."""
    mode = tokens[0]
    name = "tk %s" % mode
    rest = tokens[1:]
    pin = bool(rest) and rest[0] in ("-l", "-location")
    if pin:
        rest = rest[1:]

    # The window is where the rows go and where the budget comes from. Adding
    # to a window that is not there would be a silent no-op at best.
    if _window is None:
        raise gdb.GdbError(
            '%s: the vars window is not open. run "vars" first.' % name)
    fmt = None
    if mode == "each":
        fmt, pattern = _each_args(name, rest)
        key = pattern
    else:
        key, depth, field = _chain_args(mode, name, rest)

    group = _group_key(mode, key)
    existing = _group_members(group)
    # A repeat replaces, so the rows this group already owns are its own to
    # spend again.
    available = _available_rows() + len(existing)
    if mode == "each":
        # Expanded before the budget is judged, for the same reason the chain
        # forms evaluate first: a misspelt name is reported as one.
        entries = _expand_each(name, pattern, available)
        if available < 1:
            raise _full(name)
    else:
        if available < 1:
            raise _full(name)
        # walk counts nodes, so its depth is its row count and can be judged
        # before any memory is read. deep counts levels, and a branching type
        # turns a small depth into a large result, so it can only be measured
        # after the fact.
        if mode == "walk" and depth > available:
            raise gdb.GdbError(
                "%s: depth %d is too large. "
                "the vars window has room for %d more rows.%s"
                % (name, depth, available, _taller_hint()))
        entries = _expand(mode, name, key, depth, field, available)
    # Pinning is done here and not in _install_group, because _pinned() can
    # refuse a value that has no address. Doing it after the old rows were
    # dropped would leave the window holding half a group.
    if pin:
        entries = [(_pinned(expression), "@" + label)
                   for expression, label in entries]
    # The format goes on after the pin, as plain "track -l /x p" stores it.
    if fmt:
        entries = [("/%s %s" % (fmt, expression), label)
                   for expression, label in entries]
    if len(entries) > available:
        # Not an exact count: both expansions stop as soon as the answer
        # cannot fit, so the true total may be far larger than this.
        raise gdb.GdbError(
            "%s: this expands past the %d rows the vars window has left. %s"
            % (name, available,
               "narrow the range." if mode == "each" else "try a smaller depth."))
    _install_group(group, entries, existing, pin)


class TrackCommand(gdb.Command):
    """Track an expression in the vars window.

Usage: track [-l|-location] EXPR
       track walk EXPR DEPTH [FIELD]     the first DEPTH nodes of a chain
       track deep EXPR DEPTH             EXPR and what it reaches, DEPTH levels
       track each [/FMT] PATTERN         one row per element or member
       track e [/FMT] PATTERN            the same, under each's short name

The expression is re-evaluated at every stop and shown on its own row, which
is overwritten rather than appended to. Compare "display", which scrolls.

With -l the expression is evaluated once and the row is rewritten to the
address it referred to, so it survives leaving the frame. Compare
"watch -location".

A PATTERN is an expression with [A..B], [..], [] or .* in it: tri[0..5],
s.items[], s.items[0..3]->id, s.*. Such a pattern needs no "each" in front
of it. "track each EXPR" with no token steps through an array or a struct
whole. See "help each" for the grammar.

"walk", "deep" and "each" print these same three expansions once, without
the window."""

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
        mode = _group_mode(tokens[0]) if tokens else None
        if len(tokens) >= 2 and mode:
            _track_group([mode] + tokens[1:])
            return
        # A pattern such as tri[0..5] or s.* cannot be a C expression, so it
        # needs no keyword: it is "track each" whether or not that was typed.
        # This runs before the flags are read, so "-l" and "/x" travel with it.
        if each.has_pattern(arg):
            _track_group(["each"] + tokens)
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
            if label and label != expr:
                print("%-4d %s   is   %s" % (i, label, expr))
            else:
                print("%-4d %s" % (i, expr))


class UntrackCommand(gdb.Command):
    """Stop tracking expression N. With no argument, stop tracking all.

Usage: untrack N [N ...]          the rows numbered N, see "info track"
       untrack 1..4               rows 1 to 4, both included, as "tk tri[0..5]"
       untrack 6..   untrack ..4  from row 6 to the last; from the first to row 4

"untrack walk EXPR", "untrack deep EXPR" and "untrack each PATTERN" take
back what the matching "track" put in, and "untrack e PATTERN" is the same
as the last of those. A pattern with a token in it needs
no keyword here either: "untrack tri[0..5]", spelled as it was tracked. A
group added as "track each s.items" (no token) is removed with "untrack each
s.items" or by number."""

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
        group = None
        mode = _group_mode(tokens[0]) if tokens else None
        if len(tokens) >= 2 and mode == "each":
            group = _group_key("each", _split_format(" ".join(tokens[1:]))[1])
        elif len(tokens) == 2 and mode:
            group = _group_key(mode, tokens[1])
        elif each.has_pattern(arg):
            # The same sugar "track" takes, and the same key it built: the
            # format prefix is not part of the group's name.
            group = _group_key("each", _split_format(" ".join(tokens))[1])
        if group is not None:
            if not _group_members(group):
                raise gdb.GdbError(
                    'untrack: no group named "%s". see: info track' % group)
            _drop_group(group)
            _redraw()
            return
        # Every token is checked before any row goes, so a typo in the
        # second token cannot leave the first one half done.
        picked = _row_numbers(tokens)
        # Remove from the back, so the earlier indices stay valid.
        for index in sorted(picked, reverse=True):
            _forget(_exprs.pop(index))
        _redraw()


def _row_numbers(tokens):
    """The rows a list of tokens names, as 0-based indices.

    A token is a row number or a range of them: "3", "1..4", "6.." (to the
    last row), "..4" (from the first). The ".." is the one "tk tri[0..5]"
    uses, both ends included, so one habit serves both commands."""
    picked = set()
    last = len(_exprs)
    for token in tokens:
        if ".." in token:
            low_text, _, high_text = token.partition("..")
            try:
                low = int(low_text) if low_text else 1
                high = int(high_text) if high_text else last
            except ValueError:
                raise gdb.GdbError(
                    'untrack: "%s" is not a range of row numbers. '
                    "see: info track" % token)
            if high < low:
                raise gdb.GdbError(
                    "untrack: %s runs backwards. give the low number first."
                    % token)
            if high > last:
                raise gdb.GdbError(
                    "untrack: %s reaches past the last row, %d. see: info track"
                    % (token, last))
            numbers = range(low, high + 1)
        else:
            try:
                numbers = [int(token)]
            except ValueError:
                raise gdb.GdbError("untrack takes numbers. see: info track")
        for number in numbers:
            if not 1 <= number <= last:
                raise gdb.GdbError(
                    "no tracked expression %d. see: info track" % number)
            picked.add(number - 1)
    return picked


def _clear():
    for expr in list(_exprs):
        _forget(expr)
    _exprs.clear()
    _groups.clear()
    _pinned_groups.clear()
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
# winheight is the only way past that, and the layouts in ~/.config/gdb/gdbinit want
# exactly the third gdb already gives, so the "vars" command no longer calls
# it. The fraction below is what a bare "cmdwin" resets to; "cmdwin ROWS"
# still takes any height the terminal will accept.
_CMD_ROWS_FRACTION = 0.33
_CMD_ROWS_MIN = 4
_TERMINAL_RESERVE = 7   # one more than the measured limit, as margin

# gdb also refuses to make a window shorter than three rows, and it clamps in
# silence rather than saying so: "winheight cmd 1", "winheight cmd 2" and
# "winheight cmd 3" all draw the same screen. Measured on gdb 17.1 in a
# 50-row terminal, all three leave the vars window 44 rows. Three is
# therefore the smallest command window there is, and what "vars full" asks
# for. It is below _CMD_ROWS_MIN on purpose: that floor is what a balanced
# layout should not go under, and "vars full" is the one layout that should.
_CMD_ROWS_FULL = 3


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


def _fit_cmd_window(wanted=None, floor=_CMD_ROWS_MIN):
    """Size the command window against the terminal.

    "wanted" of None means the layout default: a fraction of the terminal,
    so the balance holds when the panel is dragged to another size. "floor"
    is the height below which resizing is not worth doing at all, and the
    caller lowers it for a layout that wants the smallest window gdb has."""
    rows = _terminal_rows()
    if not rows:
        return None
    if wanted is None:
        wanted = max(floor, int(rows * _CMD_ROWS_FRACTION))
    target = min(wanted, rows - _TERMINAL_RESERVE)
    if target < floor:
        # Too short to divide. Leave the layout weights to it.
        return None
    try:
        gdb.execute("winheight cmd %d" % target, to_string=True)
    except gdb.error:
        return None
    return target


def _full_layout_height():
    """The height this window gets under "vars full", in content rows.

    The whole terminal, less the three rows gdb will not take from the
    command window, the two borders of this one and the one status row.
    Measured against gdb 17.1 at four terminal sizes: 64 rows give this
    window 58, 52 give 46, 40 give 34 and 30 give 24, which is what the
    arithmetic below says at all four. 0 when the terminal cannot be
    measured, which the callers read as "do not promise anything"."""
    rows = _terminal_rows()
    if not rows:
        return 0
    return max(0, rows - _CMD_ROWS_FULL - 3)


class CmdWinCommand(gdb.Command):
    """Resize the command window to fit the terminal.

Usage: cmdwin [ROWS]

ROWS is a wish, not a demand: it is capped at the terminal height minus seven
(gdb's own limit is six, and one more is kept as margin), so a short terminal
gets a shorter command window instead of a warning."""

    def __init__(self):
        super().__init__("cmdwin", gdb.COMMAND_TUI)

    def invoke(self, arg, from_tty):
        arg = arg.strip()
        try:
            wanted = int(arg) if arg else None
        except ValueError:
            raise gdb.GdbError("cmdwin takes a number of rows. see: help cmdwin")
        if _fit_cmd_window(wanted) is None and from_tty:
            print("terminal too short to divide; "
                  "the command window keeps its height")


# A command window shrunk by "vars full" stays shrunk when another layout
# opens, so the flag below remembers to hand the rows back. Measured on
# gdb 17.1 in a 50-row terminal: "vars" gives the vars window 15 rows on its
# own and 22 straight after "vars full" without this flag, because the three
# rows left to the command window were still three. With it, 16: the height
# handed back is the fraction a bare "cmdwin" resets to, which lands one row
# off gdb's own third rather than on it.
_cmd_minimised = False


def _size_cmd_window(full, from_tty=False):
    """Give the command window the height the layout being opened wants.

    Only the full layout sets a height of its own. The others want exactly
    the third gdb hands them, so nothing is resized for them unless this
    module shrank the window earlier and owes the rows back."""
    global _cmd_minimised
    if full:
        _cmd_minimised = _fit_cmd_window(_CMD_ROWS_FULL,
                                         floor=_CMD_ROWS_FULL) is not None
        if not _cmd_minimised and from_tty:
            print("terminal too short to shrink the command window; "
                  "the layout keeps gdb's own split")
    elif _cmd_minimised:
        _fit_cmd_window()
        _cmd_minimised = False


class VarsLayoutCommand(gdb.Command):
    """Open a tracked-expression layout.

Usage: vars [src | full]

No argument gives the "vars-even" layout: a source window just tall enough
to show the arrow, and the rest split between tracked expressions and the
command window. "vars src" gives the "src-vars" layout, where the source
window is the large one, for reading code inside gdb rather than in the
editor. "vars full" gives the "vars-full" layout: no source window at all
and the command window at the three rows gdb will not go under, for a
structure with more rows than a third of the screen can hold.

"vars" and "vars src" give the even split back, and "cmdwin ROWS" sets any
other height for the command window. All three layouts are defined in
~/.config/gdb/gdbinit, and "layout vars", "layout vars src" and
"layout vars full" are the same three commands under gdb's own spelling."""

    _LAYOUTS = {"": "vars-even", "src": "src-vars", "full": "vars-full"}

    def __init__(self):
        super().__init__("vars", gdb.COMMAND_USER)

    def invoke(self, arg, from_tty):
        arg = arg.strip()
        try:
            layout = self._LAYOUTS[arg]
        except KeyError:
            raise gdb.GdbError(
                "vars takes no argument, \"src\" or \"full\". see: help vars")
        gdb.execute("layout %s" % layout)
        _size_cmd_window(arg == "full", from_tty)
        # Focus on the command window, so the arrow keys walk the command
        # history instead of scrolling the source. PageUp and PageDown do
        # nothing while it has focus (gdb 17.1); "focus src" gives them, and
        # the arrows, back to the source.
        gdb.execute("focus cmd")
        active = gdb.execute("info display", to_string=True)
        if "There are no auto-display expressions now" not in active:
            print("note: display expressions are still active and will keep "
                  "scrolling the command window. 'delete display' stops them.")


class TuiLayoutVarsCommand(gdb.Command):
    """Open a tracked-expression layout.

Usage: layout vars [src | full]

The three layouts of the "vars" command, under the name gdb uses for
layouts. This is a command rather than a layout of its own because gdb
reads only the first word after "layout" and drops the rest in silence:
with "vars" defined as a layout, "layout vars full" opens the plain one,
which is the wrong window, and nothing on the screen says so. Registering
here, in the place "tui new-layout vars" would have taken, is what makes
the rest of the line arrive.

The layouts therefore carry the names "vars-even", "src-vars" and
"vars-full", which also leaves the "vars" command free to apply one
without calling itself."""

    def __init__(self):
        super().__init__("tui layout vars", gdb.COMMAND_TUI)

    def invoke(self, arg, from_tty):
        try:
            gdb.execute(("vars %s" % arg.strip()).strip())
        except gdb.error as err:
            # execute() hands back a refusal from "vars" as a plain
            # gdb.error, and a gdb.error that escapes invoke is printed as
            # "Error occurred in Python: ...". Re-raising it as a GdbError
            # is what keeps a mistyped name a one-line answer.
            raise gdb.GdbError(str(err))


gdb.register_window_type("vars", VarWindow)
TrackCommand()
InfoTrackCommand()
UntrackCommand()
DeleteTrackCommand()
CmdWinCommand()
VarsLayoutCommand()
TuiLayoutVarsCommand()
gdb.events.stop.connect(_snapshot)
gdb.events.exited.connect(_on_exit)
gdb.events.before_prompt.connect(_redraw)
