import asyncio
import atexit
import os
import shutil
import subprocess
import time

import logging

try:
    from poezio.plugin import BasePlugin
except ImportError:
    from plugin import BasePlugin

try:
    from poezio.common import find_delayed_tag
except ImportError:
    try:
        from common import find_delayed_tag
    except ImportError:
        def find_delayed_tag(message):
            try:
                delay = message.xml.find('{urn:xmpp:delay}delay')
                if delay is None:
                    delay = message.xml.find('{jabber:x:delay}x')
                return (delay is not None, None)
            except Exception:
                return (False, None)

STATE_FILE = os.path.expanduser("~/.cache/i3blocks-poezio-unread")
SIGNAL = 10  # SIGRTMIN+10 -- must match signal= in i3blocks config
DEFAULT_SOUND_FILE = '/usr/share/sounds/freedesktop/stereo/message-new-instant.oga'
SOUND_PLAYER_CANDIDATES = ('paplay', 'pw-play', 'aplay', 'ffplay')

log = logging.getLogger(__name__)

# Set to True to notify on every message in a MUC.
# Set to False if you only want notifications when mentioned/highlighted.
NOTIFY_ALL_MUC = True

def _sound_command(player, sound_file):
    if player == 'aplay':
        return [player, '-q', sound_file]
    if player == 'ffplay':
        return [player, '-nodisp', '-autoexit', '-loglevel', 'quiet', sound_file]
    return [player, sound_file]  # paplay, pw-play


