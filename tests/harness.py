"""Assert helpers for the gdb-hosted test files.

Each test file runs inside gdb ("gdb --nx --batch -x FILE ./fixture"), so the
result has to leave gdb with an exit code the shell runner can read."""

import gdb

_passed = []
_failed = []


def check(name, got, want):
    if got == want:
        _passed.append(name)
    else:
        _failed.append("%s\n       got:  %r\n       want: %r" % (name, got, want))


def check_raises(name, fragment, fn, *args, **kwargs):
    """The call must fail, and its message must contain fragment."""
    try:
        fn(*args, **kwargs)
    except (gdb.error, gdb.GdbError) as err:
        if fragment in str(err):
            _passed.append(name)
        else:
            _failed.append("%s\n       message: %r\n       wanted:  %r"
                           % (name, str(err), fragment))
        return
    _failed.append("%s\n       no error raised, expected one about %r"
                   % (name, fragment))


def start(binary_note=""):
    """Run the fixture up to its "ready" function."""
    gdb.execute("set confirm off")
    gdb.execute("set pagination off")
    gdb.execute("tbreak ready", to_string=True)
    gdb.execute("run", to_string=True)


def report(suite):
    print("\n--- %s: %d passed, %d failed" % (suite, len(_passed), len(_failed)))
    for line in _failed:
        print("  FAIL %s" % line)
    gdb.execute("quit %d" % (1 if _failed else 0))
