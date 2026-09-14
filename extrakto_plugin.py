#!/usr/bin/env python3

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict

# shutil and traceback are imported where they are used: both are off the
# launch path (cleanup after fzf closes, and the error branch), and each costs
# a couple of ms of import time that every child process would otherwise pay.

from extrakto import Extrakto, get_lines

SCRIPT = os.path.realpath(__file__)
SCRIPT_DIR = os.path.dirname(SCRIPT)
MODULE = os.path.splitext(os.path.basename(SCRIPT))[0]
PYTHON = sys.executable

COLORS = {
    "RED": "\033[0;31m",
    "GREEN": "\033[0;32m",
    "BLUE": "\033[0;34m",
    "PURPLE": "\033[0;35m",
    "CYAN": "\033[0;36m",
    "WHITE": "\033[0;37m",
    "YELLOW": "\033[0;33m",
    "OFF": "\033[0m",
    "BOLD": "\033[1m",
}

FZF_FLUSH_BYTES = 8192

# pane captures are cached to disk so fzf's reload can reuse them; NUL cannot
# appear in captured text, so it is a safe chunk separator
CHUNK_SEP = "\0"

# every candidate carries "<token><US><pane_id><US><socket><US><cwd>": ctrl-j needs
# the pane, and relative paths have to resolve against the cwd of the pane the text
# came from, not ours. fzf displays and matches field 1 only. US (0x1f) rather than
# tab, because tmux hands us real tabs inside captured lines.
META_SEP = "\x1f"

# "src/main.py:42", "src/main.py:42:13", "src/main.py:42:" -> ("src/main.py", "42")
RE_PATH_LINE = re.compile(r"^(.+?):(\d+)(?::\d+)?:?$")


# Written into the state dir and used as fzf's --preview. It has to mirror
# split_path_line/resolve_path, because a candidate is not a plain path: path-line
# results carry a :line[:col] suffix, and a relative path belongs to the pane it
# came from. Kept as shell rather than a --preview subcommand of this script: it
# runs on every cursor move, and a python start-up per keypress is far too slow.
PREVIEW_SCRIPT = r"""#!/usr/local/bin/bash
# $1 = candidate token (fzf field 1), $2 = cwd of the originating pane (field 4)
tok=$1
cwd=$2

EZA=/usr/local/bin/eza
BAT=/usr/local/bin/bat

resolve() {
    local p=$1
    [[ $p == '~' ]] && p=$HOME
    [[ $p == '~/'* ]] && p=$HOME/${p#'~/'}
    if [[ $p != /* && -n $cwd && -e $cwd/$p ]]; then
        p=$cwd/$p
    fi
    printf '%s' "$p"
}

# Peel a trailing :line[:col]. Trying the three-field form first is what makes
# file:42:13 give line 42 rather than column 13 - the same choice
# split_path_line() makes with its non-greedy match.
bare=${tok%:}
line=
if [[ $bare =~ ^(.*):([0-9]+):([0-9]+)$ ]]; then
    bare=${BASH_REMATCH[1]}
    line=${BASH_REMATCH[2]}
elif [[ $bare =~ ^(.*):([0-9]+)$ ]]; then
    bare=${BASH_REMATCH[1]}
    line=${BASH_REMATCH[2]}
else
    bare=$tok
fi

show() {
    local p=$1
    if [[ -d $p ]]; then
        exec "$EZA" --color always -T "$p"
    elif [[ -f $p ]]; then
        if [[ -n $line ]]; then
            # scroll to the hit instead of opening at line 1, where the
            # highlighted line would be off-screen in a short preview window
            local start=$(( line - 5 ))
            (( start < 1 )) && start=1
            exec "$BAT" --color always --paging never -H "$line" -r "$start:" "$p"
        fi
        exec "$BAT" --color always --paging never "$p"
    fi
}

show "$(resolve "$bare")"
line=
show "$(resolve "$tok")"   # a filename that really does contain ":42"
printf '%s\n' "$tok"
"""


