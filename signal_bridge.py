"""
signal_bridge.py -- Poezio plugin that turns Poezio into a Signal client
by connecting to a signal-cli JSON-RPC daemon over its UNIX socket.

Port of the Profanity plugin of the same name. Targets poezio 0.18
(git); version drift in the tab/timer APIs is reported in the info
window instead of crashing the plugin.

Installation: copy to ~/.local/share/poezio/plugins/ and run:
    /load signal_bridge
"""

import json
import os
import queue
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
import uuid

from slixmpp import JID

from poezio import tabs as poezio_tabs
from poezio import timed_events
from poezio.plugin import BasePlugin

try:
    from poezio.ui.types import InfoMessage  # poezio >= 0.13 message objects
except Exception:
    InfoMessage = None

DEFAULT_SOCKET_PATH = os.path.expanduser("~/.local/state/signal-cli/socket")
DEFAULT_ATTACHMENTS_DIR = os.path.expanduser("~/.local/share/signal-cli/attachments")

# Fake domain for Signal tabs. ".invalid" can never resolve or be routed,
# and the input hooks below make sure nothing (not even chatstates) is
# ever sent towards it anyway.
SIGNAL_DOMAIN = "signal.invalid"

# Max size of the chafa ASCII preview (width x height in terminal cells).
ASCII_SIZE = "100x80"

CHAFA = shutil.which("chafa")

SUBCOMMANDS = ["open", "send", "socket", "account", "attachments",
               "colors", "colortest", "status", "reconnect"]

USAGE = ("<open <number> | send <number> <message> | socket <path> | "
         "account <account> | attachments <path> | colors <none|16|256> | "
         "colortest | status | reconnect>")

HELP = ("Bridge to a signal-cli JSON-RPC daemon socket, for sending and "
        "receiving Signal messages from poezio. Incoming image attachments "
        "are shown as ASCII art previews.")


def _poezio_version():
    try:
        import poezio
        version = getattr(poezio, "__version__", None)
        if version:
            return str(version)
        from poezio.version import __version__ as v  # older layouts
        return str(v)
    except Exception:
        return "unknown"


def backup_image_to_ascii(path):
    """Pure function: no poezio calls, no state, safe from any thread.
    Returns None on any failure instead of raising."""
    if not CHAFA:
        return None
    try:
        command = [
            CHAFA,
            "--format=symbols",
            "--symbols=block+border+space",
            "--colors=256",
            "-s", ASCII_SIZE,
            path,
        ]
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            timeout=5,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip("\r\n") or None
    except Exception:
        return None


_ANSI_CSI_RE = re.compile(r'\x1b\[([0-9;]*)m')

# ---------------------------------------------------------------------------
# Colored ASCII previews.
#
# Poezio does not interpret ANSI escapes; message colors are driven by its
# own inline markup, built around FORMAT_CHAR ("\x19"):
#
#     \x19o      reset attributes          (format_chars.reset)
#     \x19b      bold                      (format_chars.bold)
#     \x19<n>}   foreground color n        (n: 0-255, closed by '}')
#
# The '}' is required: it terminates the (possibly multi-digit) color
# number. A bare "\x19<n>" is NOT a valid code, and an invalid \x19 byte
# reaching the renderer is what corrupted the display before.
# ---------------------------------------------------------------------------

DEFAULT_COLORS = "256"          # "none", "16" or "256"

try:
    from poezio.theming import FORMAT_CHAR as _FC
    FORMAT_CHAR = _FC if (isinstance(_FC, str) and _FC) else "\x19"
except ImportError:
    FORMAT_CHAR = "\x19"

try:
    from poezio.theming import format_chars as _poefmt
except ImportError:
    _poefmt = None

RESET = getattr(_poefmt, "reset", FORMAT_CHAR + "o")
BOLD = getattr(_poefmt, "bold", FORMAT_CHAR + "b")

_SGR_RE = re.compile(r"\x1b\[([0-9;:<=>?]*)([\x40-\x7e])")
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# Control chars, ESC included; \x19, \t and \n spared (markup mode).
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x18\x1a-\x1f\x7f]")
# All control chars, \x19 included (plain mode).
_CTRL_STRICT_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _fmt(n):
    return "%s%d}" % (FORMAT_CHAR, n)


def _sanitize(text, markup=False):
    """Last line of defense: whatever reaches a tab must contain no raw
    control bytes. In plain mode \x19 is stripped too, so a markup bug
    can never wedge the renderer again."""
    text = _OSC_RE.sub("", text)
    return (_CTRL_RE if markup else _CTRL_STRICT_RE).sub("", text)


