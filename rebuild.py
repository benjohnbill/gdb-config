# "rebuild" — rebuild the executable gdb currently has loaded.
#
# gdb never compiles anything, so after editing a source file the loaded
# binary is stale and the TUI shows lines that are not the ones running.
# This command closes that gap so "rerun" can mean edit -> build -> run.
#
# Two sources, tried in this order:
#   1. A build record "<exe>.build" next to the executable. The "c dbg"
#      wrapper (~/.local/bin/c) writes it: its exact gcc argv, NUL-separated.
#      Replaying it keeps the wrapper's flag list the only copy of the flags.
#   2. A makefile above the executable, in the common layout of these labs:
#      a target whose name is the executable's basename, e.g. build/01_foo is
#      built by "make 01_foo".
# Other projects simply get a warning and no build.

import os
import subprocess

import gdb

MAKEFILES = ("GNUmakefile", "makefile", "Makefile")


def _find_make_dir(start):
    """Walk up from the executable looking for a makefile. Stop at $HOME or /."""
    home = os.path.expanduser("~")
    cur = start
    while True:
        for name in MAKEFILES:
            if os.path.isfile(os.path.join(cur, name)):
                return cur
        parent = os.path.dirname(cur)
        if parent == cur or cur == home:
            return None
        cur = parent


def _build_record(exe):
    """The argv the "c" wrapper recorded for this executable, or None."""
    try:
        with open(exe + ".build", "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return None
    return [os.fsdecode(a) for a in data.split(b"\0") if a]


def _run(argv):
    proc = subprocess.run(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    out = proc.stdout.strip()
    if out:
        print(out)
    return proc


class Rebuild(gdb.Command):
    """Rebuild the loaded executable: the "c dbg" build record, else make.

Aborts the enclosing command on a build error, so a failed build leaves the
running process untouched instead of killing it for nothing."""

    def __init__(self):
        super().__init__("rebuild", gdb.COMMAND_RUNNING)

    def invoke(self, arg, from_tty):
        exe = gdb.current_progspace().filename
        if not exe:
            print("rebuild: no executable loaded, nothing to build")
            return

        # The record first: only wrapper-built executables have one, so it is
        # the more specific signal, and makefile labs never reach this branch.
        argv = _build_record(exe)
        if argv:
            if _run(argv).returncode != 0:
                raise gdb.GdbError("rebuild: gcc failed, keeping the current process")
            return

        make_dir = _find_make_dir(os.path.dirname(os.path.abspath(exe)))
        if make_dir is None:
            print("rebuild: no makefile above %s, skipping the build" % exe)
            return

        target = arg.strip() or os.path.basename(exe)
        proc = _run(["make", "-C", make_dir, target])
        if proc.returncode == 0:
            return
        # A missing target means this project is not laid out the way we guessed.
        # That is not a compile error, so it must not block the run.
        if "No rule to make target" in proc.stdout:
            print("rebuild: no such target, skipping the build")
            return
        raise gdb.GdbError("rebuild: make failed, keeping the current process")


Rebuild()
