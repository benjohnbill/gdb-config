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
# 아래 두 블록(색 슬롯 + disassembler 스타일)은 gdb 16 문법이라 15 에서는 건너뛴다.
# 컨테이너(gdb 15)는 기본색으로 뜬다. 색은 호스트 전용이니 문제 없다.
if $_gdb_major >= 16
  set style tui-border foreground 8
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
  set style line-number foreground 8
  set style line-number intensity normal
  set style metadata foreground 8
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
  set style disassembler comment foreground 8
  set style disassembler comment intensity normal
end
set history filename ~/.config/gdb/history
set history remove-duplicates 1

# The cmd window is only a few lines tall in TUI; a paging prompt there is unusable
set pagination off

# Let the terminal keep its own scrollback and text selection
set tui mouse-events off

# Edit -> build -> run in one word. gdb itself never compiles, so "rebuild"
# (rebuild.py, sourced just above) calls make for the loaded executable first;
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
# "rebuild" for the command below: run make for the loaded executable.
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
Rebuild first, then rerun: make the loaded executable, kill the old process,
drop the source cache, run. A failed build stops here, so the process you
were debugging stays alive. Takes no argument; "rebuild TARGET" builds one
target by hand.
end

# A TUI window that redraws tracked expressions in place, instead of the
# scrolling output that "display" produces.
# Commands: track / info track / untrack / delete track / vars
# "track walk EXPR DEPTH" and "track deep EXPR DEPTH" put a whole chain or a
# whole structure on the board at once; "untrack walk EXPR" takes it back.
# Short names for the three typed most often are set up below: tk / itk / utk
# chase.py needs no "source" line: varwin and walk import it themselves.
source ~/.config/gdb/varwin.py

# A TUI window that holds the program's own output, so printf no longer
# scrolls the command window away or breaks the screen. The "out" command
# it defines opens the layout below.
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

# "snap" / "snaps" / "back" are named checkpoints. Plain "restart" consumes a
# checkpoint the moment you run forward from it; "back" re-takes it instead.
source ~/.config/gdb/snap.py
# Two layouts, both opened by the "vars" command from varwin.py.
#
# "vars" is the working layout: source, tracked expressions and commands get
# a third of the panel each. "vars src" (also spelled "dbg") trades two of
# those thirds for a tall source window, for the times gdb itself has to show
# the code instead of the editor beside this panel.
#
# gdb ignores the cmd weight here. That window always takes one third of the
# terminal, whatever number the layout gives it, so only the src and vars
# weights do any work and they divide what is left. An equal split therefore
# needs nothing more than "src 1 vars 1". Measured heights, gdb 17.1:
#           64-row        52-row        40-row        30-row
#   vars      21 22 21      17 18 17      13 14 13      10 10 10
#   src-vars  28 15 21      23 12 17      18  9 13      13  7 10
# (src, vars, cmd; the status window is one row at every size.)
tui new-layout vars      src 1  vars 1  status 0  cmd 1
tui new-layout src-vars  src 2  vars 1  status 0  cmd 1

# The third layout trades the source window for the program's output: what it
# printed on top, tracked expressions in the middle, commands below, in the
# same thirds as "vars". The source is read in the editor beside this panel;
# what is hard to read anywhere else is the output, which until now landed in
# the command window and pushed everything else off the screen.
# Opened by "out" (outwin.py), the way "vars" opens its own.
tui new-layout out       out 1  vars 1  status 0  cmd 1

# "track" is the most typed command here, so give it a two-letter name.
# Not "tr": gdb already ships that as an alias of "trace" (with trac, tra, tp),
# and it refuses to redefine an existing alias.
# These must come after the "source varwin.py" line above, which is what
# defines "track", "info track" and "untrack".
alias tk = track
alias itk = info track
alias utk = untrack

# The old "dbg" was "tui enable" plus "cmdwin". The "vars" command does both
# and focuses the command window as well, so dbg is now just the name for the
# large-source layout. Ctrl-x a followed by "vars" is the other entry point.
alias dbg = vars src
