# debuginfod(심볼 자동 다운로드) 질문 끄기 — 컨테이너 gdb 15 에서도 이 파일을 읽는다
set debuginfod enabled off

set print pretty on
set history save on
set history size 10000
set startup-quietly on

# TUI layout
set tui tab-width 4
set tui compact-source on
set tui border-kind acs
set tui active-border-mode normal

# TUI colors — 24비트(#RRGGBB)는 ncurses 가 16색으로 내려버려 엉뚱한 슬롯을 고른다.
# 그래서 슬롯을 직접 지정한다. 16색은 Zed 테마가 다음 값으로 그린다:
#   기본전경 #564454 · 8(bright black) #745d71 · magenta #974787 · blue #8c4fa1
#   green #427431 · yellow #82640c · red #bc3358
# 회색은 8 이 아니라 244 로 지정한다. varwin.py·outwin.py 의 흐린 글씨도 38;5;244 다.
# gdb 17 은 `set style` 의 8(XTERM_256 의 8)과 ANSI \033[90m 의 8(AIXTERM_16 의 8)을 서로
# 다른 색으로 보고 curses 슬롯을 따로 나눠 주며, 터미널에는 번호가 아니라 슬롯이 간다
# (첫 색 = 슬롯 8 = SGR 90, 둘째 색 = 슬롯 9 = SGR 91 = bright red). 16 이상의 번호는 두
# 경로가 같은 색이라 슬롯도 하나다. 이 회색을 바꾸면 세 파일을 같이 바꿀 것.
# 아래 두 블록(색 슬롯 + disassembler 스타일)은 gdb 16 문법이라 15 에서는 건너뛴다.
# 컨테이너(gdb 15)는 기본색으로 뜬다. 색은 호스트 전용이니 문제 없다.
if $_gdb_major >= 16
  set style tui-border foreground 244
  set style tui-active-border foreground magenta
  set style tui-current-position on

  # 강조·구문 — gdb 기본값(red/green/yellow/cyan/blue)을 테마 슬롯으로 교체
  set style highlight foreground magenta
  set style highlight intensity normal
  set style function foreground magenta
  set style function intensity normal
  set style variable foreground none
  set style variable intensity normal
  set style filename foreground green
  set style filename intensity normal
  set style address foreground blue
  set style address intensity normal
  set style title foreground magenta
  set style version foreground magenta
  set style command foreground none
  set style line-number foreground 244
  set style line-number intensity normal
  set style metadata foreground 244
  set style metadata intensity normal
end

# 소스 하이라이팅은 Pygments 가 담당해서 set style 로 제어할 수 없다.
# 켜 두면 테마 밖의 원색이 섞이므로 끈다. 색은 Zed 편집기에서 보면 된다.
set style sources off

# 디스어셈블리
if $_gdb_major >= 16
  set style disassembler mnemonic foreground blue
  set style disassembler register foreground yellow
  set style disassembler immediate foreground yellow
  set style disassembler comment foreground 244
  set style disassembler comment intensity normal
end
set history filename ~/.config/gdb/history
set history remove-duplicates 1

# The cmd window is only a few lines tall in TUI; a paging prompt there is unusable
set pagination off

# Let the terminal keep its own scrollback and text selection
set tui mouse-events off

# Edit -> build -> run in one word. gdb itself never compiles, so "rebuild"
# (rebuild.py, sourced just below) rebuilds the loaded executable first, by
# replaying the "c dbg" wrapper's gcc command or else by calling make;
# on a compile error it raises, which stops this command before the kill and
# leaves the running process alone.
#
# A plain "run" after a rebuild executes the new binary but keeps showing the
# old source: gdb caches source file contents, and neither "run" nor "file"
# drops that cache. "directory" with no argument does.
#
# The guard matters: "kill" with no live process raises an error, and an error
# inside a define stops the whole command there.
# confirm is saved and restored rather than forced back to "on", so a user who
# works with "set confirm off" keeps that setting.
# Native targets only. A remote target (QEMU, JTAG) has no "run".
# "rebuild" for the command below: rebuild the loaded executable.
source ~/.config/gdb/rebuild.py

define rerun
  rebuild
  set $_rerun_confirm = $_gdb_setting("confirm")
  set confirm off
  if $_inferior_thread_count > 0
    kill
  end
  directory
  if $_rerun_confirm
    set confirm on
  else
    set confirm off
  end
  run
