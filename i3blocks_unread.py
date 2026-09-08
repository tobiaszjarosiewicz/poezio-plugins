import atexit
import os
import subprocess

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

# Set to True to notify on every message in a MUC.
# Set to False if you only want notifications when mentioned/highlighted.
NOTIFY_ALL_MUC = True


class Plugin(BasePlugin):
    def init(self):
        self._unread = set()
        self._load()

        # Catch tabs already unread when the plugin is loaded
        if hasattr(self.core, 'tabs'):
            for tab in self.core.tabs:
                if tab and getattr(tab, 'state', None) in ('message', 'highlight', 'private', 'attention'):
                    ident = self._get_tab_identifier(tab)
                    if ident:
                        self._unread.add(ident)

        self._save_and_signal()

        # Message hooks
        self.api.add_event_handler('conversation_msg', self.on_conversation_msg)
        self.api.add_event_handler('private_msg', self.on_private_msg)

        if NOTIFY_ALL_MUC:
            self.api.add_event_handler('muc_msg', self.on_muc_msg)
        else:
            self.api.add_event_handler('highlight', self.on_highlight)

        # Tab focus hook
        self.api.add_event_handler('tab_change', self.on_tab_change)

        atexit.register(self.cleanup)

    def cleanup(self):
        try:
            atexit.unregister(self.cleanup)
        except Exception:
            pass
        self._unread.clear()
        self._save_and_signal()

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

    def on_conversation_msg(self, message, tab):
        if not message['body']:
            return
        if find_delayed_tag(message)[0]:
            return
        try:
            if message['from'].bare == self.core.xmpp.boundjid.bare:
                return
        except Exception:
            pass

        if tab != self.api.current_tab():
            ident = self._get_tab_identifier(tab)
            if ident:
                self._mark_unread(ident)

    def on_private_msg(self, message, tab):
        if not message['body']:
            return
        if find_delayed_tag(message)[0]:
            return

        if tab != self.api.current_tab():
            ident = self._get_tab_identifier(tab)
            if ident:
                self._mark_unread(ident)

    def on_muc_msg(self, message, tab):
        if not message['body']:
            return
        if find_delayed_tag(message)[0]:
            return

        nick = getattr(message['from'], 'resource', None)
        if not nick and '/' in str(message['from']):
            nick = str(message['from']).split('/', 1)[1]
        if hasattr(tab, 'own_nick') and nick == tab.own_nick:
            return

        if tab != self.api.current_tab():
            ident = self._get_tab_identifier(tab)
            if ident:
                self._mark_unread(ident)

    def on_highlight(self, message, tab):
        if not message['body']:
            return
        if find_delayed_tag(message)[0]:
            return

        if tab != self.api.current_tab():
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
