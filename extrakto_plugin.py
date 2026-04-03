#!/usr/bin/env python3

import os
import subprocess
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict

from extrakto import Extrakto, get_lines

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


def fzf_sel(command, lines):
    p = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None
    )
    assert p.stdin is not None and p.stdout is not None
    try:
        for line in lines:
            p.stdin.write(line.encode("utf-8") + b"\n")
            p.stdin.flush()
    except BrokenPipeError:
        pass
    p.stdin.close()
    p.wait()
    res = p.stdout.read().decode("utf-8").split("\n")
    # omit last empty line
    return res[:-1]


def get_cap(sel_filter, chunks, *, extrakto_all, extrakto_any):
    seen = set()
    any_match = False

    for data in chunks:
        if sel_filter == "line":
            res = get_lines(data)
        elif sel_filter == "all":
            res = []
            for name in extrakto_all.all():
                res += extrakto_all[name].filter(data)
        else:
            res = extrakto_any[sel_filter].filter(data)

        for item in reversed(res):
            if item not in seen:
                seen.add(item)
                yield item
                any_match = True

    if not any_match:
        yield "NO MATCH - use a different filter"


class ExtraktoPlugin:

    def __init__(self, trigger_pane, launch_mode):
        self.trigger_pane = trigger_pane
        self.launch_mode = launch_mode

        self.clip_tool = "pbcopy"
        self.clip_mode = "bg"
        self.clip_mode_key = "ctrl-t"
        self.copy_key = "tab"
        self.edit_key = "ctrl-e"
        self.editor = os.environ.get("EDITOR", "vi")
        self.filter_key = "ctrl-f"
        self.fzf_header = "i c o e q s p l f g"
        self.fzf_layout = "reverse"
        self.fzf_tool = "fzf"
        self.grab_area = "all full"
        self.grab_key = "ctrl-g"
        self.insert_key = "enter"
        self.line_key = "ctrl-l"
        self.open_key = "ctrl-o"
        self.open_tool = "open"
        self.path_key = "ctrl-p"
        self.quote_key = "ctrl-q"
        self.squote_key = "ctrl-s"
        self.alt = "all"
        self.prefix_name = "all"
        self.extra_sockets = ["tokyo", "seafoam"]

        self.extrakto_all = Extrakto(alt=True, prefix_name=True)
        self.extrakto_any = Extrakto(alt=False, prefix_name=False)

        self.original_grab_area = self.grab_area

        filter_order = "word path quote s-quote url line all".split()
        self.next_filter = self.prep_cycle(filter_order)
        self.next_filter["initial"] = (
            os.environ.get("extrakto_inital_mode", "").strip() or filter_order[0]
        )

        clip_mode_order = "bg buffer".split()
        self.next_clip_mode = self.prep_cycle(clip_mode_order)

        os.environ.pop("FZF_DEFAULT_OPTS", None)
        os.environ.pop("FZF_DEFAULT_OPTS_FILE", None)

        if launch_mode != "popup":
            lines = os.get_terminal_size().lines
            if lines < 7:
                subprocess.run("tmux resize-pane -Z", shell=True)

    def prep_cycle(self, keys):
        res = {}
        l = len(keys)
        for i in range(l):
            res[keys[i]] = keys[(i + 1) % l]
        return res

    def copy(self, text):
        if self.clip_mode == "fg":
            subprocess.run(["tmux", "set-buffer", "--", text], check=True)
            subprocess.run(
                ["tmux", "run-shell", f"tmux show-buffer|{self.clip_tool}"], check=True
            )
        elif self.clip_mode == "tmux_osc52":
            subprocess.run(["tmux", "set-buffer", "-w", "--", text], check=True)
        elif self.clip_mode == "buffer":
            subprocess.run(["tmux", "set-buffer", "--", text], check=True)
        else:
            # run in background as xclip won't work otherwise
            subprocess.run(["tmux", "set-buffer", "--", text], check=True)
            subprocess.run(
                ["tmux", "run-shell", "-b", f"tmux show-buffer|{self.clip_tool}"],
                check=True,
            )

    def open(self, path):
        if self.open_tool:
            subprocess.run(
                ["tmux", "run-shell", "-b", f"cd -- $PWD; {self.open_tool} {path}"],
                check=True,
            )

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

    def capture_panes(self):
        capture_pane_start = self.get_capture_pane_start()

        # collect (pane_id, socket) tasks for non-trigger panes
        tasks = []

        if self.grab_area.startswith("all "):
            panes = subprocess.check_output(
                ["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
                universal_newlines=True,
            ).strip().split("\n")
            tasks += [(p, None) for p in panes if p and p != self.trigger_pane]
        elif self.grab_area.startswith("session "):
            panes = subprocess.check_output(
                ["tmux", "list-panes", "-s", "-F", "#{pane_id}"],
                universal_newlines=True,
            ).strip().split("\n")
            tasks += [(p, None) for p in panes if p and p != self.trigger_pane]
        elif self.grab_area.startswith("window "):
            panes = subprocess.check_output(
                ["tmux", "list-panes", "-F", "#{pane_active}:#{pane_id}"],
                universal_newlines=True,
            ).split("\n")
            tasks += [
                (p[2:], None) for p in panes
                if p.startswith("0:") and p[2:] != self.trigger_pane
            ]

        for socket in self.extra_sockets:
            try:
                panes = subprocess.check_output(
                    ["tmux", "-L", socket, "list-panes", "-a", "-F", "#{pane_id}"],
                    universal_newlines=True,
                    stderr=subprocess.DEVNULL,
                ).strip().split("\n")
                tasks += [(p, socket) for p in panes if p]
            except subprocess.CalledProcessError:
                pass  # socket not running, skip silently

        # stream captures: trigger pane first, others in original list order
        with ThreadPoolExecutor() as executor:
            trigger_future = executor.submit(
                self.capture_pane, self.trigger_pane, capture_pane_start
            )
            other_futures = [
                executor.submit(self.capture_pane, pane_id, capture_pane_start, socket)
                for pane_id, socket in tasks
            ]
            yield trigger_future.result()
            for future in other_futures:
                yield future.result()

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
        num_panes = len(
            subprocess.check_output(
                ["tmux", "list-panes"], universal_newlines=True
            ).split("\n")
        )
        if self.launch_mode == "popup":
            return num_panes == 1
        else:
            return num_panes == 2

    def capture(self):
        sel_filter = self.next_filter["initial"]
        header_parts = []
        for o in self.fzf_header.split(" "):
            if not o:
                continue
            if o == "i":
                header_parts.append(
                    f"{COLORS['BOLD']}{self.insert_key}{COLORS['OFF']}=insert"
                )
            elif o == "c":
                header_parts.append(
                    f"{COLORS['BOLD']}{self.copy_key}{COLORS['OFF']}=copy"
                )
            elif o == "o":
                if self.open_tool:
                    header_parts.append(
                        f"{COLORS['BOLD']}{self.open_key}{COLORS['OFF']}=open"
                    )
            elif o == "e":
                header_parts.append(
                    f"{COLORS['BOLD']}{self.edit_key}{COLORS['OFF']}=edit"
                )
            elif o == "q":
                header_parts.append(
                    f"{COLORS['BOLD']}{self.quote_key}{COLORS['OFF']}=quote"
                )
            elif o == "s":
                header_parts.append(
                    f"{COLORS['BOLD']}{self.squote_key}{COLORS['OFF']}=squote"
                )
            elif o == "p":
                header_parts.append(
                    f"{COLORS['BOLD']}{self.path_key}{COLORS['OFF']}=path"
                )
            elif o == "l":
                header_parts.append(
                    f"{COLORS['BOLD']}{self.line_key}{COLORS['OFF']}=line"
                )
            elif o == "f":
                header_parts.append(
                    f"{COLORS['BOLD']}{self.filter_key}{COLORS['OFF']}=filter [{COLORS['YELLOW']}{COLORS['BOLD']}:filter:{COLORS['OFF']}]"
                )
            elif o == "g":
                header_parts.append(
                    f"{COLORS['BOLD']}{self.grab_key}{COLORS['OFF']}=grab [{COLORS['YELLOW']}{COLORS['BOLD']}:ga:{COLORS['OFF']}]"
                )
            elif o == "m":
                header_parts.append(
                    f"{COLORS['BOLD']}{self.clip_mode_key}{COLORS['OFF']}=clip [{COLORS['YELLOW']}{COLORS['BOLD']}:clip_mode:{COLORS['OFF']}]"
                )
            elif o == "h":
                continue
            else:
                header_parts.append("(config error)")

        header_tmpl = ", ".join(header_parts)
        expect_keys = list(
            OrderedDict.fromkeys(
                key
                for key in [
                    "ctrl-c",
                    "ctrl-g",
                    "esc",
                    self.insert_key,
                    self.copy_key,
                    self.filter_key,
                    self.edit_key,
                    self.quote_key,
                    self.squote_key,
                    self.path_key,
                    self.line_key,
                    self.open_key,
                    self.grab_key,
                    self.clip_mode_key,
                ]
                if key
            )
        )

        query = ""
        while True:
            header = (
                header_tmpl.replace(":ga:", self.grab_area)
                .replace(":filter:", sel_filter)
                .replace(":clip_mode:", self.clip_mode)
                .replace("ctrl-", "^")
            )

            # for troubleshooting add `tee /tmp/stageN | ` between the commands
            fzf_cmd = []
            try:
                fzf_cmd = [
                    self.fzf_tool,
                    "--multi",
                    "--print-query",
                    f"--query={query}",
                    f"--header={header}",
                    f"--expect={','.join(expect_keys)}",
                    "--tiebreak=index",
                    f"--layout={self.fzf_layout}",
                    "--no-info",
                    "--color", "fg:#D8DEE9,bg:#2E3440,hl:#A3BE8C,fg+:#D8DEE9,bg+:#434C5E,hl+:#A3BE8C",
                    "--color", "pointer:#BF616A,info:#4C566A,spinner:#4C566A,header:#4C566A,prompt:#81A1C1,marker:#EBCB8B",
                    "--border=none",
                    "--height=100%",
                    "--preview-window=top:30%",
                    "--preview=if [[ -d {} ]]; then exa --color always -T {}; elif [[ -f {} ]]; then bat --color always --paging never {}; else echo {}; fi",
                ]
                query, key, *selection = fzf_sel(
                    fzf_cmd,
                    get_cap(sel_filter, self.capture_panes(),
                           extrakto_all=self.extrakto_all,
                           extrakto_any=self.extrakto_any),
                )
            except Exception:
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

            text = ""
            if (
                self.prefix_name == "all" and sel_filter == "all"
            ) or self.prefix_name == "any":
                selection = [next(iter(s.split(": ", 1)[1:2]), s) for s in selection]

            if sel_filter in ("all", "line"):
                text = "\n".join(selection)
            else:
                text = " ".join(selection)

            if key == self.copy_key:
                self.copy(text)
                return 0
            elif key == self.insert_key:
                subprocess.run(["tmux", "set-buffer", "--", text], check=True)
                subprocess.run(
                    ["tmux", "paste-buffer", "-p", "-t", self.trigger_pane], check=True
                )
                return 0
            elif key == self.filter_key:
                sel_filter = self.next_filter[sel_filter]
            elif key == self.quote_key:
                sel_filter = "quote"
            elif key == self.squote_key:
                sel_filter = "s-quote"
            elif key == self.path_key:
                sel_filter = "path"
            elif key == self.line_key:
                sel_filter = "line"
            elif key == self.clip_mode_key:
                self.clip_mode = self.next_clip_mode[self.clip_mode]
            elif key == self.grab_key:
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
                    self.grab_area = grab_cycle[(idx + 1) % len(grab_cycle)]
                except ValueError:
                    self.grab_area = "recent"
            elif key == self.open_key:
                self.open(text)
                return 0
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
                        f"{self.editor} -- {text}",
                        "C-m",
                    ],
                    check=True,
                )
                return 0
            else:
                return 0

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: extrakto-plugin.py trigger_pane launch_mode")
        sys.exit(1)
    else:
        ExtraktoPlugin(sys.argv[1], sys.argv[2]).capture()
