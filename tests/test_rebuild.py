"""The "rebuild" command on a "c dbg" build record, and without one."""

import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gdb
import harness
import rebuild              # noqa: F401  (registers the command)

check = harness.check
gdb.execute("set confirm off")

work = tempfile.mkdtemp(prefix="rebuild-test.")
src = os.path.join(work, "probe.c")
exe = os.path.join(work, "probe")
# The same shape the wrapper writes: the whole gcc argv, NUL-separated.
argv = ["gcc", "-g", "-O0", src, "-o", exe]


def write_source(body):
    with open(src, "w") as f:
        f.write(body)


def say(text):
    write_source('#include <stdio.h>\nint main(void) { puts("%s"); return 0; }\n'
                 % text)


def output():
    return subprocess.run([exe], stdout=subprocess.PIPE,
                          universal_newlines=True).stdout.strip()


def out(command):
    return gdb.execute(command, to_string=True)


say("old")
subprocess.run(argv, check=True)
with open(exe + ".build", "wb") as f:
    f.write(b"".join(os.fsencode(a) + b"\0" for a in argv))
gdb.execute("file " + exe, to_string=True)

say("new")
out("rebuild")
check("record: the edited source is compiled", output(), "new")

write_source("int main(void) { return 0 }\n")
harness.check_raises("record: a compile error stops the command",
                     "gcc failed", out, "rebuild", clean=False)
check("record: a failed compile leaves the old binary", output(), "new")

os.remove(exe + ".build")
say("unrecorded")
check("no record, no makefile: a warning, no build",
      "no makefile above" in out("rebuild"), True)
check("no record, no makefile: the binary is untouched", output(), "new")

harness.report("rebuild")
