---
scope: Handoff — make gdb "rerun" rebuild binaries made by the `c` wrapper
status: done 2026-09-28 — build record "<exe>.build" + rebuild.py replay; tests/test_rebuild.py
written: 2026-09-28, from a c-workshop tutoring session
---

# Handoff: `rebuild` for `c dbg` binaries

## Problem

`rerun` (`gdbinit`, `define rerun`) calls `rebuild` (`rebuild.py`), then
`kill`, `directory`, and `run`. `rebuild` looks for a makefile above the
loaded executable and runs `make`.

`c dbg FILE.c ARGS...` (`~/.local/bin/c`) compiles into a temp dir
`/tmp/c.XXXXXXXX/<basename>` and starts `gdb -q --args <exe> ARGS...`.
No makefile exists above `/tmp/c.XXXXXXXX/`, so `rebuild` prints a warning and
returns. Then `rerun` continues:

- The OLD binary runs.
- `directory` drops the source cache, so the TUI shows the NEW source.

The user sees edited source lines while the unedited code executes. This
happened in the session: a deleted `memcpy` line still corrupted data.

Evidence (2026-09-28):

```
rebuild: no makefile above /tmp/c.4IiCiaFc/argv_probe, skipping the build
```

## Goal

After the user edits `FILE.c` inside a `c dbg` session, `rerun` must compile
`FILE.c` again with the same flags, into the same exe path, and then run it.
A compile error must stop `rerun` and keep the current process (the same
contract `rebuild` already has for `make`).

## Constraints

- The makefile path in `rebuild.py` must keep working for other labs.
- Use the wrapper's exact flags. Do not copy them into a second place by hand;
  one source of truth.
- `~/.local/bin/c` changes are a user decision. The c-workshop `CLAUDE.md`
  says: do not change the wrapper before Stage 7 unless the user decides.
  The user chose to do this work in a separate session, so ask once before you
  edit the wrapper.
- `gdbinit` is also read by gdb 15 in a container. Keep new syntax compatible,
  or guard it.
- The wrapper deletes the temp dir on exit (`trap 'rm -rf "$build_dir"' EXIT`).
  The dir exists while gdb runs, because gdb is a child of the wrapper.
- This repo has uncommitted work in progress (`git status`). Do not commit
  or revert files that this task does not touch.

## Candidate approach (not decided)

1. The wrapper writes a small build record next to the exe in `dbg` mode,
   for example `<exe>.build` with the source path and the gcc argv. Or it
   exports environment variables before it starts gdb.
2. `rebuild.py`: when no makefile is found, look for that record. If it
   exists, run the recorded gcc command. On failure, raise `gdb.error`.
3. Keep the flags in one array in the wrapper, and write that array to the
   record.

Compare with the alternatives before you choose: environment variables
(`os.environ` in gdb Python) versus a file next to the exe.

## Verify

- In `~/dev/krafton-jungle/c-workshop`: `c dbg lab/argv_probe.c C 30`,
  `break main`, `run`. Edit the printf text in
  another shell. `rerun`. The new text must print.
- Put a syntax error in the file. `rerun` must print the gcc error and keep
  the process alive.
- A lab with a makefile must still use `make`.
- Run `tests/run.sh` in this repo.