def resolve_path(token, cwd=None):
    """Turn a candidate into a path the exec/shell actions can actually use.

    Two things have to happen here. ~ must be expanded by us: shlex.quote wraps a
    leading ~ in single quotes, which defeats shell expansion, and open() is handed
    an argv element with no shell involved at all. And a relative path has to be
    resolved against the pane it was captured from, which is not necessarily the
    directory we are running in. Only edit/open use this - insert and copy keep the
    text exactly as it appeared on screen.
    """
    expanded = os.path.expanduser(token)
    if os.path.isabs(expanded) or not cwd:
        return expanded
    joined = os.path.join(cwd, expanded)
    # only rewrite to the origin pane's directory if that is where it lives
    return joined if os.path.exists(joined) else expanded


def split_path_line(token, cwd=None):
    """Split a grep/compiler style file:line[:col] token into (path, line)."""
    m = RE_PATH_LINE.match(token)
    if not m:
        return token, None
    path, line = m.group(1), m.group(2)
    # only treat it as file:line if the file part really is a file, otherwise a
    # plain token like "note:42" would get mangled
    if os.path.exists(resolve_path(path, cwd)):
        return path, line
    return token, None


def parse_pane_line(line):
    """Parse a "#{pane_id}\\t#{pane_current_path}" list-panes row."""
    pane, _tab, cwd = line.partition("\t")
    return pane, (cwd or None)


def split_meta(line):
    """Split a fzf result line back into (token, pane_id, socket, cwd)."""
    parts = line.split(META_SEP)
    if len(parts) < 4:
        return line, None, None, None
    return parts[0], parts[1] or None, parts[2] or None, parts[3] or None


def fzf_sel(command, batches):
    p = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None
    )
    assert p.stdin is not None and p.stdout is not None
    try:
        # one flush per captured pane. flushing per line would cost a syscall per
        # token, but waiting on a byte threshold alone held the trigger pane's
        # candidates back until some later, slower pane pushed the buffer over it
        # - measured at ~22 ms, nearly the whole capture. note fzf only draws
        # once it has enough items to fill the list area (~14 rows on a 30-row
        # popup), so a sparse trigger pane can still look blank for a while.
        buf = bytearray()
        for batch in batches:
            for line in batch:
                buf += line.encode("utf-8")
                buf += b"\n"
                if len(buf) >= FZF_FLUSH_BYTES:
                    p.stdin.write(buf)
                    p.stdin.flush()
                    buf.clear()
            if buf:
                p.stdin.write(buf)
                p.stdin.flush()
                buf.clear()
    except BrokenPipeError:
        pass
    # communicate(), not wait()-then-read: a large multi-selection can fill the
    # stdout pipe, and fzf would block writing while we block in wait()
    out, _err = p.communicate()
    res = out.decode("utf-8").split("\n")
    # omit last empty line
    return res[:-1]


def get_cap_batches(sel_filter, chunks, *, extrakto):
    """Yield one list of candidates per captured pane.

    The batch boundary is the point where the next capture may block, so it is
    also the only point where a consumer can usefully flush. Keeping it in the
    producer means fzf_sel does not have to guess where a pane ended.
    """
    seen = set()
    any_match = False

    for data, pane, socket, cwd in chunks:
        if sel_filter == "line":
            res = get_lines(data)
        elif sel_filter == "all":
            res = []
            for name in extrakto.all():
                res += extrakto[name].filter(data)
        else:
            res = extrakto[sel_filter].filter(data)

        batch = []
        for item in reversed(res):
            if item not in seen:
                # dedup is by token, so a token seen in several panes keeps the
                # first origin - and the trigger pane is always captured first
                seen.add(item)
                batch.append(
                    f"{item}{META_SEP}{pane}{META_SEP}{socket or ''}"
                    f"{META_SEP}{cwd or ''}"
                )
                any_match = True
        if batch:
            yield batch

    if not any_match:
        yield [f"NO MATCH - use a different filter{META_SEP}{META_SEP}{META_SEP}"]


def get_cap(sel_filter, chunks, *, extrakto):
    """Flat view of get_cap_batches, for callers that do not care about panes."""
    for batch in get_cap_batches(sel_filter, chunks, extrakto=extrakto):
        yield from batch