end
document rerun
Rebuild the loaded executable and run it again from the start.
The order is rebuild, kill the old process, drop the source cache, run. A
failed build stops before the kill, so the old process stays alive. Takes no
argument; "rebuild TARGET" is for the make route, a "c dbg" record ignores it.
end

# A TUI window that redraws tracked expressions in place, instead of the
# scrolling output that "display" produces.
# Commands: track / info track / untrack / delete track / vars / each
# "track walk EXPR DEPTH" and "track deep EXPR DEPTH" put a whole chain or a
# whole structure on the board at once; "untrack walk EXPR" takes it back.
# "track each PATTERN" (or just "track tri[0..5]") puts one row per element
# or member; "each PATTERN" prints the same lines once. A value with no parts
# to step through prints as it stands, numbered "$N" the way "print" numbers
# its own, so "each" is a "print" that expands as well and there is rarely a
# reason to type the other one.
# Short names for the ones typed most often are set up below: tk / itk / utk
# and e, which works in the subcommand slot too ("tk e tri[0..5]").
# chase.py needs no "source" line: varwin and walk import it themselves.
# each.py needs none either: varwin imports it, and that import is what
# registers "each". Sourcing it would put its helpers in the shared __main__.
source ~/.config/gdb/varwin.py

# A TUI window that holds the program's own output, so printf no longer
# scrolls the command window away or breaks the screen. The "out" command
# it defines opens the layout below.
#
# The import is what starts the collecting, not the window: gdb reads
# "inferior-tty" only as it launches the program, so a window opened later
# could never see a run that was already under way. The layout is therefore
# a view that can be opened at any point in a run. The program reads from
# that terminal too, so "out send TEXT" types into it and "out off" hands
# the keyboard back; see the docstring in outwin.py.
#
# Imported rather than sourced. "source x.py" runs the file in gdb's own
# __main__, which every sourced script shares, and outwin needs the same
# helper names varwin already uses there (_wrap, _clip, _window, _redraw ...).
# Sourcing it second silently replaced varwin's, and the vars window lost the
# ANSI-aware wrapping that its colours depend on. An import gives the file a
# namespace of its own, so the two cannot reach each other at all.
python
import os, sys
sys.path.insert(0, os.path.expanduser("~/.config/gdb"))
import outwin
end

# "walk EXPR [FIELD]" follows a chain of nodes. It knows no field name in
# advance, so it works on this week's ListNode and on anything later.
# Its traversal lives in chase.py, shared with "track walk" above.
source ~/.config/gdb/walk.py

# "deep EXPR DEPTH" prints what "track deep EXPR DEPTH" would put in the vars
# window: EXPR and every struct it reaches, DEPTH levels out. The traversal is
# chase.deep(), shared with that command, so a level means the same thing in
# both. Each line is numbered "$N" the way "print" numbers its own.
#
# Imported rather than sourced. It borrows each.py's line numbering, and a
# sourced file would mix the helpers of both into gdb's shared __main__; the
# "import outwin" comment above tells that story in full. sys.path was set
# there, so this needs only the import.
python
import deep
end

# "snap" / "snaps" / "back" are named checkpoints. Plain "restart" consumes a
# checkpoint the moment you run forward from it; "back" re-takes it instead.
source ~/.config/gdb/snap.py
# Three layouts, all opened by the "vars" command from varwin.py. None of
# them is named after the command that opens it: "vars", "vars src" and
# "vars full" are what you type, and "layout vars", "layout vars src" and
# "layout vars full" are the same three under gdb's own spelling. The names
# differ because "layout vars" is a command in varwin.py, not a layout, and
# a command cannot apply a layout of its own name without calling itself.
# The comment on TuiLayoutVarsCommand says why it has to be a command.
#
# "vars-even" is the working layout: source, tracked expressions and commands
# get a third of the panel each. "src-vars" (typed "vars src", also spelled
# "dbg") trades two of those thirds for a tall source window, for the times
# gdb itself has to show the code instead of the editor beside this panel.
# "vars-full" drops the source window and leaves the command window the three
# rows gdb will not go under, for a structure with more rows than a third of
# the screen can hold. "vars" or "vars src" gives the even split back, and
# "cmdwin ROWS" sets any other height for the command window.
#
# gdb ignores the cmd weight here. That window always takes one third of the
# terminal, whatever number the layout gives it, so only the src and vars
# weights do any work and they divide what is left. An equal split therefore
# needs nothing more than "src 1 vars 1", and "vars-full" cannot be written
# in weights at all: the winheight that gets it past the third is run by the
# "vars" command. Measured heights, gdb 17.1:
#           64-row        52-row        40-row        30-row
#   vars-even 21 22 21      17 18 17      13 14 13      10 10 10
#   src-vars  28 15 21      23 12 17      18  9 13      13  7 10
#   vars-full  - 60  3       - 48  3       - 36  3       - 26  3
# (src, vars, cmd, counted as "info win" counts them, borders included. The
# status window is one row at every size. Under "vars-full" the vars window
# shows two rows fewer than it is tall; see _full_layout_height in varwin.py.)
tui new-layout vars-even  src 1  vars 1  status 0  cmd 1
tui new-layout src-vars   src 2  vars 1  status 0  cmd 1
tui new-layout vars-full          vars 1  status 0  cmd 1

