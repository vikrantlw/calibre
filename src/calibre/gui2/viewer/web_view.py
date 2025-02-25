#!/usr/bin/env python2
# vim:fileencoding=utf-8
# License: GPL v3 Copyright: 2018, Kovid Goyal <kovid at kovidgoyal.net>

from __future__ import absolute_import, division, print_function, unicode_literals

import os
import sys
from itertools import count

from PyQt5.Qt import (
    QApplication, QBuffer, QByteArray, QFontDatabase, QFontInfo, QHBoxLayout, QSize,
    Qt, QTimer, QUrl, QWidget, pyqtSignal
)
from PyQt5.QtWebEngineCore import QWebEngineUrlSchemeHandler
from PyQt5.QtWebEngineWidgets import (
    QWebEnginePage, QWebEngineProfile, QWebEngineScript, QWebEngineView
)

from calibre import as_unicode, prints
from calibre.constants import (
    FAKE_HOST, FAKE_PROTOCOL, __version__, is_running_from_develop, isosx, iswindows
)
from calibre.ebooks.metadata.book.base import field_metadata
from calibre.ebooks.oeb.polish.utils import guess_type
from calibre.gui2 import error_dialog, safe_open_url
from calibre.gui2.webengine import (
    Bridge, RestartingWebEngineView, create_script, from_js, insert_scripts,
    secure_webengine, to_js
)
from calibre.srv.code import get_translations_data
from calibre.utils.config import JSONConfig
from calibre.utils.serialize import json_loads
from polyglot.builtins import iteritems

try:
    from PyQt5 import sip
except ImportError:
    import sip

vprefs = JSONConfig('viewer-webengine')
vprefs.defaults['session_data'] = {}
vprefs.defaults['main_window_state'] = None
vprefs.defaults['main_window_geometry'] = None
vprefs.defaults['old_prefs_migrated'] = False


# Override network access to load data from the book {{{

def set_book_path(path, pathtoebook):
    set_book_path.pathtoebook = pathtoebook
    set_book_path.path = os.path.abspath(path)
    set_book_path.metadata = get_data('calibre-book-metadata.json')[0]
    set_book_path.manifest, set_book_path.manifest_mime = get_data('calibre-book-manifest.json')
    set_book_path.parsed_metadata = json_loads(set_book_path.metadata)
    set_book_path.parsed_manifest = json_loads(set_book_path.manifest)


def get_path_for_name(name):
    bdir = getattr(set_book_path, 'path', None)
    if bdir is None:
        return
    path = os.path.abspath(os.path.join(bdir, name))
    if path.startswith(bdir):
        return path


def get_data(name):
    path = get_path_for_name(name)
    if path is None:
        return None, None
    try:
        with lopen(path, 'rb') as f:
            return f.read(), guess_type(name)
    except EnvironmentError as err:
        prints('Failed to read from book file: {} with error: {}'.format(name, as_unicode(err)))
    return None, None


def send_reply(rq, mime_type, data):
    if sip.isdeleted(rq):
        return
    # make the buf a child of rq so that it is automatically deleted when
    # rq is deleted
    buf = QBuffer(parent=rq)
    buf.open(QBuffer.WriteOnly)
    # we have to copy data into buf as it will be garbage
    # collected by python
    buf.write(data)
    buf.seek(0)
    buf.close()
    rq.reply(mime_type.encode('ascii'), buf)