class ExtraktoPlugin:

    def __init__(self, trigger_pane, launch_mode, state_dir, trigger_path=None):
        self.trigger_pane = trigger_pane
        self.launch_mode = launch_mode
        self.state_dir = state_dir
        # the key binding expands #{pane_current_path} for us, which saves a
        # tmux round trip and is the only reliable source now that the popup no
        # longer inherits the trigger pane's directory
        self.trigger_path = trigger_path

        self.clip_tool = "/usr/bin/pbcopy"
        self.clip_mode = "bg"
        self.clip_mode_key = "ctrl-t"
        self.copy_key = "ctrl-y"
        self.edit_key = "ctrl-e"
        self.editor = os.environ.get("EDITOR", "vi")
        self.filter_key = "ctrl-f"
        self.fzf_header = "i c o e q s p l f g r j"
        self.jump_key = "ctrl-j"
        self.refresh_key = "ctrl-r"
        self.fzf_layout = "reverse"
        self.fzf_tool = "/usr/local/bin/fzf"
        self.grab_area = "all full"
        self.grab_key = "ctrl-g"
        self.insert_key = "enter"
        self.line_key = "ctrl-l"
        self.open_key = "ctrl-o"
        self.open_tool = "/usr/bin/open"
        self.path_key = "ctrl-p"
        self.quote_key = "ctrl-q"
        self.squote_key = "ctrl-s"
        self.alt = "all"
        self.prefix_name = "all"
        self.extra_sockets = ["tokyo", "seafoam"]

        # sockets that are not running are probed once per process, not once
        # per capture
        self.dead_sockets = set()

        # built lazily: --transform runs on every filter keypress and does not
        # need the config parsed
        self._extrakto_all = None
        self._extrakto_any = None

        # bumped by ctrl-r; part of the cache filename so a capture that is still
        # streaming cannot republish its stale result over a refreshed one
        self.generation = 0

        self.original_grab_area = self.grab_area

        filter_order = "word path path-line quote s-quote url line all".split()
        self.next_filter = self.prep_cycle(filter_order)
        self.sel_filter = (
            os.environ.get("extrakto_inital_mode", "").strip() or filter_order[0]
        )

        clip_mode_order = "bg buffer".split()
        self.next_clip_mode = self.prep_cycle(clip_mode_order)

        os.environ.pop("FZF_DEFAULT_OPTS", None)
        os.environ.pop("FZF_DEFAULT_OPTS_FILE", None)

    def extrakto_for(self, sel_filter):
        """The one Extrakto this filter needs, built on demand.

        Passing both as keyword arguments defeated the lazy properties: Python
        evaluates every argument before the call, so every run parsed the config
        twice (~5 ms) even though only one variant is ever used.
        """
        if sel_filter == "line":
            return None
        return self.extrakto_all if sel_filter == "all" else self.extrakto_any

    @property
    def extrakto_all(self):
        if self._extrakto_all is None:
            self._extrakto_all = Extrakto(alt=True, prefix_name=True)
        return self._extrakto_all

    @property
    def extrakto_any(self):
        if self._extrakto_any is None:
            self._extrakto_any = Extrakto(alt=False, prefix_name=False)
        return self._extrakto_any

    def prep_cycle(self, keys):
        res = {}
        l = len(keys)
        for i in range(l):
            res[keys[i]] = keys[(i + 1) % l]
        return res

    # -- state shared with the --list/--transform child processes ------------

    def state_path(self):
        return os.path.join(self.state_dir, "state.json")

    def save_state(self):
        tmp = self.state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "trigger_pane": self.trigger_pane,
                    "launch_mode": self.launch_mode,
                    "trigger_path": self.trigger_path,
                    "sel_filter": self.sel_filter,
                    "grab_area": self.grab_area,
                    "clip_mode": self.clip_mode,
                    "generation": self.generation,
                },
                f,
            )
        os.replace(tmp, self.state_path())

    def load_state(self):
        with open(self.state_path(), encoding="utf-8") as f:
            st = json.load(f)
        self.sel_filter = st["sel_filter"]
        self.grab_area = st["grab_area"]
        self.clip_mode = st["clip_mode"]
        self.generation = st.get("generation", 0)

    @classmethod
    def from_state(cls, state_dir):
        with open(os.path.join(state_dir, "state.json"), encoding="utf-8") as f:
            st = json.load(f)
        self = cls(
            st["trigger_pane"],
            st["launch_mode"],
            state_dir,
            st.get("trigger_path"),
        )
        self.load_state()
        return self

    def child_command(self, *args):
        # -S -m, not a script path: a file run as __main__ recompiles this
        # module every time, while -m reuses the cached bytecode, and -S skips
        # site. Together they take ~14 ms off each of the two children a filter
        # keypress spawns. -m needs the module dir importable, hence PYTHONPATH.
        parts = [
            "env",
            f"PYTHONPATH={SCRIPT_DIR}",
            PYTHON,
            "-S",
            "-m",
            MODULE,
            *args,
            self.state_dir,
        ]
        return " ".join(shlex.quote(p) for p in parts)

    # -- capture -------------------------------------------------------------

    def copy(self, text):
        if self.clip_mode == "tmux_osc52":
            subprocess.run(["tmux", "set-buffer", "-w", "--", text], check=True)
            return

        subprocess.run(["tmux", "set-buffer", "--", text], check=True)
        if self.clip_mode == "buffer":
            return

        # feed pbcopy directly. the fg/bg dance existed for xclip, which has to
        # stay alive to own the X selection; pbcopy takes the text and exits, so
        # tmux run-shell -> sh -> tmux show-buffer -> pipe is three wasted hops
        subprocess.run([self.clip_tool], input=text, text=True, check=True)

    def open(self, paths):
        if self.open_tool and paths:
            # run from this process, not tmux run-shell: run-shell inherits the
            # tmux *server's* cwd, while a popup inherits the pane's, so relative
            # paths only resolve correctly here
            subprocess.run([self.open_tool, "--", *paths], check=False)

    def jump(self, pane, socket):
        if socket:
            subprocess.run(
                [
                    "tmux",
                    "display-message",
                    f"extrakto: {pane} lives on tmux server '{socket}', "
                    "cannot jump to it from this client",
                ],
                check=False,
            )
            return
        for args in (
            ["switch-client", "-t", pane],
            ["select-window", "-t", pane],
            ["select-pane", "-t", pane],
        ):
            subprocess.run(["tmux", *args], check=False)

    def get_capture_pane_start(self):
        area = self.grab_area
        for prefix in ("all ", "session ", "window "):
            if area.startswith(prefix):
                area = area[len(prefix):]
                break

        if area == "recent":
            return "-200"
        elif area == "full":
            return "-2000"
        else:
            return f"-{area}"

    def cache_path(self):
        return os.path.join(
            self.state_dir,
            "cap-%s-g%d" % (re.sub(r"\W+", "_", self.grab_area), self.generation),
        )

    def write_preview_script(self):
        path = os.path.join(self.state_dir, "preview.sh")
        with open(path, "w", encoding="utf-8") as f:
            f.write(PREVIEW_SCRIPT)
        os.chmod(path, 0o755)
        return path

    def drop_cache(self):
        try:
            os.remove(self.cache_path())
        except FileNotFoundError:
            pass

    def read_cache(self):
        """Return cached chunks, or None if there is no usable cache."""
        try:
            with open(self.cache_path(), encoding="utf-8") as f:
                data = f.read()
        except FileNotFoundError:
            return None
        head, _sep, body = data.partition("\n")
        try:
            origins = json.loads(head)
        except ValueError:
            return None
        texts = body.split(CHUNK_SEP)
        if len(origins) != len(texts):
            return None  # not ours to trust; capture again
        return [
            (text, pane, socket, cwd)
            for text, (pane, socket, cwd) in zip(texts, origins)
        ]

    def write_cache(self, chunks):
        # metadata and text go in ONE file replaced atomically, so a reader can
        # never pair the text of one capture with the metadata of another. the
        # temp name carries our pid: while the initial capture is still streaming
        # a reload child may be writing this same cache, and a shared temp name
        # would let one process rename the other's half-written file into place.
        tmp = f"{self.cache_path()}.{os.getpid()}.tmp"
        head = json.dumps([[pane, socket, cwd] for _t, pane, socket, cwd in chunks])
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(head + "\n")
            f.write(CHUNK_SEP.join(text for text, _p, _s, _c in chunks))
        os.replace(tmp, self.cache_path())  # atomic; last writer wins, both agree

    def capture_panes(self):
        """Yield (text, pane_id, socket, cwd) for every pane in the grab area."""
        # only the grab key changes what gets captured, so cycling filters must
        # not re-run list-panes/capture-pane across every pane and socket. the
        # cache is on disk because fzf's reload runs in a fresh process.
        cached = self.read_cache()
        if cached is not None:
            yield from cached
            return

        chunks = []
        for chunk in self.capture_panes_uncached():
            chunks.append(chunk)
            yield chunk

        # only cache a complete capture: an early fzf exit abandons this generator
        self.write_cache(chunks)

    def list_socket_panes(self, socket):
        try:
            out = subprocess.check_output(
                [
                    "tmux", "-L", socket, "list-panes", "-a",
                    "-F", "#{pane_id}\t#{pane_current_path}",
                ],
                universal_newlines=True,
                stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            return None  # socket not running
        return [parse_pane_line(line) for line in out.strip().split("\n") if line]

    def trigger_cwd(self):
        # the key binding passes #{pane_current_path} in argv, so the usual case
        # costs nothing. the tmux round trip below is only a fallback for a
        # launch that did not supply it.
        if self.trigger_path:
            return self.trigger_path
        try:
            return subprocess.check_output(
                ["tmux", "display-message", "-p", "-t", self.trigger_pane,
                 "#{pane_current_path}"],
                universal_newlines=True,
            ).strip()
        except subprocess.CalledProcessError:
            return None

    def capture_panes_uncached(self):
        capture_pane_start = self.get_capture_pane_start()

        with ThreadPoolExecutor() as executor:
            # submit the trigger pane before anything else: it is the capture the
            # user is waiting on, and it overlaps the list-panes round trips below
            trigger_future = executor.submit(
                self.capture_pane, self.trigger_pane, capture_pane_start
            )
            trigger_cwd_future = executor.submit(self.trigger_cwd)
            socket_futures = [
                (socket, executor.submit(self.list_socket_panes, socket))
                for socket in self.extra_sockets
                if socket not in self.dead_sockets
            ]

            # collect (pane_id, socket, cwd) tasks for non-trigger panes
            tasks = []
            fmt = "#{pane_id}\t#{pane_current_path}"

            if self.grab_area.startswith(("all ", "session ")):
                scope = "-a" if self.grab_area.startswith("all ") else "-s"
                rows = subprocess.check_output(
                    ["tmux", "list-panes", scope, "-F", fmt],
                    universal_newlines=True,
                ).strip().split("\n")
                tasks += [
                    (pane, None, cwd)
                    for pane, cwd in (parse_pane_line(r) for r in rows if r)
                    if pane != self.trigger_pane
                ]
            elif self.grab_area.startswith("window "):
                rows = subprocess.check_output(
                    ["tmux", "list-panes", "-F", "#{pane_active}\t" + fmt],
                    universal_newlines=True,
                ).strip().split("\n")
                for row in rows:
                    active, _tab, rest = row.partition("\t")
                    pane, cwd = parse_pane_line(rest)
                    if active == "0" and pane != self.trigger_pane:
                        tasks.append((pane, None, cwd))

            for socket, future in socket_futures:
                panes = future.result()
                if panes is None:
                    self.dead_sockets.add(socket)
                else:
                    tasks += [(pane, socket, cwd) for pane, cwd in panes]

            # stream captures: trigger pane first, others in original list order
            other_futures = [
                (
                    pane_id,
                    socket,
                    cwd,
                    executor.submit(
                        self.capture_pane, pane_id, capture_pane_start, socket
                    ),
                )
                for pane_id, socket, cwd in tasks
            ]
            yield (
                self.capture_result(trigger_future),
                self.trigger_pane,
                None,
                self.capture_result(trigger_cwd_future),
            )
            for pane_id, socket, cwd, future in other_futures:
                yield self.capture_result(future), pane_id, socket, cwd

    @staticmethod
    def capture_result(future):
        try:
            return future.result()
        except subprocess.CalledProcessError:
            # the pane was closed while we were capturing it; losing one pane
            # must not take down the whole picker
            return ""

    def capture_pane(self, pane, capture_pane_start, socket=None):
        tmux = ["tmux", "-L", socket] if socket else ["tmux"]
        command = tmux + ["capture-pane", "-pJ", "-S", capture_pane_start, "-t", pane]

        if self.grab_area.endswith("recent"):
            try:
                pane_in_mode, scroll_position, pane_height = [
                    int(n)
                    for n in subprocess.check_output(
                        tmux + [
                            "display-message",
                            "-p",
                            "-t",
                            pane,
                            "#{pane_in_mode}\t#{scroll_position}\t#{pane_height}",
                        ],
                        universal_newlines=True,
                        encoding="utf-8",
                    )
                    .strip()
                    .split("\t")
                ]

                if pane_in_mode == 1:
                    start = int(capture_pane_start) - scroll_position
                    end = (pane_height - 1) - scroll_position
                    command = tmux + [
                        "capture-pane", "-pJ",
                        "-S", str(start),
                        "-E", str(end),
                        "-t", pane,
                    ]
            except (ValueError, subprocess.CalledProcessError):
                pass

        return subprocess.check_output(
            command,
            universal_newlines=True,
            encoding="utf-8",
        )

    def has_single_pane(self):
        # split() rather than split("\n"): the trailing newline used to add a
        # phantom pane, so the popup branch could never be true
        num_panes = len(
            subprocess.check_output(
                ["tmux", "list-panes", "-F", "#{pane_id}"], universal_newlines=True
            ).split()
        )
        if self.launch_mode == "popup":
            return num_panes == 1
        else:
            return num_panes == 2  # the extrakto split is one of them

    def next_grab_area(self):
        grab_cycle = ["recent"]
        if not self.has_single_pane():
            grab_cycle.append("window recent")
        grab_cycle.extend(["session recent", "all recent", "full"])
        if not self.has_single_pane():
            grab_cycle.append("window full")
        grab_cycle.extend(["session full", "all full"])
        if not self.original_grab_area.startswith(
            ("window ", "session ", "all ", "recent", "full")
        ):
            grab_cycle.append(self.original_grab_area)

        try:
            idx = grab_cycle.index(self.grab_area)
            return grab_cycle[(idx + 1) % len(grab_cycle)]
        except ValueError:
            return "recent"

    # -- header --------------------------------------------------------------

    def build_header(self):
        parts = []
        for o in self.fzf_header.split(" "):
            if not o:
                continue
            if o == "i":
                parts.append(f"{COLORS['BOLD']}{self.insert_key}{COLORS['OFF']}=insert")
            elif o == "c":
                parts.append(f"{COLORS['BOLD']}{self.copy_key}{COLORS['OFF']}=copy")
            elif o == "o":
                if self.open_tool:
                    parts.append(f"{COLORS['BOLD']}{self.open_key}{COLORS['OFF']}=open")
            elif o == "e":
                parts.append(f"{COLORS['BOLD']}{self.edit_key}{COLORS['OFF']}=edit")
            elif o == "q":
                parts.append(f"{COLORS['BOLD']}{self.quote_key}{COLORS['OFF']}=quote")
            elif o == "s":
                parts.append(f"{COLORS['BOLD']}{self.squote_key}{COLORS['OFF']}=squote")
            elif o == "p":
                parts.append(f"{COLORS['BOLD']}{self.path_key}{COLORS['OFF']}=path")
            elif o == "l":
                parts.append(f"{COLORS['BOLD']}{self.line_key}{COLORS['OFF']}=line")
            elif o == "f":
                parts.append(
                    f"{COLORS['BOLD']}{self.filter_key}{COLORS['OFF']}=filter "
                    f"[{COLORS['YELLOW']}{COLORS['BOLD']}{self.sel_filter}{COLORS['OFF']}]"
                )
            elif o == "g":
                parts.append(
                    f"{COLORS['BOLD']}{self.grab_key}{COLORS['OFF']}=grab "
                    f"[{COLORS['YELLOW']}{COLORS['BOLD']}{self.grab_area}{COLORS['OFF']}]"
                )
            elif o == "m":
                parts.append(
                    f"{COLORS['BOLD']}{self.clip_mode_key}{COLORS['OFF']}=clip "
                    f"[{COLORS['YELLOW']}{COLORS['BOLD']}{self.clip_mode}{COLORS['OFF']}]"
                )
            elif o == "r":
                parts.append(
                    f"{COLORS['BOLD']}{self.refresh_key}{COLORS['OFF']}=refresh"
                )
            elif o == "j":
                parts.append(f"{COLORS['BOLD']}{self.jump_key}{COLORS['OFF']}=jump")
            elif o == "h":
                continue
            else:
                parts.append("(config error)")

        header = ", ".join(parts).replace("ctrl-", "^")
        # parens would terminate the change-header(...) action early
        return header.replace("(", "[").replace(")", "]")

    # -- child modes ---------------------------------------------------------

    def emit_tokens(self):
        """Print the token list for the current state (fzf reload target)."""
        out = sys.stdout
        try:
            for item in get_cap(
                self.sel_filter,
                self.capture_panes(),
                extrakto=self.extrakto_for(self.sel_filter),
            ):
                out.write(item)
                out.write("\n")
            out.flush()
        except BrokenPipeError:
            # fzf went away mid-reload; a traceback here would land in the popup.
            # dup /dev/null over stdout so interpreter shutdown cannot re-raise
            # while flushing the already-dead pipe.
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())

    def transform(self, action):
        """Mutate state and print the fzf actions to apply (fzf transform target)."""
        needs_reload = True
        if action == "filter":
            self.sel_filter = self.next_filter[self.sel_filter]
        elif action == "grab":
            self.grab_area = self.next_grab_area()
        elif action == "clip":
            self.clip_mode = self.next_clip_mode[self.clip_mode]
            needs_reload = False  # only the header changes
        elif action == "refresh":
            # re-capture with the query and filter left alone. bumping the
            # generation (rather than only deleting) means a capture still
            # streaming from before the refresh writes to a filename nobody
            # reads, instead of landing on top of the fresh one later.
            self.drop_cache()
            self.generation += 1
        else:
            self.sel_filter = action  # jump straight to a named filter

        self.save_state()

        actions = []
        if needs_reload:
            actions.append(f"reload-sync({self.child_command('--list')})")
        actions.append(f"change-header({self.build_header()})")
        sys.stdout.write("+".join(actions))

    # -- main ----------------------------------------------------------------

    def editor_command(self, located):
        args = []
        line = None
        for token, cwd in located:
            path, ln = split_path_line(token, cwd)
            if ln and line is None:
                line = ln
            args.append(shlex.quote(resolve_path(path, cwd)))
        cmd = self.editor
        if line:
            cmd += f" +{line}"
        return cmd + " -- " + " ".join(args)

    def capture(self):
        if self.launch_mode != "popup":
            lines = os.get_terminal_size().lines
            if lines < 7:
                subprocess.run("tmux resize-pane -Z", shell=True)

        self.save_state()

        # keys that end the session; everything else is handled inside fzf so it
        # never has to be torn down and relaunched
        expect_keys = list(
            OrderedDict.fromkeys(
                key
                for key in [
                    "ctrl-c",
                    "esc",
                    self.insert_key,
                    self.copy_key,
                    self.edit_key,
                    self.open_key,
                    self.jump_key,
                ]
                if key
            )
        )

        binds = [
            (self.filter_key, "filter"),
            (self.grab_key, "grab"),
            (self.clip_mode_key, "clip"),
            (self.refresh_key, "refresh"),
            (self.line_key, "line"),
            (self.path_key, "path"),
            (self.quote_key, "quote"),
            (self.squote_key, "s-quote"),
        ]

        fzf_cmd = []
        try:
            fzf_cmd = [
                self.fzf_tool,
                "--multi",
                "--print-query",
                f"--header={self.build_header()}",
                f"--expect={','.join(expect_keys)}",
                "--tiebreak=index",
                # candidates carry their origin in fields 2/3; display and match
                # on field 1 only, but fzf still returns the whole line to us
                f"--delimiter={META_SEP}",
                "--with-nth=1",
                # identify items by the token alone, so multi-select marks survive
                # a ctrl-r refresh (the pane id in field 2 may well have changed)
                "--id-nth=1",
                f"--layout={self.fzf_layout}",
                "--no-info",
                "--color", "fg:#D8DEE9,bg:#2E3440,hl:#A3BE8C,fg+:#D8DEE9,bg+:#434C5E,hl+:#A3BE8C",
                "--color", "pointer:#BF616A,info:#4C566A,spinner:#4C566A,header:#4C566A,prompt:#81A1C1,marker:#EBCB8B",
                "--border=none",
                "--height=100%",
                "--preview-window=top:30%",
                f"--preview={shlex.quote(self.write_preview_script())} {{1}} {{4}}",
            ]
            for key, action in binds:
                fzf_cmd += [
                    "--bind",
                    f"{key}:transform({self.child_command('--transform', action)})",
                ]

            res = fzf_sel(
                fzf_cmd,
                get_cap_batches(
                    self.sel_filter,
                    self.capture_panes(),
                    extrakto=self.extrakto_for(self.sel_filter),
                ),
            )
        except Exception:
            import traceback

            msg = (
                str(fzf_cmd)
                + "\n"
                + traceback.format_exc()
                + "\n"
                + "error: unable to extract - check/report errors above"
                + "\n"
                + "If fzf is not found you need to set the fzf path in options (see readme)."
            )
            print(msg)
            confirm = input("Copy this message to the clipboard? [Y/n]")
            if confirm != "n":
                self.copy(msg)
            sys.exit(0)

        # with --print-query/--expect fzf emits a query line and a key line even
        # on abort; anything shorter means it was killed, which is not an error
        if len(res) < 2:
            return 0
        _query, key, *selection = res

        # the transform children own the filter/clip state now
        self.load_state()

        origins = [split_meta(s) for s in selection]
        selection = [token for token, _pane, _socket, _cwd in origins]

        if (
            self.prefix_name == "all" and self.sel_filter == "all"
        ) or self.prefix_name == "any":
            selection = [next(iter(s.split(": ", 1)[1:2]), s) for s in selection]

        # pair each (possibly un-prefixed) token back up with the cwd of the pane
        # it came from, so edit/open can resolve relative paths
        located = list(zip(selection, [cwd for *_head, cwd in origins]))

        if self.sel_filter in ("all", "line"):
            text = "\n".join(selection)
        else:
            text = " ".join(selection)

        if key == self.copy_key:
            self.copy(text)
        elif key == self.insert_key:
            subprocess.run(["tmux", "set-buffer", "--", text], check=True)
            subprocess.run(
                ["tmux", "paste-buffer", "-p", "-t", self.trigger_pane], check=True
            )
        elif key == self.open_key:
            self.open(
                [
                    resolve_path(split_path_line(tok, cwd)[0], cwd)
                    for tok, cwd in located
                ]
            )
        elif key == self.jump_key:
            if origins:
                _token, pane, socket, _cwd = origins[0]
                if pane:
                    self.jump(pane, socket)
        elif key == self.edit_key:
            subprocess.run(
                [
                    "tmux",
                    "if-shell",
                    "-t",
                    self.trigger_pane,
                    "-F",
                    "#{pane_in_mode}",
                    f"send-keys -t {self.trigger_pane} -X cancel",
                    ";",
                    "send-keys",
                    "-t",
                    self.trigger_pane,
                    self.editor_command(located),
                    "C-m",
                ],
                check=True,
            )

        return 0


def main(argv):
    if len(argv) >= 3 and argv[1] == "--list":
        ExtraktoPlugin.from_state(argv[2]).emit_tokens()
        return 0

    if len(argv) >= 4 and argv[1] == "--transform":
        ExtraktoPlugin.from_state(argv[3]).transform(argv[2])
        return 0

    if len(argv) < 3:
        print("Usage: extrakto_plugin.py trigger_pane launch_mode [trigger_path]")
        return 1

    trigger_path = argv[3] if len(argv) > 3 else None
    state_dir = tempfile.mkdtemp(prefix="extrakto-")
    try:
        return ExtraktoPlugin(argv[1], argv[2], state_dir, trigger_path).capture()
    finally:
        import shutil

        shutil.rmtree(state_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