def _rgb_to_256(r, g, b):
    """Truecolor -> xterm256 fallback (only used if chafa ever emits
    38;2 sequences)."""
    r, g, b = max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b))
    if max(r, g, b) - min(r, g, b) < 10 and r >= 8:
        v = round((r - 8) / 10.0)
        if 0 <= v <= 23:
            return 232 + v
    return (16 + 36 * int(round(r / 51.0))
                + 6 * int(round(g / 51.0))
                + int(round(b / 51.0)))


def _convert_ansi_line(line):
    """SGR -> poezio markup for one line. Color state is not carried
    across lines; each art line becomes its own message anyway."""
    out = []
    cur = None
    pos = 0
    for match in _SGR_RE.finditer(line):
        out.append(line[pos:match.start()])
        pos = match.end()
        if match.group(2) != "m":
            continue
        codes = match.group(1).split(";") if match.group(1) else [""]
        i = 0
        while i < len(codes):
            try:
                n = int(codes[i] or "0")
            except ValueError:
                i += 1
                continue
            if n in (0, 39):
                cur = None
            elif n in (38, 48):               # 48 (bg): parsed, then dropped
                col, used = None, 0
                if i + 2 < len(codes) and codes[i + 1] == "5":
                    used = 2
                    try:
                        col = int(codes[i + 2])
                    except ValueError:
                        col = None
                elif i + 4 < len(codes) and codes[i + 1] == "2":
                    used = 4
                    try:
                        col = _rgb_to_256(int(codes[i + 2]),
                                          int(codes[i + 3]),
                                          int(codes[i + 4]))
                    except ValueError:
                        col = None
                i += used
                if col is not None and n == 38:
                    col = max(0, min(255, col))
                    if col != cur:
                        out.append(_fmt(col))
                        cur = col
            elif 30 <= n <= 37 or 90 <= n <= 97:
                col = (n - 30) if n < 90 else (n - 90 + 8)
                if col != cur:
                    out.append(_fmt(col))
                    cur = col
            i += 1
    out.append(line[pos:])
    return "".join(out)


def _ansi_to_poezio(text):
    if "\x1b" not in text:
        return text
    text = _OSC_RE.sub("", text)
    return "\n".join(
        (RESET + _convert_ansi_line(line) + RESET) if line else line
        for line in text.split("\n"))


