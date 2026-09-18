# "rebuild" — run make for the executable gdb currently has loaded.
#
# gdb never compiles anything, so after editing a source file the loaded
# binary is stale and the TUI shows lines that are not the ones running.
# This command closes that gap so "rerun" can mean edit -> build -> run.
#
# It assumes the common layout of these labs: a makefile at the project root
# and a target whose name is the executable's basename, e.g. build/01_foo is
# built by "make 01_foo". Other projects simply get a warning and no build.

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


class Rebuild(gdb.Command):
    """Run make for the loaded executable.

Aborts the enclosing command on a build error, so a failed build leaves the
running process untouched instead of killing it for nothing."""

    def __init__(self):
        super().__init__("rebuild", gdb.COMMAND_RUNNING)

    def invoke(self, arg, from_tty):
        exe = gdb.current_progspace().filename
        if not exe:
            print("rebuild: no executable loaded, nothing to build")
            return

        make_dir = _find_make_dir(os.path.dirname(os.path.abspath(exe)))
        if make_dir is None:
            print("rebuild: no makefile above %s, skipping the build" % exe)
            return

        target = arg.strip() or os.path.basename(exe)
        proc = subprocess.run(
            ["make", "-C", make_dir, target],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
        )
        out = proc.stdout.strip()
        if out:
            print(out)
        if proc.returncode == 0:
            return
        # A missing target means this project is not laid out the way we guessed.
        # That is not a compile error, so it must not block the run.
        if "No rule to make target" in proc.stdout:
            print("rebuild: no such target, skipping the build")
            return
        raise gdb.error("rebuild: make failed, keeping the current process")


Rebuild()