class UrlSchemeHandler(QWebEngineUrlSchemeHandler):

    def __init__(self, parent=None):
        QWebEngineUrlSchemeHandler.__init__(self, parent)
        self.mathjax_dir = P('mathjax', allow_user_override=False)
        self.mathjax_manifest = None

    def requestStarted(self, rq):
        if bytes(rq.requestMethod()) != b'GET':
            rq.fail(rq.RequestDenied)
            return
        url = rq.requestUrl()
        if url.host() != FAKE_HOST or url.scheme() != FAKE_PROTOCOL:
            rq.fail(rq.UrlNotFound)
            return
        name = url.path()[1:]
        if name.startswith('book/'):
            name = name.partition('/')[2]
            try:
                data, mime_type = get_data(name)
                if data is None:
                    rq.fail(rq.UrlNotFound)
                    return
                if isinstance(data, type('')):
                    data = data.encode('utf-8')
                mime_type = {
                    # Prevent warning in console about mimetype of fonts
                    'application/vnd.ms-opentype':'application/x-font-ttf',
                    'application/x-font-truetype':'application/x-font-ttf',
                    'application/font-sfnt': 'application/x-font-ttf',
                }.get(mime_type, mime_type)
                send_reply(rq, mime_type, data)
            except Exception:
                import traceback
                traceback.print_exc()
                rq.fail(rq.RequestFailed)
        elif name == 'manifest':
            data = b'[' + set_book_path.manifest + b',' + set_book_path.metadata + b']'
            send_reply(rq, set_book_path.manifest_mime, data)
        elif name.startswith('mathjax/'):
            from calibre.gui2.viewer.mathjax import monkeypatch_mathjax
            if name == 'mathjax/manifest.json':
                if self.mathjax_manifest is None:
                    import json
                    from calibre.srv.books import get_mathjax_manifest
                    self.mathjax_manifest = json.dumps(get_mathjax_manifest()['files'])
                    send_reply(rq, 'application/json', self.mathjax_manifest)
                    return
            path = os.path.abspath(os.path.join(self.mathjax_dir, '..', name))
            if path.startswith(self.mathjax_dir):
                mt = guess_type(name)
                try:
                    with lopen(path, 'rb') as f:
                        raw = f.read()
                except EnvironmentError as err:
                    prints("Failed to get mathjax file: {} with error: {}".format(name, err))
                    rq.fail(rq.RequestFailed)
                    return
                if 'MathJax.js' in name:
                    # raw = open(os.path.expanduser('~/work/mathjax/unpacked/MathJax.js')).read()
                    raw = monkeypatch_mathjax(raw.decode('utf-8')).encode('utf-8')

                send_reply(rq, mt, raw)
        elif not name:
            send_reply(rq, 'text/html', viewer_html())

# }}}


def get_session_pref(name, default=None, group='standalone_misc_settings'):
    sd = vprefs['session_data']
    g = sd.get(group, {})
    return g.get(name, default)


def create_profile():
    ans = getattr(create_profile, 'ans', None)
    if ans is None:
        ans = QWebEngineProfile(QApplication.instance())
        osname = 'windows' if iswindows else ('macos' if isosx else 'linux')
        ua = 'calibre-viewer {} {}'.format(__version__, osname)
        ans.setHttpUserAgent(ua)
        if is_running_from_develop:
            from calibre.utils.rapydscript import compile_viewer
            print('Compiling viewer code...')
            compile_viewer()
        js = P('viewer.js', data=True, allow_user_override=False)
        translations_json = get_translations_data() or b'null'
        js = js.replace(b'__TRANSLATIONS_DATA__', translations_json, 1)
        insert_scripts(ans, create_script('viewer.js', js))
        url_handler = UrlSchemeHandler(ans)
        ans.installUrlSchemeHandler(QByteArray(FAKE_PROTOCOL.encode('ascii')), url_handler)
        s = ans.settings()
        s.setDefaultTextEncoding('utf-8')
        s.setAttribute(s.LinksIncludedInFocusChain, False)
        create_profile.ans = ans
    return ans


class ViewerBridge(Bridge):

    set_session_data = from_js(object, object)
    reload_book = from_js()
    toggle_toc = from_js()
    toggle_bookmarks = from_js()
    toggle_inspector = from_js()
    toggle_lookup = from_js()
    update_current_toc_nodes = from_js(object, object)
    toggle_full_screen = from_js()
    report_cfi = from_js(object, object)
    ask_for_open = from_js(object)
    selection_changed = from_js(object)
    copy_selection = from_js(object)
    view_image = from_js(object)

    create_view = to_js()
    show_preparing_message = to_js()
    start_book_load = to_js()
    goto_toc_node = to_js()
    goto_cfi = to_js()
    full_screen_state_changed = to_js()
    get_current_cfi = to_js()
    show_home_page = to_js()


def apply_font_settings(page_or_view):
    s = page_or_view.settings()
    sd = vprefs['session_data']
    fs = sd.get('standalone_font_settings', {})
    if fs.get('serif_family'):
        s.setFontFamily(s.SerifFont, fs.get('serif_family'))
    else:
        s.resetFontFamily(s.SerifFont)
    if fs.get('sans_family'):
        s.setFontFamily(s.SansSerifFont, fs.get('sans_family'))
    else:
        s.resetFontFamily(s.SansSerifFont)
    if fs.get('mono_family'):
        s.setFontFamily(s.FixedFont, fs.get('mono_family'))
    else:
        s.resetFontFamily(s.SansSerifFont)
    sf = fs.get('standard_font') or 'serif'
    sf = getattr(s, {'serif': 'SerifFont', 'sans': 'SansSerifFont', 'mono': 'FixedFont'}[sf])
    s.setFontFamily(s.StandardFont, s.fontFamily(sf))
    mfs = fs.get('minimum_font_size')
    if mfs is None:
        s.resetFontSize(s.MinimumFontSize)
    else:
        s.setFontSize(s.MinimumFontSize, mfs)
    bfs = sd.get('base_font_size')
    if bfs is not None:
        s.setFontSize(s.DefaultFontSize, bfs)

    return s


