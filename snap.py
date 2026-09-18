"""Named, reusable checkpoints.

gdb's own "checkpoint" and "restart" have a quiet trap. A checkpoint is a
forked copy of the process, and "restart N" does not copy it back: it makes
that fork the live process. So the moment you run forward from it, the
checkpoint moves with you and the state you meant to keep is gone. Nothing
reports this. You believe you went back, and you did not.

"back" closes that hole. It restarts to the flag and then immediately takes a
fresh checkpoint at the same point, so the flag survives every return.

    snap [LABEL]    flag this moment      (LABEL defaults to 01, 02, ...)
    snaps           list the flags
    back [LABEL]    return to a flag      (LABEL defaults to the newest)

"back" completes on the labels, so a numbered scheme such as 01setup,
02inserted, 03bug lets Tab do the remembering.

Checkpoints are a native-target feature. A remote target (QEMU for Pintos,
JTAG on a board) has none of this.
"""

import re
import unicodedata

import gdb


# label, the checkpoint id behind it, and where it was taken. The id changes
# every time "back" re-flags, so nothing outside this module may hold on to it.
_flags = []

_ROW = re.compile(r"^\s*(\*?)\s*(\d+)\s+\w+\s+(.*)$")
_PID = re.compile(r"process\s+(\d+)")


def _checkpoints():
    """Map of checkpoint id -> process id, the live process included.

    The pid matters. A fresh "run" kills every fork and restarts the id
    counter at 1, so an id on its own is not a stable name: an old flag
    holding id 1 would silently point at a brand new, unrelated checkpoint.
    The pid never repeats within a session, so it tells the two apart."""
    try:
        out = gdb.execute("info checkpoints", to_string=True)
    except gdb.error:
        return {}
    found = {}
    for line in out.splitlines():
        row = _ROW.match(line)
        if not row:
            continue
        pid = _PID.search(row.group(3))
        found[int(row.group(2))] = int(pid.group(1)) if pid else None
    return found


def _checkpoint_ids():
    return set(_checkpoints())


def _active_id():
    """The id of the process that is running now, or None."""
    try:
        out = gdb.execute("info checkpoints", to_string=True)
    except gdb.error:
        return None
    for line in out.splitlines():
        row = _ROW.match(line)
        if row and row.group(1) == "*":
            return int(row.group(2))
    return None


def _prune():
    """Drop flags whose checkpoint is gone, e.g. after a fresh "run"."""
    live = _checkpoints()
    _flags[:] = [f for f in _flags
                 if f["cid"] in live and live[f["cid"]] == f["pid"]]


def _take_checkpoint():
    """Run "checkpoint" and return the id it created.

    "checkpoint" prints through a channel that to_string does not capture, so
    the id has to come from the difference in the list.

    Take the highest of the new ids, not the lowest. Before the first
    checkpoint exists, "info checkpoints" lists nothing at all, so the first
    one makes TWO rows appear: id 0, which is the live process, and id 1, the
    fork just made. The fork is always the higher one."""
    before = _checkpoint_ids()
    try:
        gdb.execute("checkpoint", to_string=True)
    except gdb.error as err:
        # With no live process this is "The program is not being run." Report
        # it as an error message, not as a Python traceback.
        raise gdb.GdbError("깃발을 찍을 수 없다: %s" % err)
    new = _checkpoint_ids() - before
    if not new:
        raise gdb.GdbError(
            "checkpoint 를 만들지 못했다. 프로세스가 실행 중인지 확인할 것.")
    cid = max(new)
    return cid, _checkpoints().get(cid)


def _drop_checkpoint(cid):
    """Delete a checkpoint, and say nothing if gdb will not.

    gdb refuses to delete the process that is running. That is a reason to
    leave it be, never a reason to abort the command that asked."""
    try:
        gdb.execute("delete checkpoint %d" % cid, to_string=True)
    except gdb.error:
        pass


def _where():
    try:
        frame = gdb.selected_frame()
    except gdb.error:
        return "?"
    sal = frame.find_sal()
    if sal.symtab is None:
        return frame.name() or "?"
    return "%s, %s:%d" % (frame.name() or "?",
                          sal.symtab.filename.split("/")[-1], sal.line)


