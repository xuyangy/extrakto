# extrakto for tmux (personal fork)

A personal, macOS-only fork of [laktak/extrakto](https://github.com/laktak/extrakto/).

**Output completions** — complete commands using text that is already on the screen,
without retyping it. Works everywhere, including remote ssh sessions.

- press tmux `prefix + space` to start extrakto
- fuzzy find the text/path/url/line
- press `enter` to insert it into the pane, `tab` to copy it to the clipboard

Use it for paths, URLs, options from a man page, git hashes, docker container
names, grep hits, ...

> **This fork is not configurable.** Upstream reads ~20 `@extrakto_*` tmux options
> at startup; that was removed for launch latency. Everything below is hardcoded in
> `extrakto_plugin.py` and `scripts/open.sh` — to change behaviour, edit the source.
> Filters are the exception: those are still read from `extrakto.conf`.

- [Keys](#keys)
- [Filters](#filters)
- [Behaviour](#behaviour)
- [How it works](#how-it-works)
- [Custom filters](#custom-filters)
- [CLI tool](#cli-tool)

## Keys

| Key      | Action |
| :---     | :--- |
| `enter`  | insert the selection into the pane you came from |
| `tab`    | copy the selection to the clipboard |
| `ctrl-f` | next filter mode |
| `ctrl-l` | jump to the *line* filter |
| `ctrl-p` | jump to the *path* filter |
| `ctrl-q` | jump to the *quote* filter |
| `ctrl-s` | jump to the *s-quote* filter |
| `ctrl-g` | cycle the grab area |
| `ctrl-t` | cycle the clipboard mode (`bg` → `buffer`) |
| `ctrl-r` | re-capture the panes, keeping the current query and filter |
| `ctrl-j` | jump to the pane the selection came from |
| `ctrl-o` | pass the selection to `open` |
| `ctrl-e` | open the selection in `$EDITOR` |
| `esc` / `ctrl-c` | cancel |

Note `enter`/`tab` are swapped relative to upstream. Use `shift-tab` to select
multiple entries.

`ctrl-e` understands `file:line` and `file:line:col` locations: if the file part
exists it runs `$EDITOR +42 -- file`. `ctrl-o` strips the `:line` suffix.

## Filters

`ctrl-f` cycles: `word` → `path` → `path-line` → `quote` → `s-quote` → `url` →
`line` → `all`.

| Filter      | Matches |
| :---        | :--- |
| `word`      | anything not whitespace/brackets (the default) |
| `path`      | file and directory paths |
| `path-line` | grep/compiler locations: `src/main.py:42`, `src/main.py:42:13`, `Makefile:12` |
| `url`       | http, git, ssh, ftp, file URLs |
| `quote`     | `"double quoted"` strings |
| `s-quote`   | `'single quoted'` strings |
| `line`      | whole lines |
| `all`       | every filter with `in_all` at once, each result prefixed with its filter name |

`~/.config/extrakto/extrakto.conf` currently adds `emails` and `ips`, and raises
`min_length` to 10 for `word` and 15 for `path`/`url`. The `path` limit is high
enough to drop short paths like `src/main.py` — lower it there if that bites.

## Behaviour

| | |
| :--- | :--- |
| launcher | `tmux popup`, 60% × 60%, centred |
| grab area at startup | `all full` — every pane on every server below |
| tmux servers scanned | default, plus the `tokyo` and `seafoam` sockets |
| `recent` | last 200 lines per pane |
| `full` | last 2000 lines per pane |
| clipboard | `/usr/bin/pbcopy`, fed directly |
| open | `/usr/bin/open`, invoked from the plugin process |
| fzf | `/usr/local/bin/fzf` |
| preview | `eza` for directories, `bat` for files |
| python | `/Users/xuyangy/.pyenv/versions/3.13.12/bin/python3` (pyenv 3.13.12), pinned in `scripts/open.sh`. The old anaconda pin broke when that pyenv env was deleted (exit 127). |

`ctrl-g` cycles the grab area through `recent`, `window recent`, `session recent`,
`all recent`, `full`, `window full`, `session full`, `all full`. The `window`
entries are skipped when the window has only one pane.

Panes that are closed while being captured are skipped rather than aborting the
picker, and tmux servers that are not running are probed once and then ignored.

## How it works

- `extrakto.tmux` binds `prefix + space` to `scripts/open.sh`.
- `scripts/open.sh` opens a tmux popup running `extrakto_plugin.py <pane> popup`.
- The plugin captures panes in a thread pool — the pane you started from first, so
  it streams into fzf while the rest are still being captured — and pipes
  candidates in.
- **fzf is launched once.** Filter, grab, clip-mode and refresh keys are fzf
  `transform` bindings that re-invoke the plugin as `--transform`; it mutates the
  shared state and prints `reload-sync(...)` / `change-header(...)` back to fzf.
  Your query survives, and nothing is re-captured on a filter change.
- Items are identified by their token (`--id-nth=1`), so multi-select marks are
  carried across a reload for candidates that still exist afterwards — which in
  practice means `ctrl-r`. A filter change produces a different set of tokens, so
  there is nothing for the marks to attach to and they go away.
- Captures are cached per grab area in a temp state dir, so only `ctrl-g` and
  `ctrl-r` cause new `capture-pane` calls. Metadata and text live in one file that
  is replaced atomically under a pid-tagged temp name, because a reload child can
  be writing the same cache while the first capture is still streaming. The dir is
  removed on exit.
- Each candidate is emitted as `token<US>pane_id<US>socket<US>cwd`; fzf displays and
  matches field 1 only, but returns the whole line. `ctrl-j` uses the pane, and
  `ctrl-e`/`ctrl-o` resolve relative paths against the cwd of the pane the text was
  captured from — not the directory extrakto happens to be running in.

## Custom filters

Define your own in `~/.config/extrakto/extrakto.conf`:

```ini
[quote]
regex: ("[^"\n\r]+")
```

To override a built-in filter, copy it there first. To remove an alternate
filter, set it to `None`:

```ini
[quote]
alt2: None
```

Note every filter regex is compiled with `re.I`; use `(?-i:...)` where case
matters. See [extrakto.conf](extrakto.conf) for the syntax and the predefined
filters.

## CLI tool

`extrakto.py` also works standalone as a token extractor:

```
git clone https://github.com/laktak/extrakto
cd extrakto
ln -s $PWD/extrakto.py ~/.local/bin/extrakto
```

```
usage: extrakto.py [-h] [--name] [-w] [-l] [--all] [-a ADD] [-p] [-u] [--alt] [-r] [-m MIN_LENGTH] [--warn-empty]

Extracts tokens from plaintext.

optional arguments:
  -h, --help            show this help message and exit
  --name                prefix filter name in the output
  -w, --words           extract "word" tokens
  -l, --lines           extract lines
  --all                 extract using all filters defined in extrakto.conf
  -a ADD, --add ADD     add custom filter
  -p, --paths           short for -a=path
  -u, --urls            short for -a=url
  --alt                 return alternate variants for each match (e.g. https://example.com and example.com)
  -r, --reverse         reverse output
  -m MIN_LENGTH, --min-length MIN_LENGTH
                        minimum token length
  --warn-empty          warn if result is empty
```

Note it reads `~/.config/extrakto/extrakto.conf` too, which is why the test suite
is sensitive to what is set there.

## Upstream

Original project by laktak, MIT licensed — see [LICENSE](LICENSE).
Issues and PRs belong upstream:
[github.com/laktak/extrakto](https://github.com/laktak/extrakto/) ·
[codeberg.org/laktak/extrakto](https://codeberg.org/laktak/extrakto)