class WebPage(QWebEnginePage):

    def __init__(self, parent):
        profile = create_profile()
        QWebEnginePage.__init__(self, profile, parent)
        profile.setParent(self)
        secure_webengine(self, for_viewer=True)
        apply_font_settings(self)
        self.bridge = ViewerBridge(self)
        self.bridge.copy_selection.connect(self.trigger_copy)

    def trigger_copy(self, what):
        if what:
            QApplication.instance().clipboard().setText(what)
        else:
            self.triggerAction(self.Copy)

    def javaScriptConsoleMessage(self, level, msg, linenumber, source_id):
        if level >= QWebEnginePage.ErrorMessageLevel and source_id == 'userscript:viewer.js':
            error_dialog(self.parent(), _('Unhandled error'), _(
                'There was an unhandled error: {} at line: {} of {}').format(
                    msg, linenumber, source_id.partition(':')[2]), show=True)
        prefix = {QWebEnginePage.InfoMessageLevel: 'INFO', QWebEnginePage.WarningMessageLevel: 'WARNING'}.get(
                level, 'ERROR')
        prints('%s: %s:%s: %s' % (prefix, source_id, linenumber, msg), file=sys.stderr)
        sys.stderr.flush()

    def acceptNavigationRequest(self, url, req_type, is_main_frame):
        if req_type == self.NavigationTypeReload:
            return True
        if req_type == self.NavigationTypeBackForward:
            return True
        if url.scheme() in (FAKE_PROTOCOL, 'data'):
            return True
        if url.scheme() in ('http', 'https'):
            safe_open_url(url)
        prints('Blocking navigation request to:', url.toString())
        return False

    def go_to_anchor(self, anchor):
        self.bridge.go_to_anchor.emit(anchor or '')

    def runjs(self, src, callback=None):
        if callback is None:
            self.runJavaScript(src, QWebEngineScript.ApplicationWorld)
        else:
            self.runJavaScript(src, QWebEngineScript.ApplicationWorld, callback)


def viewer_html():
    ans = getattr(viewer_html, 'ans', None)
    if ans is None:
        ans = viewer_html.ans = P('viewer.html', data=True, allow_user_override=False)
    return ans


class Inspector(QWidget):

    def __init__(self, dock_action, parent=None):
        QWidget.__init__(self, parent=parent)
        self.view_to_debug = parent
        self.view = None
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.dock_action = dock_action
        QTimer.singleShot(0, self.connect_to_dock)

    def connect_to_dock(self):
        ac = self.dock_action
        ac.toggled.connect(self.visibility_changed)
        if ac.isChecked():
            self.visibility_changed(True)

    def visibility_changed(self, visible):
        if visible and self.view is None:
            self.view = QWebEngineView(self.view_to_debug)
            self.view_to_debug.page().setDevToolsPage(self.view.page())
            self.layout.addWidget(self.view)

    def sizeHint(self):
        return QSize(600, 1200)