def _display_width(text):
    """Columns the terminal spends on text. A Hangul syllable takes two."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1
               for ch in text)


def _pad(text, width):
    return text + " " * max(0, width - _display_width(text))


def _find(label):
    for flag in _flags:
        if flag["label"] == label:
            return flag
    return None


def _auto_label():
    used = {f["label"] for f in _flags}
    n = 1
    while "%02d" % n in used:
        n += 1
    return "%02d" % n


def _require_flags():
    _prune()
    if not _flags:
        raise gdb.GdbError("깃발이 없다. snap 으로 먼저 찍을 것.")


class SnapCommand(gdb.Command):
    """Flag this moment so "back" can return to it.

Usage: snap [LABEL]

Without a label the flag is numbered 01, 02, and so on. Reusing a label
replaces the flag that had it."""

    def __init__(self):
        super().__init__("snap", gdb.COMMAND_RUNNING)

    def invoke(self, arg, from_tty):
        _prune()
        label = arg.strip()
        if " " in label:
            raise gdb.GdbError("이름표에는 공백을 쓸 수 없다.")
        if not label:
            label = _auto_label()

        old = _find(label)
        where = _where()
        cid, pid = _take_checkpoint()
        if old is not None:
            # Replace rather than shadow, so the label means one thing.
            # gdb refuses to delete the process that is running, which the old
            # flag can be after a hand-typed "restart". Leave it alone then:
            # a spare checkpoint is harmless, a half-applied rename is not.
            _drop_checkpoint(old["cid"])
            old.update(cid=cid, pid=pid, where=where)
            print("깃발 이동 -> '%s'   (%s)" % (label, where))
            return
        _flags.append({"label": label, "cid": cid, "pid": pid, "where": where})
        print("깃발 -> '%s'   (%s)" % (label, where))


class SnapsCommand(gdb.Command):
    """List the flags that "back" can return to."""

    def __init__(self):
        super().__init__("snaps", gdb.COMMAND_STATUS)

    def invoke(self, arg, from_tty):
        _prune()
        if not _flags:
            print("깃발이 없다. snap 으로 찍을 것.")
            return
        width = max(_display_width(f["label"]) for f in _flags)
        for flag in _flags:
            print("  %s  %s" % (_pad(flag["label"], width), flag["where"]))
        print("  -> 총 %d 개. 가장 최근은 '%s' 다."
              % (len(_flags), _flags[-1]["label"]))


class BackCommand(gdb.Command):
    """Return to a flag taken with "snap".

Usage: back [LABEL]

Without a label this returns to the newest flag. The flag is re-taken on the
way, so the same one works again and again."""

    def __init__(self):
        super().__init__("back", gdb.COMMAND_RUNNING)

    def complete(self, text, word):
        # The "complete" command passes word as None, so fall back to text.
        prefix = word if word else (text or "")
        _prune()
        return [f["label"] for f in _flags if f["label"].startswith(prefix)]

    def invoke(self, arg, from_tty):
        _require_flags()
        label = arg.strip()
        if label:
            flag = _find(label)
            if flag is None:
                raise gdb.GdbError(
                    "'%s' 라는 깃발이 없다. 있는 것: %s"
                    % (label, ", ".join(f["label"] for f in _flags)))
        else:
            flag = _flags[-1]

        target = flag["cid"]
        abandoned = _active_id()
        gdb.execute("restart %d" % target)
        # The flag just became the live process. Take a fresh copy at this
        # exact point, or running forward would consume it.
        flag["cid"], flag["pid"] = _take_checkpoint()
        # "restart" does not end the process we were in; it leaves it in the
        # list as a fork nobody can name. Without this, every "back" adds one
        # more stopped process for the rest of the session. To keep a place
        # you might return to, give it a name with "snap" before leaving.
        if abandoned is not None and abandoned != target:
            if not any(f["cid"] == abandoned for f in _flags):
                _drop_checkpoint(abandoned)
        print("복귀 -> '%s'   (%s)" % (flag["label"], flag["where"]))


SnapCommand()
SnapsCommand()
BackCommand()