class Plugin(BasePlugin):
    def init(self):
        self._unread = set()
        self._load()
        self._sound_player_cache = None  # None = not probed yet, '' = none found
        self._last_sound_time = 0.0


        # Catch tabs already unread when the plugin is loaded
        if hasattr(self.core, 'tabs'):
            for tab in self.core.tabs:
                if tab and getattr(tab, 'state', None) in ('message', 'highlight', 'private', 'attention'):
                    ident = self._get_tab_identifier(tab)
                    if ident:
                        self._unread.add(ident)

        self._save_and_signal()

        self.api.add_command(
            'sound_test',
            self.command_sound_test,
            usage='[delay_seconds]',
            help=('Diagnose the sound-notification pipeline and try to play '
                'the configured sound immediately, bypassing all the '
                'normal gating. Optionally wait N seconds first so you can '
                'switch to another window before the focus check runs.'),
            short='Test the notification sound',
        )

        # Message hooks
        self.api.add_event_handler('conversation_msg', self.on_conversation_msg)
        self.api.add_event_handler('signal_msg', self.on_signal_msg)
        self.api.add_event_handler('private_msg', self.on_private_msg)

        if NOTIFY_ALL_MUC:
            self.api.add_event_handler('muc_msg', self.on_muc_msg)
        else:
            self.api.add_event_handler('highlight', self.on_highlight)

        # Tab focus hook
        self.api.add_event_handler('tab_change', self.on_tab_change)

        atexit.register(self.cleanup)


    def _sound_events(self):
        raw = self.config.get('sound_events',
                              'conversation_msg,private_msg,highlight')
        return {e.strip() for e in raw.split(',') if e.strip()}

    def _resolve_sound_player(self):
        if self._sound_player_cache is not None:
            return self._sound_player_cache or None
        forced = self.config.get('sound_player', '').strip()
        candidates = [forced] if forced else list(SOUND_PLAYER_CANDIDATES)
        for candidate in candidates:
            if candidate and shutil.which(candidate):
                self._sound_player_cache = candidate
                return candidate
        self._sound_player_cache = ''
        return None

    async def _maybe_play_sound(self, event_name):
        if event_name not in self._sound_events():
            return

        sound_file = self.config.get('sound_file', DEFAULT_SOUND_FILE)
        if not sound_file or not os.path.isfile(sound_file):
            return

        if await self._terminal_has_focus():
            return  # you're looking at it, no need to ding

        cooldown = self.config.get('sound_cooldown', 3.0)
        now = time.monotonic()
        if now - self._last_sound_time < cooldown:
            return

        player = self._resolve_sound_player()
        if not player:
            return

        self._last_sound_time = now
        try:
            await asyncio.create_subprocess_exec(
                *_sound_command(player, sound_file),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            pass


    def cleanup(self):
        try:
            atexit.unregister(self.cleanup)
        except Exception:
            pass
        self._unread.clear()
        self._save_and_signal()

    async def command_sound_test(self, args: str) -> None:
        """
        Diagnose the sound pipeline step by step and actually try to
        play the sound, bypassing the event filter, the focus check,
        and the cooldown -- and, unlike normal playback, surfaces
        whatever the player itself printed on failure instead of
        swallowing it.

        Usage: /sound_test [delay_seconds]

        Running it with no argument checks focus *right now*, which
        is trivially always "focused" since you just typed a command
        into this terminal. Pass a delay (e.g. `/sound_test 5`) to
        get a few seconds to switch to another window first, so the
        focus check reflects a genuinely unfocused terminal.
        """
        sound_file = self.config.get('sound_file', DEFAULT_SOUND_FILE)
        self.api.information('sound_file = %s' % sound_file, 'Info')
        if not sound_file or not os.path.isfile(sound_file):
            self.api.information(
                'File not found or unreadable: %r -- fix sound_file in the '
                'config before going further.' % sound_file, 'Error')
            return
        self.api.information('Sound file exists, OK.', 'Info')

        args = args.strip()
        delay = 0.0
        if args:
            try:
                delay = float(args)
            except ValueError:
                self.api.information(
                    'Usage: /sound_test [delay_seconds] -- %r is not a '
                    'number' % args, 'Error')
                return

        if delay > 0:
            self.api.information(
                'Waiting %.3gs -- switch to another window now to test the '
                '"unfocused" path for real.' % delay, 'Info')
            await asyncio.sleep(delay)

        # Force a fresh read: don't trust the 0.5s cache from before
        # the wait (or from a previous /sound_test).
        self._focus_cache = None
        focused = await self._terminal_has_focus()
        self.api.information(
            '_terminal_has_focus() currently reports: %r (True = sound '
            'would be suppressed right now in normal operation; None = '
            'undetectable, falls back to "play")' % (focused,), 'Info')

        # Force a fresh probe: don't trust a cached "not found" from
        # earlier in the session.
        self._sound_player_cache = None
        player = self._resolve_sound_player()
        if not player:
            forced = self.config.get('sound_player', '').strip()
            checked = forced or ', '.join(SOUND_PLAYER_CANDIDATES)
            self.api.information(
                'No usable sound player found on PATH (checked: %s). '
                'Install one of these, or set sound_player explicitly.'
                % checked, 'Error')
            return
        self.api.information('Using player: %s' % player, 'Info')

        cmd = _sound_command(player, sound_file)
        self.api.information('Running: ' + ' '.join(cmd), 'Info')
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
        except OSError as exc:
            self.api.information('Failed to launch %r: %s' % (player, exc),
                                 'Error')
            return

        if proc.returncode == 0:
            self.api.information(
                "Player exited successfully. If you didn't hear anything, "
                'the problem is downstream of poezio: wrong audio '
                'sink/device, volume/mute, or this process landing in a '
                "different audio session than your desktop's.", 'Info')
        else:
            detail = (err or out or b'').decode(errors='replace').strip()
            self.api.information(
                '%s exited with code %s: %s'
                % (player, proc.returncode, detail or '(no output)'),
                'Error')

    # async notifications
    async def _should_notify(self, tab) -> bool:
        """
        True if this message should be surfaced to i3blocks: either
        it's not poezio's currently-selected tab, or it is, but the
        terminal itself doesn't actually have focus right now (so
        you're not really looking at it).
        """
        if tab != self.api.current_tab():
            return True
        focused = await self._terminal_has_focus()
        # Unknown (no DISPLAY, xdotool missing, ...) -> notify anyway.
        # Missing a message is worse than one extra blink.
        return not focused

    async def _terminal_has_focus(self):
        now = time.monotonic()
        cached = getattr(self, '_focus_cache', None)
        if cached is not None and now - cached[0] < 0.5:
            return cached[1]
        value = await self._compute_terminal_focus()
        self._focus_cache = (now, value)
        return value

    async def _compute_terminal_focus(self):
        if not os.environ.get('DISPLAY'):
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                'xdotool', 'getactivewindow', 'getwindowpid',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
        except (FileNotFoundError, OSError):
            return None
        try:
            focused_pid = int(out.strip())
        except ValueError:
            return None
        return focused_pid in self._own_ancestor_pids()

    @staticmethod
    def _own_ancestor_pids(max_depth=12):
        pids = set()
        current = os.getpid()
        for _ in range(max_depth):
            if current is None or current <= 1 or current in pids:
                break
            pids.add(current)
            current = Plugin._parent_pid(current)
        return pids

    @staticmethod
    def _parent_pid(pid):
        try:
            with open(f'/proc/{pid}/stat') as f:
                data = f.read()
            # comm (2nd field) is in parens and can itself contain
            # spaces/parens, so split on the *last* ')'
            fields = data.rsplit(')', 1)[1].split()
            return int(fields[1])  # ppid
        except (OSError, IndexError, ValueError):
            return None

    # ---- State persistence & signaling -----------------------------------

    def _load(self):
        try:
            with open(STATE_FILE, "r") as f:
                self._unread = set(line.strip() for line in f if line.strip())
        except IOError:
            self._unread = set()

    def _save_and_signal(self):
        d = os.path.dirname(STATE_FILE)
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            f.write("\n".join(sorted(self._unread)))
        try:
            subprocess.Popen(["pkill", "-RTMIN+%d" % SIGNAL, "-x", "i3blocks"])
        except Exception:
            pass

    def _get_tab_identifier(self, tab):
        if not tab:
            return None
        if getattr(tab, 'name', None):
            return str(tab.name)
        if getattr(tab, 'jid', None):
            return str(getattr(tab.jid, 'bare', tab.jid))
        return None

    def _get_tab_keys(self, tab):
        """Returns all matching names/JIDs that identify this tab."""
        keys = set()
        if not tab:
            return keys
        name = getattr(tab, 'name', None)
        if name:
            keys.add(str(name))
            keys.add(str(name).split('/')[0])
        jid = getattr(tab, 'jid', None)
        if jid:
            keys.add(str(jid))
            if hasattr(jid, 'bare'):
                keys.add(str(jid.bare))
        return keys

    def _mark_unread(self, identifier):
        self._load()
        if identifier not in self._unread:
            self._unread.add(identifier)
            self._save_and_signal()

    def _mark_read(self, tab):
        self._load()
        keys = self._get_tab_keys(tab)
        if not keys:
            return
        matched = self._unread.intersection(keys)
        if matched:
            self._unread.difference_update(matched)
            self._save_and_signal()

    def _prune_closed_tabs(self):
        """Clean up unreads if a tab is closed via /close."""
        if not hasattr(self.core, 'tabs'):
            return
        open_keys = set()
        for t in self.core.tabs:
            if t:
                open_keys.update(self._get_tab_keys(t))
        pruned = self._unread.intersection(open_keys)
        if pruned != self._unread:
            self._unread = pruned
            self._save_and_signal()

    # ---- Event Handlers --------------------------------------------------

    async def on_conversation_msg(self, message, tab):
        log.debug("[i3blocks] on_conversation_msg ENTER: body=%r, tab=%r", message['body'] if message else None, tab)
        if not message['body']:
            return
        if find_delayed_tag(message)[0]:
            return
        try:
            if message['from'].bare == self.core.xmpp.boundjid.bare:
                return
        except Exception:
            pass

        await self._maybe_play_sound('conversation_msg')
        if await self._should_notify(tab):
            ident = self._get_tab_identifier(tab)
            if ident:
                self._mark_unread(ident)

    async def on_private_msg(self, message, tab):
        log.debug("[i3blocks] on_private_msg ENTER: body=%r, tab=%r", message['body'] if message else None, tab)
        if not message['body']:
            return
        if find_delayed_tag(message)[0]:
            return

        if await self._should_notify(tab):
            ident = self._get_tab_identifier(tab)
            if ident:
                self._mark_unread(ident)

    async def on_signal_msg(self, tab, text='', kind='message'):
        """signal_bridge fires 'signal_msg' whenever it displays an
        incoming Signal message (kind='message') or a reaction to one of
        your messages (kind='reaction'). It never fires for messages sent
        from your own linked devices."""
        if kind == 'reaction' and not self.config.get('notify_reactions', True):
            return
        # Same sound policy as ordinary chat messages, so the existing
        # sound_events / cooldown / focus configuration applies unchanged.
        await self._maybe_play_sound('conversation_msg')
        if await self._should_notify(tab):
            ident = self._get_tab_identifier(tab)
            if ident:
                self._mark_unread(ident)

    async def on_muc_msg(self, message, tab):
        log.debug("[i3blocks] on_muc_msg ENTER: body=%r, tab=%r", message['body'] if message else None, tab)
        if not message['body']:
            return
        if find_delayed_tag(message)[0]:
            return

        nick = getattr(message['from'], 'resource', None)
        if not nick and '/' in str(message['from']):
            nick = str(message['from']).split('/', 1)[1]
        if hasattr(tab, 'own_nick') and nick == tab.own_nick:
            return

        if await self._should_notify(tab):
            ident = self._get_tab_identifier(tab)
            if ident:
                self._mark_unread(ident)

    async def on_highlight(self, message, tab):
        if not message['body']:
            return
        if find_delayed_tag(message)[0]:
            return

        if await self._should_notify(tab):
            ident = self._get_tab_identifier(tab)
            if ident:
                self._mark_unread(ident)

    def on_tab_change(self, old_tab=None, new_tab=None, *args, **kwargs):
        # Resolve target whether passed as positional args, kwargs, or tab index
        target = new_tab if new_tab is not None else kwargs.get('new_tab')
        if target is None:
            if len(args) >= 2:
                target = args[1]
            elif len(args) == 1:
                target = args[0]
            elif old_tab is not None and new_tab is None:
                target = old_tab

        tab = None
        if isinstance(target, int):
            try:
                tab = self.core.tabs[target]
            except Exception:
                pass
        elif target is not None:
            tab = target

        if not tab:
            tab = self.api.current_tab()

        if tab:
            self._mark_read(tab)

        self._prune_closed_tabs()