class WebView(RestartingWebEngineView):

    cfi_changed = pyqtSignal(object)
    reload_book = pyqtSignal()
    toggle_toc = pyqtSignal()
    toggle_bookmarks = pyqtSignal()
    toggle_inspector = pyqtSignal()
    toggle_lookup = pyqtSignal()
    update_current_toc_nodes = pyqtSignal(object, object)
    toggle_full_screen = pyqtSignal()
    ask_for_open = pyqtSignal(object)
    selection_changed = pyqtSignal(object)
    view_image = pyqtSignal(object)

    def __init__(self, parent=None):
        self._host_widget = None
        self.callback_id_counter = count()
        self.callback_map = {}
        self.current_cfi = None
        RestartingWebEngineView.__init__(self, parent)
        self.dead_renderer_error_shown = False
        self.render_process_failed.connect(self.render_process_died)
        w = QApplication.instance().desktop().availableGeometry(self).width()
        self._size_hint = QSize(int(w/3), int(w/2))
        self._page = WebPage(self)
        self.bridge.bridge_ready.connect(self.on_bridge_ready)
        self.bridge.set_session_data.connect(self.set_session_data)
        self.bridge.reload_book.connect(self.reload_book)
        self.bridge.toggle_toc.connect(self.toggle_toc)
        self.bridge.toggle_bookmarks.connect(self.toggle_bookmarks)
        self.bridge.toggle_inspector.connect(self.toggle_inspector)
        self.bridge.toggle_lookup.connect(self.toggle_lookup)
        self.bridge.update_current_toc_nodes.connect(self.update_current_toc_nodes)
        self.bridge.toggle_full_screen.connect(self.toggle_full_screen)
        self.bridge.ask_for_open.connect(self.ask_for_open)
        self.bridge.selection_changed.connect(self.selection_changed)
        self.bridge.view_image.connect(self.view_image)
        self.bridge.report_cfi.connect(self.call_callback)
        self.pending_bridge_ready_actions = {}
        self.setPage(self._page)
        self.setAcceptDrops(False)
        self.setUrl(QUrl('{}://{}/'.format(FAKE_PROTOCOL, FAKE_HOST)))
        self.urlChanged.connect(self.url_changed)
        if parent is not None:
            self.inspector = Inspector(parent.inspector_dock.toggleViewAction(), self)
            parent.inspector_dock.setWidget(self.inspector)

    def url_changed(self, url):
        if url.hasFragment():
            frag = url.fragment(url.FullyDecoded)
            if frag and frag.startswith('bookpos='):
                cfi = frag[len('bookpos='):]
                if cfi:
                    self.current_cfi = cfi
                    self.cfi_changed.emit(cfi)

    @property
    def host_widget(self):
        ans = self._host_widget
        if ans is not None and not sip.isdeleted(ans):
            return ans

    def change_zoom_by(self, steps=1):
        # TODO: Add UI for this
        ss = vprefs['session_data'].get('zoom_step_size') or 20
        amt = (ss / 100) * steps
        self._page.setZoomFactor(self._page.zoomFactor() + amt)

    def render_process_died(self):
        if self.dead_renderer_error_shown:
            return
        self.dead_renderer_error_shown = True
        error_dialog(self, _('Render process crashed'), _(
            'The Qt WebEngine Render process has crashed.'
            ' You should try restarting the viewer.') , show=True)

    def event(self, event):
        if event.type() == event.ChildPolished:
            child = event.child()
            if 'HostView' in child.metaObject().className():
                self._host_widget = child
                self._host_widget.setFocus(Qt.OtherFocusReason)
        return QWebEngineView.event(self, event)

    def sizeHint(self):
        return self._size_hint

    def refresh(self):
        self.pageAction(QWebEnginePage.ReloadAndBypassCache).trigger()

    @property
    def bridge(self):
        return self._page.bridge

    def on_bridge_ready(self):
        f = QApplication.instance().font()
        fi = QFontInfo(f)
        self.bridge.create_view(
            vprefs['session_data'], QFontDatabase().families(), field_metadata.all_metadata(),
            f.family(), '{}px'.format(fi.pixelSize()))
        for func, args in iteritems(self.pending_bridge_ready_actions):
            getattr(self.bridge, func)(*args)

    def start_book_load(self, initial_cfi=None, initial_toc_node=None):
        key = (set_book_path.path,)
        self.execute_when_ready('start_book_load', key, initial_cfi, initial_toc_node, set_book_path.pathtoebook)

    def execute_when_ready(self, action, *args):
        if self.bridge.ready:
            getattr(self.bridge, action)(*args)
        else:
            self.pending_bridge_ready_actions[action] = args

    def show_preparing_message(self):
        msg = _('Preparing book for first read, please wait') + '…'
        self.execute_when_ready('show_preparing_message', msg)

    def goto_toc_node(self, node_id):
        self.execute_when_ready('goto_toc_node', node_id)

    def goto_cfi(self, cfi):
        self.execute_when_ready('goto_cfi', cfi)

    def notify_full_screen_state_change(self, in_fullscreen_mode):
        self.execute_when_ready('full_screen_state_changed', in_fullscreen_mode)

    def set_session_data(self, key, val):
        if key == '*' and val is None:
            vprefs['session_data'] = {}
            apply_font_settings(self._page)
        elif key != '*':
            sd = vprefs['session_data']
            sd[key] = val
            vprefs['session_data'] = sd
            if key in ('standalone_font_settings', 'base_font_size'):
                apply_font_settings(self._page)

    def do_callback(self, func_name, callback):
        cid = next(self.callback_id_counter)
        self.callback_map[cid] = callback
        self.execute_when_ready('get_current_cfi', cid)

    def call_callback(self, request_id, data):
        callback = self.callback_map.pop(request_id, None)
        if callback is not None:
            callback(data)

    def get_current_cfi(self, callback):
        self.do_callback('get_current_cfi', callback)

    def show_home_page(self):
        self.execute_when_ready('show_home_page')