def _image_to_ascii(path, colors=DEFAULT_COLORS):
    """Pure function: no poezio calls, no state, safe from any thread.
    Returns None on any failure instead of raising."""
    if not CHAFA:
        return None

    base = [CHAFA, "--format=symbols", "--symbols=block+border+space",
            "-s", ASCII_SIZE]
    if colors == "none":
        command = base + ["--colors=none", path]
    else:
        command = base + ["--colors=" + colors, "--fg-only", path]

    try:
        result = subprocess.run(command, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", timeout=5)
        if colors != "none" and result.returncode != 0:
            # chafa too old for --fg-only: retry with backgrounds enabled
            # (slightly flatter art, still colored).
            command = base + ["--colors=" + colors, path]
            result = subprocess.run(command, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True,
                                    encoding="utf-8", timeout=5)
        if result.returncode != 0:
            return None
        text = result.stdout.strip("\r\n")
    except Exception:
        return None

    if not text:
        return None
    return text if colors == "none" else _ansi_to_poezio(text)

def _ansi_to_poezio_colors(text):
    """
    Translates standard 256-color ANSI escape sequences into Poezio's
    internal \x19<fg>,<bg>} format.
    """
    if '\x1b' not in text:
        return text

    fg = -1
    bg = -1
    out = []
    last_end = 0

    for match in _ANSI_CSI_RE.finditer(text):
        out.append(text[last_end:match.start()])
        last_end = match.end()

        raw_codes = match.group(1)
        if not raw_codes or raw_codes == "0":
            # Reset
            fg = -1
            bg = -1
            out.append("\x19o")
            continue

        codes = [int(c) for c in raw_codes.split(";") if c.isdigit()]
        i = 0
        changed = False

        while i < len(codes):
            code = codes[i]
            if code == 0:
                fg = -1
                bg = -1
                out.append("\x19o")
            elif code == 39:
                fg = -1
                changed = True
            elif code == 49:
                bg = -1
                changed = True
            elif 30 <= code <= 37:
                fg = code - 30
                changed = True
            elif 40 <= code <= 47:
                bg = code - 40
                changed = True
            elif 90 <= code <= 97:
                fg = code - 90 + 8
                changed = True
            elif 100 <= code <= 107:
                bg = code - 100 + 8
                changed = True
            elif code == 38 and i + 2 < len(codes) and codes[i + 1] == 5:
                # 256-color Foreground: 38;5;N
                fg = codes[i + 2]
                i += 2
                changed = True
            elif code == 48 and i + 2 < len(codes) and codes[i + 1] == 5:
                # 256-color Background: 48;5;N
                bg = codes[i + 2]
                i += 2
                changed = True
            i += 1

        if changed:
            out.append(f"\x19{fg},{bg}}}")

    out.append(text[last_end:])
    # Ensure line styles reset cleanly at the end
    out.append("\x19o")
    return "".join(out)

class Plugin(BasePlugin):
    def init(self):
        # BasePlugin.__init__ provides self.api and self.config (and on
        # recent versions self.core as well).
        self.core = getattr(self, "core", None) or self.api.core

        # Cached config, read by background threads. Only ever WRITTEN from
        # the main thread -- plain str reads/writes are atomic under the GIL.
        self.socket_path = self.config.get("socket_path", DEFAULT_SOCKET_PATH)
        self.account = self.config.get("account", "")
        self.attachments_dir = self.config.get("attachments_dir", DEFAULT_ATTACHMENTS_DIR)
        self.colors = self.config.get("colors", DEFAULT_COLORS)
        if self.colors not in ("none", "16", "256"):
            self.colors = DEFAULT_COLORS

        self._stop_event = threading.Event()
        self._receiver_thread = None
        self._listener_sock = None
        self._display_queue = queue.Queue()
        self._tabs = {}             # signal key -> tab (for completion)
        self._hooked = set()        # tab names whose input we already hooked
        self._warned_names = set()  # tab names whose error we already logged
        self._poll_event = None

        self._register_command()
        self._start_receiver()
        self._schedule_poll()

        self.api.information("signal_bridge: poezio %s detected" % _poezio_version(), "Info")
        self.api.information("signal_bridge loaded. Socket: %s" % self.socket_path, "Info")
        self.api.information(
            "signal_bridge: image preview support (chafa): %s"
            % (CHAFA if CHAFA else "NOT INSTALLED - photos will show as a path only"),
            "Info")

    # -------------------------------------------------------------------
    # Command registration
    # -------------------------------------------------------------------

    def _register_command(self):
        # poezio's add_command() keyword for the one-line description is
        # `short` -- passing `shortdesc` was what broke loading before.
        try:
            self.api.add_command(
                "signal",
                self.cmd_signal,
                usage=USAGE,
                help=HELP,
                short="Signal bridge via signal-cli",
                completion=self.completion_signal,
            )
        except TypeError as exc:
            self.api.information(
                "signal_bridge: add_command() rejected the documented "
                "keywords (%s); registering a bare command instead" % exc,
                "Warning")
            self.api.add_command("signal", self.cmd_signal)

    def _set_config(self, option, value):
        try:
            set_and_save = getattr(self.config, "set_and_save", None)
            if set_and_save is not None:
                try:
                    set_and_save(option, value)
                except TypeError:
                    self.config.set(option, value)
            else:
                self.config.set(option, value)
                save = getattr(self.config, "save", None)
                if save is not None:
                    save()
        except Exception as exc:
            self.api.information(
                "signal_bridge: could not persist %s (%s: %s); the value "
                "applies for this session only"
                % (option, type(exc).__name__, exc), "Warning")
        setattr(self, option, value)

    # -------------------------------------------------------------------
    # Tab management (the equivalent of Profanity plugin windows).
    #
    # Each Signal contact/group gets a conversation tab for a fake JID
    # under signal.invalid, with its send path hooked (see
    # _hook_tab_input). Poezio's tab internals have churned between
    # releases, so creation walks a ladder of strategies -- ending with
    # the exact code that handles the /message command -- and reports
    # every failure individually. After each strategy the tab registry
    # itself is checked, because return values are inconsistent between
    # versions.
    # -------------------------------------------------------------------

    def _tab_name(self, key):
        local = re.sub(r"[^0-9A-Za-z._+-]", "_", key)
        return local + "@" + SIGNAL_DOMAIN

    def _name_of_tab(self, tab):
        name = getattr(tab, "name", None)
        if name is None:
            getter = getattr(tab, "get_name", None)
            try:
                name = getter() if getter else ""
            except Exception:
                name = ""
        return name

    def _all_tabs(self):
        tabs_obj = getattr(self.core, "tabs", None)
        inner = getattr(tabs_obj, "tabs", None)
        if isinstance(inner, (list, tuple)):
            return list(inner)
        if isinstance(tabs_obj, (list, tuple)):
            return list(tabs_obj)
        return []

    def _find_tab(self, name):
        tabs_obj = getattr(self.core, "tabs", None)
        by_name = getattr(tabs_obj, "by_name", None)
        if by_name is not None:
            try:
                tab = by_name(name)
                if tab is not None:
                    return tab
            except Exception:
                pass
        for tab in self._all_tabs():
            if self._name_of_tab(tab) == name:
                return tab
        return None

    def _log_tab_error(self, name, message):
        if name in self._warned_names:
            return
        self._warned_names.add(name)
        self.api.information("signal_bridge: " + message, "Error")

    def _create_tab(self, name):
        errors = []

        # 1) get-or-create helpers, on both api and core
        for owner in (self.api, self.core):
            getter = getattr(owner, "get_conversation_by_jid", None)
            if getter is None:
                continue
            for arg in (JID(name), name):
                try:
                    try:
                        tab = getter(arg, True)
                    except TypeError:
                        tab = getter(arg)
                except Exception as exc:
                    errors.append("%s.get_conversation_by_jid(%r): %s: %s"
                                  % (type(owner).__name__, arg,
                                     type(exc).__name__, exc))
                    continue
                if tab is not None:
                    return tab
                errors.append("%s.get_conversation_by_jid(%r) returned None"
                              % (type(owner).__name__, arg))

        # 2) core.open_conversation_window (helper seen on some versions)
        opener = getattr(self.core, "open_conversation_window", None)
        if opener is not None:
            for arg in (JID(name), name):
                try:
                    try:
                        opener(arg, False)
                    except TypeError:
                        opener(arg)
                except Exception as exc:
                    errors.append("core.open_conversation_window(%r): %s: %s"
                                  % (arg, type(exc).__name__, exc))
                    continue
                tab = self._find_tab(name)
                if tab is not None:
                    return tab
                errors.append("core.open_conversation_window(%r) did not "
                              "register a tab" % arg)

        # 3) let poezio itself run /message -- the same path as typing
        #    "/message <jid>" by hand, so it tracks poezio's real behaviour.
        command_message = getattr(self.core, "command_message", None)
        if command_message is not None:
            try:
                command_message(name)
                tab = self._find_tab(name)
                if tab is not None:
                    return tab
                errors.append("core.command_message(%r) did not register a "
                              "tab" % name)
            except Exception as exc:
                errors.append("core.command_message(%r): %s: %s"
                              % (name, type(exc).__name__, exc))

        # 4) manual construction, then registration
        tab_cls = (getattr(poezio_tabs, "StaticConversationTab", None)
                   or getattr(poezio_tabs, "ConversationTab", None))
        if tab_cls is None:
            errors.append("poezio.tabs exposes no ConversationTab")
        else:
            tab = None
            for construct in (
                lambda: tab_cls(self.core, JID(name)),
                lambda: tab_cls(self.core, jid=JID(name)),
                lambda: tab_cls(self.core, name),
            ):
                try:
                    tab = construct()
                    break
                except Exception as exc:
                    errors.append("%s(...): %s: %s"
                                  % (tab_cls.__name__,
                                     type(exc).__name__, exc))
            if tab is not None and self._register_tab(tab, name, errors):
                return tab

        self._log_tab_error(
            name,
            "unable to open a tab for %s (poezio %s). Attempted: %s"
            % (name, _poezio_version(), "; ".join(errors)))
        return None

    def _register_tab(self, tab, name, errors):
        for adder in (getattr(getattr(self.core, "tabs", None), "add_tab", None),
                      getattr(self.core, "add_tab", None)):
            if adder is None:
                continue
            try:
                adder(tab)
            except Exception as exc:
                errors.append("registration via %s: %s: %s"
                              % (getattr(adder, "__name__", "adder"),
                                 type(exc).__name__, exc))
                continue
            try:
                tab.resize()
            except Exception:
                pass
            if self._find_tab(name) is tab:
                return True
        return False

    def _ensure_tab(self, key, focus=False):
        name = self._tab_name(key)
        tab = self._find_tab(name)
        if tab is None:
            tab = self._create_tab(name)
            if tab is None:
                return None
        if name not in self._hooked:
            self._hook_tab_input(tab, key)
            self._hooked.add(name)
        self._tabs[key] = tab
        if focus:
            self._focus_tab(tab)
        return tab

    def _focus_tab(self, tab):
        try:
            self.core.tabs.current_tab = tab
        except Exception as exc:
            self.api.information(
                "signal_bridge: cannot focus the tab (%s: %s)"
                % (type(exc).__name__, exc), "Warning")
        self._refresh_tab(tab)

    def _refresh_tab(self, tab):
        current = None
        try:
            current = self.core.tabs.current_tab
        except Exception:
            pass
        if current is tab:
            refreshed = False
            refresh_window = getattr(self.core, "refresh_window", None)
            if refresh_window is not None:
                try:
                    refresh_window()
                    refreshed = True
                except Exception:
                    pass
            if not refreshed:
                try:
                    tab.refresh()
                except Exception:
                    pass
        else:
            try:
                if getattr(tab, "state", "normal") in (None, "normal"):
                    tab.state = "message"
            except Exception:
                pass

    def _write_tab(self, tab, text, markup=None):
        """Append lines to a tab. markup=None means 'follow the colors
        setting'; every line is sanitized so raw control bytes can never
        reach the renderer. Last resort: the global info window."""
        if not text:
            return
        if markup is None:
            markup = self.colors != "none"
        for line in text.split("\n"):
            line = _sanitize(line, markup)
            if not line:
                continue
            if not self._write_line(tab, line):
                self.api.information("[Signal] " + line, "Info")

    def _write_line(self, tab, line):
        msg = None
        if InfoMessage is not None:
            try:
                msg = InfoMessage(line)
            except Exception:
                msg = None
        payload = msg if msg is not None else line
        for attempt in (
            lambda: tab.add_message(payload),
            lambda: tab.text_buffer.add_message(payload),
        ):
            try:
                attempt()
                self._refresh_tab(tab)
                return True
            except Exception:
                continue
        tw = getattr(tab, "text_win", None)
        if tw is not None:
            for meth in (getattr(tw, "add_message", None),
                         getattr(tw, "add_line", None)):
                if meth is None:
                    continue
                try:
                    meth(line)
                    self._refresh_tab(tab)
                    return True
                except Exception:
                    continue
        return False

    def _hook_tab_input(self, tab, key):
        """Route everything this tab would send over XMPP to signal-cli
        instead. Profanity gave plugins an input callback; here we wrap
        the tab's send entry points (the technique poezio's own OTR
        plugin uses) and additionally patch the /say and /me entries in
        the tab's command table, in case they hold pre-bound methods."""
        def send_signal_message(*args, **kwargs):
            first = args[0] if args else kwargs.get("message")
            if isinstance(first, str):
                text = first
            else:
                text = (getattr(first, "plain", None)
                        or getattr(first, "txt", None)
                        or "")
                if not isinstance(text, str):
                    text = str(text or "")
            text = (text or "").strip()
            if not text:
                return
            self._write_tab(tab, "me: " + text)
            self._send_async(key, text)

        for attr in ("command_say", "send_message"):
            try:
                setattr(tab, attr, send_signal_message)
            except Exception:
                pass
        # Never leak anything -- not even chatstates -- to the fake domain.
        try:
            tab.send_chat_state = lambda *a, **kw: None
        except Exception:
            pass

        commands = getattr(tab, "commands", None)
        if isinstance(commands, dict):
            for cmd_name in ("say", "me"):
                entry = commands.get(cmd_name)
                if entry is None:
                    continue
                try:
                    if hasattr(entry, "_replace"):        # namedtuple
                        commands[cmd_name] = entry._replace(
                            func=send_signal_message)
                    elif hasattr(entry, "func"):           # dataclass-ish
                        entry.func = send_signal_message
                    elif isinstance(entry, (tuple, list)) and entry:
                        rebuilt = list(entry)
                        rebuilt[0] = send_signal_message
                        commands[cmd_name] = tuple(rebuilt)
                except Exception:
                    pass

    # -------------------------------------------------------------------
    # Sending. Runs in a background thread so it never blocks the UI. Reads
    # only the cached socket_path / account attrs -- never calls poezio.*.
    # All results are handed to the main thread via _display_queue.
    # -------------------------------------------------------------------

    def _send_async(self, key, text):
        threading.Thread(target=self._do_send, args=(key, text),
                         daemon=True).start()

    def _do_send(self, key, text):
        req_id = "send-" + uuid.uuid4().hex[:8]
        if key.startswith("group:"):
            # signal-cli expects the bare groupId for group destinations
            params = {"groupId": key[len("group:"):], "message": text}
        else:
            params = {"recipient": [key], "message": text}
        if self.account:
            params["account"] = self.account
        request = {"jsonrpc": "2.0", "method": "send", "id": req_id,
                   "params": params}

        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect(self.socket_path)
            sock.sendall((json.dumps(request) + "\n").encode("utf-8"))

            sock_file = sock.makefile("r", encoding="utf-8")
            response = None
            for raw_line in sock_file:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    data = json.loads(raw_line)
                except ValueError:
                    continue
                # Skip "receive" notifications on this short-lived connection.
                if data.get("id") == req_id:
                    response = data
                    break
            sock.close()

            if response is None:
                self._display_queue.put(
                    (key, "*** signal-cli did not respond to send ***"))
            elif response.get("error"):
                err = response["error"]
                msg = (err.get("message", str(err))
                       if isinstance(err, dict) else str(err))
                self._display_queue.put(
                    (key, "*** send failed: %s ***" % msg))
            # else: success, the optimistic "me: ..." echo is already shown

        except OSError as exc:
            self._display_queue.put(
                (key, "*** could not reach signal-cli socket: %s ***" % exc))

    def _attachment_blocks(self, attachments, label):
        """Runs on the background receiver thread. Builds display-ready text
        blocks for image attachments. Never touches poezio.* directly."""
        blocks = []
        for att in attachments or []:
            content_type = att.get("contentType") or ""
            if not content_type.startswith("image/"):
                continue
            att_id = att.get("id")
            if not att_id:
                continue
            path = os.path.join(self.attachments_dir, att_id)
            art = _image_to_ascii(path, self.colors)
            if art:
                # Convert raw terminal ANSI to Poezio curses color tags
                poezio_art = _ansi_to_poezio_colors(art)
                blocks.append("%s sent a photo:\n%s" % (label, poezio_art))
            else:
                self._display_queue.put((
                    "__log_error__",
                    "signal_bridge: could not convert attachment to ASCII: %s"
                    % path))
                blocks.append("%s sent a photo (preview unavailable: %s)"
                              % (label, path))
        return blocks

    # -------------------------------------------------------------------
    # Receiving: one persistent connection, read in a background thread,
    # reconnect with backoff on failure. This thread never calls poezio.*
    # directly -- it pushes work onto _display_queue for the poller.
    # -------------------------------------------------------------------

    def _receiver_loop(self):
        backoff = 1
        while not self._stop_event.is_set():
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(self.socket_path)
                self._listener_sock = sock
                sock_file = sock.makefile("r", encoding="utf-8")
                self._display_queue.put(
                    ("__log_info__", "connected to " + self.socket_path))
                backoff = 1

                for raw_line in sock_file:
                    if self._stop_event.is_set():
                        break
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        data = json.loads(raw_line)
                    except ValueError:
                        continue
                    self._handle_notification(data)

                sock.close()
            except OSError as exc:
                self._display_queue.put(
                    ("__log_error__", "socket error: %s" % exc))
            finally:
                self._listener_sock = None

            if self._stop_event.is_set():
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)

    def _key_and_label(self, source_number, source_name, group_info):
        if group_info:
            group_id = group_info.get("groupId", "unknown-group")
            key = "group:" + group_id
            label = "%s (group %s)" % (source_name, group_id[:8])
        else:
            key = source_number
            label = source_name
        return key, label

    def _handle_notification(self, data):
        """Runs on the background receiver thread. Only ever touches
        _display_queue -- never calls poezio.* directly."""
        if data.get("method") != "receive":
            return
        params = data.get("params") or {}
        envelope = params.get("envelope")
        if envelope is None and isinstance(params.get("result"), dict):
            # subscribeReceive-style wrapping (--receive-mode=manual)
            envelope = params["result"].get("envelope")
        if not envelope:
            return
        source_number = (envelope.get("sourceNumber")
                         or envelope.get("source") or "unknown")
        source_name = envelope.get("sourceName") or source_number
        data_message = envelope.get("dataMessage")

        # Signal reaction
        if data_message:
            reaction = data_message.get("reaction")
            if reaction:
                emoji = reaction.get("emoji", "")
                target_author = reaction.get("targetAuthor")
                is_remove = reaction.get("isRemove", False)
                is_our_message = (
                    not target_author
                    or not self.account
                    or target_author == self.account
                )
                if emoji and not is_remove and is_our_message:
                    key, label = self._key_and_label(
                        source_number, source_name,
                        data_message.get("groupInfo"))
                    # (string kept verbatim from the Profanity plugin)
                    message = "%s zareagował na Twoją wiadomość %s" % (label, emoji)
                    self._display_queue.put(("__ensure__", key))
                    self._display_queue.put((key, message))
                    self._display_queue.put(("__notify__", message))
                    return

        # Normal incoming message: text and/or image attachments
        if data_message:
            text = data_message.get("message")
            attachments = data_message.get("attachments")
            if text or attachments:
                key, label = self._key_and_label(
                    source_number, source_name,
                    data_message.get("groupInfo"))
                self._display_queue.put(("__ensure__", key))
                if text:
                    self._display_queue.put((key, "%s: %s" % (label, text)))
                for block in self._attachment_blocks(attachments, label):
                    self._display_queue.put((key, block))
                notify_text = text if text else "(photo)"
                self._display_queue.put(
                    ("__notify__", "%s: %s" % (label, notify_text)))
                return

        # Messages sent from another linked Signal device
        sync_message = envelope.get("syncMessage")
        if sync_message and sync_message.get("sentMessage"):
            sent = sync_message["sentMessage"]
            dest = sent.get("destinationNumber") or sent.get("destination")
            text = sent.get("message")
            attachments = sent.get("attachments")
            if dest and (text or attachments):
                self._display_queue.put(("__ensure__", dest))
                if text:
                    self._display_queue.put(
                        (dest, "me (linked device): %s" % text))
                for block in self._attachment_blocks(attachments,
                                                     "me (linked device)"):
                    self._display_queue.put((dest, block))

    # -------------------------------------------------------------------
    # Queue poller -- runs on Poezio's main loop via self-rescheduling
    # delayed events (poezio's DelayedEvent is one-shot, unlike
    # prof.register_timed). The only place, besides command/tab callbacks
    # and init/cleanup, that is allowed to call poezio APIs.
    # -------------------------------------------------------------------

    def _schedule_poll(self, delay=0.5):
        # 1) poezio timed events
        try:
            event = timed_events.DelayedEvent(delay, self._poll_queue)
        except Exception as exc:
            event = None
            self.api.information(
                "signal_bridge: DelayedEvent failed: %s: %s"
                % (type(exc).__name__, exc), "Error")
        if event is not None:
            for adder in (getattr(self.api, "add_timed_event", None),
                          getattr(self.core, "add_timed_event", None)):
                if adder is None:
                    continue
                try:
                    adder(event)
                    self._poll_event = event
                    return
                except Exception as exc:
                    self.api.information(
                        "signal_bridge: %s failed: %s: %s"
                        % (getattr(adder, "__name__", "adder"),
                           type(exc).__name__, exc), "Error")
        # 2) fallback: the slixmpp event loop poezio runs on
        try:
            self.core.xmpp.loop.call_later(delay, self._poll_queue)
        except Exception as exc:
            self.api.information(
                "signal_bridge: cannot schedule the queue poller (%s: %s); "
                "incoming messages will not be displayed"
                % (type(exc).__name__, exc), "Error")

    def _poll_queue(self, *_args):
        if self._stop_event.is_set():
            return
        try:
            while True:
                try:
                    key, message = self._display_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._dispatch(key, message)
                except Exception as exc:
                    self.api.information(
                        "signal_bridge: error displaying a message: %s: %s"
                        % (type(exc).__name__, exc), "Error")
        finally:
            if not self._stop_event.is_set():
                self._schedule_poll()

    def _dispatch(self, key, message):
        if key == "__ensure__":
            self._ensure_tab(message)
            return
        if key == "__notify__":
            self.api.information("[Signal] " + message, "Info")
            self._notify(message)
            return
        if key == "__log_info__":
            self.api.information("signal_bridge: " + message, "Info")
            return
        if key == "__log_error__":
            self.api.information("signal_bridge: " + message, "Error")
            return
        tab = self._ensure_tab(key)
        if tab is not None:
            self._write_tab(tab, message)
        else:
            # tab creation failed -- still surface the message
            self.api.information("[Signal/%s] %s" % (key, message), "Info")

    def _notify(self, message):
        """Desktop notification, best effort (the API has moved around
        between poezio versions)."""
        handler = getattr(self.core, "notifications", None)
        if handler is not None and hasattr(handler, "notify"):
            try:
                handler.notify(message)
                return
            except Exception:
                pass
        try:
            from poezio import notify
            notify.show_notification(message, 5000)
        except Exception:
            pass

    def _start_receiver(self):
        if self._receiver_thread and self._receiver_thread.is_alive():
            return
        self._stop_event.clear()
        self._receiver_thread = threading.Thread(target=self._receiver_loop,
                                                 daemon=True)
        self._receiver_thread.start()

    def _restart_receiver(self):
        try:
            if self._listener_sock:
                self._listener_sock.close()
        except OSError:
            pass
        self._start_receiver()

    # -------------------------------------------------------------------
    # /signal command (main thread)
    # -------------------------------------------------------------------

    def cmd_signal(self, arg):
        try:
            args = shlex.split(arg or "")
        except ValueError:
            args = (arg or "").split()

        if not args:
            self.api.information("Usage: /signal " + USAGE, "Error")
            return

        sub = args[0].lower()
        rest = args[1:]

        if sub == "open" and rest:
            self._ensure_tab(rest[0], focus=True)

        elif sub == "send" and len(rest) >= 2:
            number, text = rest[0], " ".join(rest[1:])
            tab = self._ensure_tab(number, focus=True)
            echo = "me: " + text
            if tab is not None:
                self._write_tab(tab, echo)
            else:
                self.api.information("[Signal] " + echo, "Info")
            self._send_async(number, text)

        elif sub == "socket" and rest:
            self._set_config("socket_path", " ".join(rest))
            self.api.information(
                "signal_bridge: socket path set to %s, reconnecting..."
                % self.socket_path, "Info")
            self._restart_receiver()

        elif sub == "account" and rest:
            self._set_config("account", rest[0])
            self.api.information(
                "signal_bridge: account set to %s" % self.account, "Info")

        elif sub == "attachments" and rest:
            self._set_config("attachments_dir", " ".join(rest))
            self.api.information(
                "signal_bridge: attachments dir set to %s"
                % self.attachments_dir, "Info")
        
        elif sub == "colors" and rest and rest[0] in ("none", "16", "256"):
            self._set_config("colors", rest[0])
            self.api.information(
                "signal_bridge: preview color mode set to %s" % self.colors, "Info")
        
        elif sub == "colortest":
            self._colortest()

        elif sub == "status":
            self.api.information(
                "signal_bridge: poezio=%s socket=%s account=%s attachments=%s chafa=%s"
                % (_poezio_version(), self.socket_path,
                   self.account or "(single-account daemon)",
                   self.attachments_dir, CHAFA or "NOT FOUND"),
                "Info")

        elif sub == "reconnect":
            self._restart_receiver()
            self.api.information("signal_bridge: reconnecting...", "Info")

        else:
            self.api.information("Usage: /signal " + USAGE, "Error")

    def completion_signal(self, the_input):
        try:
            tokens = (the_input.get_text() or "").split(" ")
            if len(tokens) <= 1:
                return self._autocomplete(the_input, SUBCOMMANDS)
            if len(tokens) <= 3 and tokens[1].lower() in ("open", "send"):
                return self._autocomplete(the_input, sorted(self._tabs))
        except Exception:
            return None
        return None

    def _autocomplete(self, the_input, words):
        try:
            return the_input.auto_completion(words, "", quotify=False)
        except Exception:
            pass
        try:
            return the_input.auto_completion(words)
        except Exception:
            return None
    
    def _colortest(self):
        """Writes labeled probe lines into a dedicated tab. Run this
        before trusting color mode on a new poezio build."""
        tab = self._ensure_tab("colortest", focus=True)
        if tab is None:
            return
        self._write_tab(tab, "colortest (poezio %s)" % _poezio_version(),
                        markup=True)
        self._write_tab(tab, "1. plain text", markup=True)
        self._write_tab(tab, "2. " + BOLD + "bold text" + RESET
                        + " <- should be BOLD", markup=True)
        for n in range(8):
            self._write_tab(tab, "3. color %d: " % n + _fmt(n) + "█████"
                            + RESET, markup=True)
        self._write_tab(tab, "4. gradient 0-255:", markup=True)
        self._write_tab(tab,
                        "".join(_fmt(n) + "█" for n in range(0, 256, 8))
                        + RESET, markup=True)
        self.api.information(
            "signal_bridge: colortest written to the colortest@signal.invalid "
            "tab. If line 2 is bold and the blocks in 3-4 are colored, "
            "colors work -> /signal colors 256. If you see ^Y characters, "
            "stray digits or braces, this build does not parse that markup "
            "-> keep /signal colors none (and close the test tab).", "Info")

    # -------------------------------------------------------------------
    # Plugin lifecycle
    # -------------------------------------------------------------------

    def cleanup(self):
        self._stop_event.set()
        event, self._poll_event = self._poll_event, None
        if event is not None:
            for remover in (getattr(self.api, "remove_timed_event", None),
                            getattr(self.core, "remove_timed_event", None)):
                if remover is None:
                    continue
                try:
                    remover(event)
                    break
                except Exception:
                    continue
        try:
            if self._listener_sock:
                self._listener_sock.close()
        except OSError:
            pass