# A fourth layout trades the source window for the program's output: what it
# printed on top, tracked expressions in the middle, commands below, in the
# same thirds as "vars-even". The source is read in the editor beside this
# panel; what is hard to read anywhere else is the output, which until now
# landed in the command window and pushed everything else off the screen.
# Opened by "out" (outwin.py), the way "vars" opens its own.
tui new-layout out       out 1  vars 1  status 0  cmd 1

# A known flaw, and gdb's rather than ours: if "vars" or "out" is the first
# thing in a session to turn the TUI on, the command window misbehaves from
# then on. The typed command and the next line are glued
# ("(gdb) runStarting program: ..."), the "Breakpoint 1 at ..." reply of a
# "break" shows up behind the next prompt and is painted over a moment later,
# and "finish" prints its lines in the wrong order. Seen on gdb 17.1, in tmux
# as well as in the raw bytes, so it is not the capture.
#
# The cause: gdb.execute() puts back, when it returns, the ui_out it found on
# entry (python.c). "layout" inside it switched on the TUI, and that swaps in
# the TUI's own ui_out (tui_setup_io, tui-io.c), so the restore undoes the
# swap. Every reply that goes through ui_out ("Starting program: ", "Breakpoint
# 1 at ...", "Run till exit from ...") then reaches the terminal directly and
# not the command window, while gdb_printf text still goes to the window, which
# is the reordering. "vars" and "out" are Python commands that call "layout", so
# they hit it; so does a bare Python command that only runs "layout src",
# while a "define" command or a typed "layout src" does not.
#
# Nothing in the window code can undo that, but the TUI can be switched on
# before the Python command starts, and a hook is the one place that runs
# outside any Python frame: gdb runs "hook-NAME" first, as a plain gdb
# command, and "tui enable" does nothing once the TUI is up. The hook cannot
# see the arguments of the command it precedes, which is why "out" is a
# prefix command (outwin.py): "out send" and "out off" are commands of their
# own and never reach hook-out, so they do not switch the TUI on. The third
# hook is "layout vars", which is a command of its own as well.
# Without the hooks, "tui enable" typed before the first "vars" or "out" does
# the same, and "tui disable" followed by "tui enable" repairs a session that
# is already like this; the layout stays.
define hook-vars
  tui enable
end
define hook-out
  tui enable
end
define tui layout hook-vars
  tui enable
end

# "track" is the most typed command here, so give it a two-letter name.
# Not "tr": gdb already ships that as an alias of "trace" (with trac, tra, tp),
# and it refuses to redefine an existing alias.
# These must come after the "source varwin.py" line above, which is what
# defines "track", "info track" and "untrack".
alias tk = track
alias itk = info track
alias utk = untrack

# "each" is typed as often as "print" is, because it does what print does
# and expands as well, so it gets the shortest name left. On its own "e" is
# ambiguous (echo, edit, enable, eval, exit, explore), and an alias settles
# it: gdb prefers an exact name over any abbreviation. "edit" loses its
# one-letter form, which costs nothing here, where the editor is elsewhere.
# varwin spells "e" out again for "tk e PATTERN"; a subcommand is matched by
# text and does not see the command table.
alias e = each

# The old "dbg" was "tui enable" plus "cmdwin". The "vars" command does both
# and focuses the command window as well, so dbg is now just the name for the
# large-source layout. Ctrl-x a followed by "vars" is the other entry point.
alias dbg = vars src
