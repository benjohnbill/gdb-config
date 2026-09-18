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


# What gdb.execute() does to an error, measured on gdb 17.1:
#   a command that raises GdbError  -> gdb.error("a tidy refusal")
#   a gdb.error that escapes invoke -> gdb.error("Error occurred in Python: ...")
# So through execute() every failure arrives as gdb.error and the marker is
# the only thing that tells the two apart. A direct call keeps the classes
# distinct instead. Both readings are checked, because an earlier version of
# this file accepted either class and that is exactly how an unevaluatable
# expression reached a real session.
_LEAK = "Error occurred in Python"


def check_raises(name, fragment, fn, *args, clean=True, **kwargs):
    """The call must fail with a message a user can act on.

    clean=False for a call made through gdb.execute(), which flattens every
    command error to gdb.error and leaves only the marker to go on."""
    try:
        fn(*args, **kwargs)
    except (gdb.error, gdb.GdbError) as err:
        text = str(err)
        if _LEAK in text:
            _failed.append("%s\n       a Python exception escaped the command:"
                           "\n       %r" % (name, text))
        elif clean and not isinstance(err, gdb.GdbError):
            _failed.append("%s\n       raised %s, not GdbError: %r"
                           % (name, type(err).__name__, text))
        elif fragment in text:
            _passed.append(name)
        else:
            _failed.append("%s\n       message: %r\n       wanted:  %r"
                           % (name, text, fragment))
        return
    _failed.append("%s\n       no error raised, expected one about %r"
                   % (name, fragment))


def labels(entries):
    """Just the display names of an expansion, for a readable assertion."""
    return [label for _, label in entries]


def start():
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
