"""Focused contract tests for the backend-aware GE follow-up page.

Node.js is an intentional test dependency: a missing runtime is a gate failure, not a skip.
The fake DOM exercises DOM API usage; the static sink assertions below enforce that chat data
never reaches an HTML-parsing sink.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest

from client.popup import launcher


_SVG = "<svg xmlns=\"http://www.w3.org/2000/svg\"><rect width=\"10\" height=\"10\"/></svg>"

_VALID_PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP8zwACTGCSAQANHQEDgslx/wAAAABJRU5ErkJggg=="
)
_VALID_JPEG_DATA_URL = (
    "data:image/jpeg;base64,"
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIs"
    "IxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy"
    "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAACAAIDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQF"
    "BgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkK"
    "FhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZ"
    "mqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEB"
    "AQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFE"
    "KRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6"
    "goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09f"
    "b3+Pn6/9oADAMBAAIRAxEAPwDWooor80PyA//Z"
)
_VALID_WEBP_DATA_URL = (
    "data:image/webp;base64,"
    "UklGRjoAAABXRUJQVlA4IC4AAACQAQCdASoCAAIAAUAmJaACdLoAA5gA/vtV4/+lwf/S4P/pcH/pcH8bss4bpAAA"
)


def _render(title: str = "GE chat", locale: str = "en-US") -> str:
    # Chrome language now comes from the resolver via payload ui_locale (not the title). Pin it so
    # these page-contract assertions are deterministic on any host locale.
    return launcher.render_artifact_html(launcher.PopupSpec(
        kind="ge",
        title=title,
        artifact={"kind": "svg", "data": _SVG},
        payload={"ui_locale": locale},
    ))


_NODE_HARNESS = r"""
const createdTags = [];
class FakeClassList {
  constructor(node) { this.node = node; this.values = new Set(); }
  _sync() { this.node.className = Array.from(this.values).join(' '); }
  add(value) { this.values.add(value); this._sync(); }
  remove(value) { this.values.delete(value); this._sync(); }
  contains(value) { return this.values.has(value); }
}
class FakeNode {
  constructor(tag, id) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.id = id || '';
    this.className = '';
    this.children = [];
    this.listeners = {};
    this.dataset = {};
    this.disabled = false;
    this.hidden = false;
    this.value = '';
    this.placeholder = '';
    this.src = '';
    this.type = '';
    this.style = {};
    this.attributes = {};
    this.parentNode = null;
    this.scrollTop = 0;
    this.scrollHeight = 100;
    this.classList = new FakeClassList(this);
    this._text = '';
  }
  set textContent(value) { this._text = String(value == null ? '' : value); this.children = []; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(''); }
  get firstChild() { return this.children[0] || null; }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  removeChild(child) { const i = this.children.indexOf(child); if (i >= 0) this.children.splice(i, 1); }
  setAttribute(name, value) {
    name = String(name); value = String(value); this.attributes[name] = value;
    if (name === 'src') this.src = value;
  }
  getAttribute(name) {
    name = String(name);
    return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
  }
  removeAttribute(name) {
    name = String(name); delete this.attributes[name];
    if (name === 'src') this.src = '';
  }
  addEventListener(type, callback) { (this.listeners[type] || (this.listeners[type] = [])).push(callback); }
  dispatchEvent(event) {
    event = event || {};
    event.target = event.target || this;
    event.preventDefault = event.preventDefault || function () { event.defaultPrevented = true; };
    event.stopImmediatePropagation = event.stopImmediatePropagation || function () { event.immediateStopped = true; };
    const callbacks = (this.listeners[event.type] || []).slice();
    for (const callback of callbacks) {
      callback(event);
      if (event.immediateStopped) break;
    }
    return !event.defaultPrevented;
  }
  click() { this.dispatchEvent({type: 'click'}); if (typeof this.onclick === 'function') this.onclick({target: this}); }
  focus() { this.focused = true; }
  getBoundingClientRect() { return {left: 10, top: 20, width: 200, height: 100}; }
}
const ids = [
  'ge-chat-scroll', 'ge-composer', 'composer-input', 'composer-send', 'ge-imgchips',
  'ge-chat-status', 'ge-chat-status-text', 'ge-retry', 'ge-input-error', 'region-btn',
  'ge-i18n', 'ge-welcome', 'ge-image-preview', 'ge-image-preview-image',
  'ge-image-preview-close'
];
const nodes = {};
ids.forEach((id) => {
  const tag = id === 'composer-input' ? 'textarea'
    : id === 'ge-image-preview-image' ? 'img'
      : id === 'ge-image-preview-close' ? 'button' : 'div';
  nodes[id] = new FakeNode(tag, id);
});
nodes['composer-input'].disabled = true;
nodes['composer-send'].disabled = true;
nodes['region-btn'].disabled = true;
nodes['ge-image-preview'].hidden = true;
nodes['ge-i18n'].dataset.welcome = 'Welcome';
nodes['ge-i18n'].dataset.placeholder = 'Ask';
nodes['ge-welcome'].textContent = 'Welcome';
nodes['ge-chat-scroll'].appendChild(nodes['ge-welcome']);
const documentListeners = {};
const document = {
  documentElement: {lang: 'en'},
  getElementById(id) { return nodes[id] || null; },
  createElement(tag) { createdTags.push(String(tag).toLowerCase()); return new FakeNode(tag); },
  createTextNode(text) { const node = new FakeNode('#text'); node.textContent = text; return node; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener(type, callback) { (documentListeners[type] || (documentListeners[type] = [])).push(callback); },
  dispatchEvent(event) {
    event = event || {};
    event.preventDefault = event.preventDefault || function () { event.defaultPrevented = true; };
    event.stopImmediatePropagation = event.stopImmediatePropagation || function () { event.immediateStopped = true; };
    const callbacks = (documentListeners[event.type] || []).slice();
    for (const callback of callbacks) {
      callback(event);
      if (event.immediateStopped) break;
    }
    return !event.defaultPrevented;
  }
};
const calls = [];
const api = {
  chat_ready() { calls.push(['chat_ready']); return {ok: true, state: 'capability_checking'}; },
  ask() { calls.push(['ask'].concat(Array.from(arguments))); return {ok: true}; },
  retry_chat() { calls.push(['retry_chat'].concat(Array.from(arguments))); return {ok: true, status: 'recovering'}; },
  close() { calls.push(['close']); return {ok: true}; }
};
const window = {
  document,
  pywebview: {api},
  crypto: {
    randomUUID() { return '123e4567-e89b-42d3-a456-426614174000'; },
    getRandomValues(bytes) { for (let i = 0; i < bytes.length; i += 1) bytes[i] = i; return bytes; }
  },
  addEventListener() {}
};
global.document = document;
global.window = window;
const pendingReads = [];
const readFiles = [];
global.FileReader = class {
  readAsDataURL(file) {
    readFiles.push(file);
    this.result = file.dataUrl;
    pendingReads.push(() => { if (typeof this.onload === 'function') this.onload(); });
  }
};
function flushReaders() { while (pendingReads.length) pendingReads.shift()(); }
const scheduledTimeouts = [];
global.setTimeout = function (callback) { scheduledTimeouts.push(callback); return scheduledTimeouts.length; };
global.clearTimeout = function (handle) { if (handle > 0) scheduledTimeouts[handle - 1] = null; };
function runTimeouts() {
  const current = scheduledTimeouts.splice(0, scheduledTimeouts.length);
  current.forEach((callback) => { if (typeof callback === 'function') callback(); });
}
const scheduledFrames = [];
global.requestAnimationFrame = function (callback) { scheduledFrames.push(callback); return scheduledFrames.length; };
global.cancelAnimationFrame = function (handle) { if (handle > 0) scheduledFrames[handle - 1] = null; };
function runAnimationFrames() {
  const current = scheduledFrames.splice(0, scheduledFrames.length);
  current.forEach((callback) => { if (typeof callback === 'function') callback(); });
}
global.setInterval = function () { return 1; };
global.clearInterval = function () {};
function finish(value) {
  Promise.resolve().then(() => setImmediate(() => process.stdout.write(JSON.stringify(value))));
}
"""


def _run_js(
    scenario: str,
    *,
    language: str = "en",
    include_chrome: bool = False,
    preamble: str = "",
) -> dict:
    # Language is now decided in Python: inject the resolved-locale string dict into the JS (the raw
    # constants hold a __GE_*_STRINGS__ sentinel that is not valid JS on its own). <html lang> is still
    # set for realism, but the JS no longer reads it to choose a language.
    locale = "zh-CN" if str(language).lower().startswith("zh") else "en-US"
    completed = subprocess.run(
        ["node", "-"],
        input=(
            _NODE_HARNESS
            + "\ndocument.documentElement.lang = "
            + json.dumps(language)
            + ";\n"
            + "const TEST_VALID_PNG = "
            + json.dumps(_VALID_PNG_DATA_URL)
            + ";\nconst TEST_VALID_JPEG = "
            + json.dumps(_VALID_JPEG_DATA_URL)
            + ";\nconst TEST_VALID_WEBP = "
            + json.dumps(_VALID_WEBP_DATA_URL)
            + ";\n"
            + preamble
            + "\n"
            + launcher.ge_chat_js(locale)
            + ("\n" + launcher.ge_chrome_js(locale) if include_chrome else "")
            + "\n"
            + scenario
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr)
    return json.loads(completed.stdout)


class HttpChatShellCommandTestCase(unittest.TestCase):
    def test_chat_bridge_stdin_flag_is_explicit_and_value_free(self):
        command = launcher.build_shell_command(
            sys.executable,
            "popup.html",
            "GE chat",
            "result.json",
            chat_bridge_stdin=True,
        )

        self.assertEqual(command[-1], "--chat-bridge-stdin")
        self.assertNotIn("--chat-bridge-stdin", launcher.build_shell_command(
            sys.executable,
            "popup.html",
            "GE chat",
            "result.json",
        ))


class HttpChatPageContractTestCase(unittest.TestCase):
    def test_bootstrap_state_callbacks_gate_ready_and_exclude_sensitive_surfaces(self):
        page = _render()
        script = launcher._GE_CHAT_JS

        for callback in (
            "bootstrap:", "state:", "delta:", "done:", "error:",
            "willClose:", "isBusy:", "submit:", "addImg:",
        ):
            self.assertIn(callback, script)
        for element_id in (
            "ge-chat-status", "ge-chat-status-text", "ge-retry",
        ):
            self.assertIn('id="%s"' % element_id, page)
        for css_marker in (
            ".ge-chat-status{", ".ge-chat-status-text{",
            ".ge-retry{", ".ge-composer-row{display:flex;flex-direction:column;border:1px solid #c9bd97;",
            "padding:12px 12px 28px 12px;scrollbar-width:none}",
            "#composer-send{align-self:flex-end;flex:none;height:28px;padding:0 12px;",
            ".imgchip-preview{",
            ".ge-price{",
        ):
            self.assertIn(css_marker, page)
        self.assertNotIn(".ge-server-actions{", page)

        self.assertNotIn("if (ready) enableChat()", script)
        for forbidden in (
            "device_token", "endpoint", "run_id", "conversation_code",
            "turn_code", "localStorage", "sessionStorage", "Math.random",
            ".innerHTML", "insertAdjacentHTML", "document.write",
        ):
            self.assertNotIn(forbidden, script)

    def test_server_chat_has_no_privacy_delete_or_cancel_controls_and_sends_immediately(self):
        page = _render()
        for removed in (
            'id="ge-privacy"',
            'id="ge-privacy-confirm"',
            "Privacy disclosure",
            "Confirm and continue",
            'id="ge-delete"',
            "Delete conversation",
            "Confirm delete",
            'id="ge-cancel"',
            "window.pywebview.api.cancel_chat",
        ):
            self.assertNotIn(removed, page)

        result = _run_js(r"""
          const ok = window.geChat.bootstrap({
            ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:no-ui', providers: [{
              category: 'safe', data_region: 'safe', training_enabled: false,
              cache_ttl_seconds: 0, provider_retention_hours: 0, deletion_scope: 'safe'
            }]},
            history: []
          });
          const inputEnabled = !nodes['composer-input'].disabled;
          const sent = window.geChat.submit('send without consent UI', [], null);
          finish({ok, inputEnabled, sent, calls});
        """)

        self.assertTrue(result["ok"])
        self.assertTrue(result["inputEnabled"])
        self.assertTrue(result["sent"])
        asks = [call for call in result["calls"] if call[0] == "ask"]
        self.assertEqual(len(asks), 1)
        self.assertEqual(asks[0][5], "v:no-ui")

    def test_server_history_is_sorted_and_disclosure_is_validated_without_rendering(self):
        result = _run_js(r"""
          const malicious = '<img src=x onerror=alert(1)><script>owned()</script>';
          const maliciousReply = '![click](javascript:owned()) <a href="javascript:owned()" onclick="owned()">x</a>' +
            '</textarea><script>reply()</script><iframe srcdoc="<script>owned()</script>"></iframe>';
          const ok = window.geChat.bootstrap({
            ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 2, max_turns: 40, expires_at: 1787587200},
            privacy_disclosure: {version: 'sha256:v1', providers: [{
              category: '<svg onload=owned()>', data_region: 'approved-region', training_enabled: false,
              cache_ttl_seconds: 0, provider_retention_hours: 0, deletion_scope: 'contract-defined',
              model: 'SENTINEL-MODEL', api_key: 'SENTINEL-KEY', raw_body: '<script>RAW-PROVIDER</script>'
            }]},
            history: [
              {turn_no: 2, status: 'failed', user_text: 'second', assistant_text: null,
               error_code: 'model_unavailable', completed_at: 2, images: []},
              {turn_no: 1, status: 'completed', user_text: malicious, assistant_text: maliciousReply,
               error_code: null, completed_at: 1, images: [{image_index: 0, media_type: 'image/png',
                 size_bytes: 9, sha256: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'}]}
            ]
          });
          const messages = nodes['ge-chat-scroll'].children
            .filter((node) => node.className.indexOf('msg ') === 0)
            .map((node) => ({cls: node.className, text: node.textContent}));
          finish({ok, messages, inputDisabled: nodes['composer-input'].disabled,
                  createdActiveTags: createdTags.filter((tag) =>
                    ['a', 'iframe', 'img', 'script', 'style', 'svg'].indexOf(tag) !== -1), calls});
        """)

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["messages"],
            [
                {"cls": "msg user", "text": "<img src=x onerror=alert(1)><script>owned()</script>📎 1"},
                {
                    "cls": "msg bot",
                    "text": (
                        "![click](javascript:owned()) <a href=\"javascript:owned()\" "
                        "onclick=\"owned()\">x</a></textarea><script>reply()</script>"
                        "<iframe srcdoc=\"<script>owned()</script>\"></iframe>"
                    ),
                },
                {"cls": "msg user", "text": "second"},
                {"cls": "msg err", "text": "The model is temporarily unavailable."},
            ],
        )
        self.assertEqual(result["createdActiveTags"], [])
        serialized_calls = json.dumps(result["calls"])
        self.assertNotIn("SENTINEL-MODEL", serialized_calls)
        self.assertNotIn("SENTINEL-KEY", serialized_calls)
        self.assertNotIn("RAW-PROVIDER", serialized_calls)
        self.assertFalse(result["inputDisabled"])
        self.assertEqual(result["createdActiveTags"], [])

    def test_assistant_markdown_renders_for_history_while_user_text_stays_plain(self):
        result = _run_js(r"""
          const userText = '# user **literal** [not a link](https://user.example/)';
          const answer = '# Heading\n\n**bold** and *emphasis* and `inline`\n\n' +
            '- first\n- second\n\n> quoted\n\n```js\nconst value = "<tag>";\n```\n\n' +
            '[docs](https://example.com/path?q=1)';
          const ok = window.geChat.bootstrap({
            ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 1, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:markdown-history', providers: [{category: 'safe',
              data_region: 'safe', training_enabled: false, cache_ttl_seconds: 0,
              provider_retention_hours: 0, deletion_scope: 'safe'}]},
            history: [{turn_no: 1, status: 'completed', user_text: userText,
              assistant_text: answer, error_code: null, completed_at: 1, images: []}]
          });
          const messages = nodes['ge-chat-scroll'].children
            .filter((node) => node.className.indexOf('msg ') === 0);
          function descendants(node, output) {
            node.children.forEach((child) => { output.push(child); descendants(child, output); });
            return output;
          }
          const botNodes = descendants(messages[1], []);
          const link = botNodes.find((node) => node.tagName === 'A');
          finish({ok, userText: messages[0].textContent, userChildren: messages[0].children.length,
            botText: messages[1].textContent, botTags: botNodes.map((node) => node.tagName),
            link: link && {href: link.href, target: link.target, rel: link.rel}});
        """)

        self.assertTrue(result["ok"])
        self.assertEqual(result["userText"], "# user **literal** [not a link](https://user.example/)")
        self.assertEqual(result["userChildren"], 0)
        for tag in ("H1", "STRONG", "EM", "CODE", "UL", "LI", "BLOCKQUOTE", "PRE", "A"):
            self.assertIn(tag, result["botTags"])
        self.assertIn('const value = "<tag>";', result["botText"])
        self.assertEqual(
            result["link"],
            {
                "href": "https://example.com/path?q=1",
                "target": "_blank",
                "rel": "noopener noreferrer",
            },
        )

    def test_live_markdown_allows_safe_links_and_keeps_active_content_inert(self):
        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:markdown-live', providers: [{category: 'safe',
              data_region: 'safe', training_enabled: false, cache_ttl_seconds: 0,
              provider_retention_hours: 0, deletion_scope: 'safe'}]}, history: []});
          window.geChat.submit('**user stays plain**', [], null);
          function descendants(node, output) {
            node.children.forEach((child) => { output.push(child); descendants(child, output); });
            return output;
          }
          function snapshot() {
            const messages = nodes['ge-chat-scroll'].children
              .filter((node) => node.className.indexOf('msg ') === 0);
            const bot = messages[messages.length - 1];
            const all = descendants(bot, []);
            return {text: bot.textContent, tags: all.map((node) => node.tagName),
              links: all.filter((node) => node.tagName === 'A').map((node) => node.href),
              userChildren: messages[0].children.length};
          }
          window.geChat.delta(1, '## Live\n[safe](https://example.org/x) ' +
            '[script](javascript:owned()) [data](data:text/html,owned) [relative](/local) ' +
            '![pixel](https://tracker.example/pixel.png) <img src=x onerror=owned()>');
          runAnimationFrames();
          const during = snapshot();
          window.geChat.done(1, '**Final** [ok](http://example.com/) ' +
            '[bad](javascript:owned()) <script>owned()</script>');
          while (scheduledFrames.filter(Boolean).length) runAnimationFrames();
          finish({during, final: snapshot()});
        """)

        self.assertEqual(result["during"]["userChildren"], 0)
        self.assertIn("H2", result["during"]["tags"])
        self.assertEqual(result["during"]["links"], ["https://example.org/x"])
        self.assertIn("![pixel](https://tracker.example/pixel.png)", result["during"]["text"])
        self.assertIn("<img src=x onerror=owned()>", result["during"]["text"])
        self.assertFalse({"IMG", "SCRIPT", "IFRAME", "STYLE", "SVG"} & set(result["during"]["tags"]))
        self.assertIn("STRONG", result["final"]["tags"])
        self.assertEqual(result["final"]["links"], ["http://example.com/"])
        self.assertIn("[bad](javascript:owned())", result["final"]["text"])
        self.assertIn("<script>owned()</script>", result["final"]["text"])
        self.assertFalse({"IMG", "SCRIPT", "IFRAME", "STYLE", "SVG"} & set(result["final"]["tags"]))

    def test_pathological_markdown_node_count_falls_back_to_plain_text(self):
        result = _run_js(r"""
          const answer = '*x*'.repeat(5000);
          const ok = window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 1, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:markdown-budget', providers: [{category: 'safe',
              data_region: 'safe', training_enabled: false, cache_ttl_seconds: 0,
              provider_retention_hours: 0, deletion_scope: 'safe'}]},
            history: [{turn_no: 1, status: 'completed', user_text: 'stress',
              assistant_text: answer, error_code: null, completed_at: 1, images: []}]});
          const bot = nodes['ge-chat-scroll'].children
            .filter((node) => node.className === 'msg bot')[0];
          function count(node) {
            return node.children.reduce((total, child) => total + 1 + count(child), 0);
          }
          finish({ok, exactText: bot.textContent === answer, descendantCount: count(bot),
            fallbackClass: bot.children[0] && bot.children[0].className});
        """)

        self.assertTrue(result["ok"])
        self.assertTrue(result["exactText"])
        self.assertLessEqual(result["descendantCount"], 2)
        self.assertEqual(result["fallbackClass"], "markdown-fallback")

    def test_markdown_budget_normalizes_cr_and_limits_renderer_input(self):
        result = _run_js(r"""
          function envelope(history, version) {
            return {ok: true, backend: 'server', state: 'ready',
              conversation: {status: 'active', turn_count: history.length, max_turns: 40, expires_at: 1},
              privacy_disclosure: {version, providers: [{category: 'safe', data_region: 'safe',
                training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
                deletion_scope: 'safe'}]}, history};
          }
          function descendants(node) {
            return node.children.reduce((total, child) => total + 1 + descendants(child), 0);
          }
          const crAnswer = Array(5001).fill('- x').join('\r');
          window.geChat.bootstrap(envelope([{turn_no: 1, status: 'completed', user_text: 'cr budget',
            assistant_text: crAnswer, error_code: null, completed_at: 1, images: []}], 'v:cr-budget'));
          let bot = nodes['ge-chat-scroll'].children.filter((node) => node.className === 'msg bot')[0];
          const cr = {exact: bot.textContent === crAnswer, descendants: descendants(bot),
            fallback: bot.children[0] && bot.children[0].className};

          window.geChat.bootstrap(envelope([], 'v:length-budget'));
          window.geChat.submit('length budget', [], null);
          const longAnswer = 'x'.repeat(400001);
          window.geChat.delta(1, longAnswer); runAnimationFrames();
          bot = nodes['ge-chat-scroll'].children.filter((node) => node.className === 'msg bot')[0];
          finish({cr, long: {exact: bot.textContent === longAnswer, descendants: descendants(bot),
            fallback: bot.children[0] && bot.children[0].className}});
        """)

        for case in (result["cr"], result["long"]):
            self.assertTrue(case["exact"])
            self.assertLessEqual(case["descendants"], 2)
            self.assertEqual(case["fallback"], "markdown-fallback")

    def test_markdown_semantic_edges_and_missing_url_support_fail_closed(self):
        result = _run_js(r"""
          const answer = '# C#\n# Closed ###\n\n3. third\n4. fourth\n\n' +
            '> quote one\n> quote two\n\n\\*literal\\*\n\n~~~js\n<code>\n~~~\n\n~~~\nunclosed';
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 1, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:semantic-edges', providers: [{category: 'safe',
              data_region: 'safe', training_enabled: false, cache_ttl_seconds: 0,
              provider_retention_hours: 0, deletion_scope: 'safe'}]},
            history: [{turn_no: 1, status: 'completed', user_text: 'edges',
              assistant_text: answer, error_code: null, completed_at: 1, images: []}]});
          let bot = nodes['ge-chat-scroll'].children.filter((node) => node.className === 'msg bot')[0];
          function descendants(node, output) {
            node.children.forEach((child) => { output.push(child); descendants(child, output); });
            return output;
          }
          let all = descendants(bot, []);
          const headings = all.filter((node) => node.tagName === 'H1').map((node) => node.textContent);
          const ordered = all.find((node) => node.tagName === 'OL');
          const semantic = {headings, orderedStart: Number.isInteger(ordered && ordered.start) ? ordered.start : null,
            orderedItems: ordered ? ordered.children.map((node) => node.textContent) : [],
            blockquote: (all.find((node) => node.tagName === 'BLOCKQUOTE') || {}).textContent,
            codeBlocks: all.filter((node) => node.tagName === 'PRE').map((node) => node.textContent),
            text: bot.textContent, emphasisCount: all.filter((node) => node.tagName === 'EM').length};

          const guardedLinks = '[credentials](https://user:pass@example.com/) ' +
            '[tab](https://exa\tmple.com/) [long](https://example.com/' + 'a'.repeat(2050) + ') ' +
            '[ok](https://example.com/)';
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 1, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:guarded-links', providers: [{category: 'safe',
              data_region: 'safe', training_enabled: false, cache_ttl_seconds: 0,
              provider_retention_hours: 0, deletion_scope: 'safe'}]},
            history: [{turn_no: 1, status: 'completed', user_text: 'guard links',
              assistant_text: guardedLinks, error_code: null, completed_at: 1, images: []}]});
          bot = nodes['ge-chat-scroll'].children.filter((node) => node.className === 'msg bot')[0];
          all = descendants(bot, []);
          const guarded = {text: bot.textContent,
            links: all.filter((node) => node.tagName === 'A').map((node) => node.href)};

          global.URL = undefined;
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 1, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:no-url', providers: [{category: 'safe', data_region: 'safe',
              training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
              deletion_scope: 'safe'}]},
            history: [{turn_no: 1, status: 'completed', user_text: 'link',
              assistant_text: '[safe](https://example.com/) [tab](https://exa\tmple.com/)',
              error_code: null, completed_at: 1, images: []}]});
          bot = nodes['ge-chat-scroll'].children.filter((node) => node.className === 'msg bot')[0];
          all = descendants(bot, []);
          finish({semantic, guarded, noUrl: {text: bot.textContent,
            anchors: all.filter((node) => node.tagName === 'A').length}});
        """)

        self.assertEqual(result["semantic"]["headings"], ["C#", "Closed"])
        self.assertEqual(result["semantic"]["orderedStart"], 3)
        self.assertEqual(result["semantic"]["orderedItems"], ["third", "fourth"])
        self.assertEqual(result["semantic"]["blockquote"], "quote onequote two")
        self.assertEqual(result["semantic"]["codeBlocks"], ["<code>", "unclosed"])
        self.assertIn("*literal*", result["semantic"]["text"])
        self.assertEqual(result["semantic"]["emphasisCount"], 0)
        self.assertEqual(result["guarded"]["links"], ["https://example.com/"])
        self.assertIn("[credentials](https://user:pass@example.com/)", result["guarded"]["text"])
        self.assertIn("[tab](https://exa\tmple.com/)", result["guarded"]["text"])
        self.assertIn("[long](https://example.com/", result["guarded"]["text"])
        self.assertEqual(result["noUrl"]["anchors"], 0)
        self.assertIn("[safe](https://example.com/)", result["noUrl"]["text"])
        self.assertIn("[tab](https://exa\tmple.com/)", result["noUrl"]["text"])
        self.assertIn(
            ".msg.bot p,.msg.bot li,.msg.bot blockquote{white-space:pre-wrap}",
            _render(),
        )

    def test_pending_assistant_uses_animated_dots_and_terminal_paths_remove_them(self):
        rendered = _render()
        animation_rule = (
            ".typing-dot{display:inline-block;"
            "animation:ge-typing-dot 1.2s ease-in-out infinite}"
        )
        motion_override = (
            "@media (prefers-reduced-motion:reduce){"
            ".typing-dot,.status-dot{animation:none;opacity:1;transform:none}}"
        )
        self.assertEqual(rendered.count(animation_rule), 1)
        self.assertEqual(rendered.count("@keyframes ge-typing-dot"), 1)
        self.assertEqual(rendered.count(motion_override), 1)
        self.assertLess(rendered.index(animation_rule), rendered.index(motion_override))
        self.assertIn(
            'id="ge-chat-status" class="ge-chat-status" role="status"',
            rendered,
        )

        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:typing', providers: [{category: 'safe', data_region: 'safe',
              training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
              deletion_scope: 'safe'}]}, history: []});
          const turnIds = ['123e4567-e89b-42d3-a456-426614174001',
                           '123e4567-e89b-42d3-a456-426614174002',
                           '123e4567-e89b-42d3-a456-426614174003'];
          window.crypto.randomUUID = () => turnIds.shift();
          window.geChat.submit('first', [], null);
          let bot = nodes['ge-chat-scroll'].children.filter((node) => node.className === 'msg bot')[0];
          let typing = bot.children.find((node) => node.className === 'typing');
          const pending = typing ? {
            text: typing.textContent,
            dots: typing.children.filter((node) => node.className === 'typing-dot').length,
            hidden: typing.getAttribute('aria-hidden'),
            statusText: nodes['ge-chat-status'].textContent,
            hiddenDots: typing.children.filter((node) => node.getAttribute('aria-hidden') === 'true').length
          } : null;
          window.geChat.done(1, 'first answer');
          const afterDone = {
            text: bot.textContent,
            typing: bot.children.filter((node) => node.className === 'typing').length
          };
          window.geChat.submit('second', [], null);
          bot = nodes['ge-chat-scroll'].children.filter((node) => node.className === 'msg bot').slice(-1)[0];
          window.geChat.error(2, 'model_unavailable');
          const afterError = {
            cls: bot.className,
            text: bot.textContent,
            typing: bot.children.filter((node) => node.className === 'typing').length
          };
          window.geChat.submit('third', [], null);
          bot = nodes['ge-chat-scroll'].children.filter((node) => node.className === 'msg bot').slice(-1)[0];
          window.geChat.state(3, 'cancelled', {});
          const afterCancel = {
            cls: bot.className,
            text: bot.textContent,
            typing: bot.children.filter((node) => node.className === 'typing').length
          };
          finish({pending, afterDone, afterError, afterCancel});
        """)

        self.assertEqual(
            result["pending"],
            {
                "text": "...",
                "dots": 3,
                "hidden": "true",
                "statusText": "Sending...",
                "hiddenDots": 3,
            },
        )
        self.assertEqual(result["afterDone"], {"text": "first answer", "typing": 0})
        self.assertEqual(
            result["afterError"],
            {"cls": "msg err", "text": "The model is temporarily unavailable.", "typing": 0},
        )
        self.assertEqual(
            result["afterCancel"],
            {"cls": "msg err", "text": "The follow-up was cancelled.", "typing": 0},
        )

    def test_chat_chrome_status_and_free_price_follow_page_language(self):
        english = _render()
        chinese = _render(locale="zh-CN")
        self.assertIn('<html lang="en">', english)
        self.assertIn('<button id="composer-send" type="button" disabled>Send</button>', english)
        self.assertIn('<textarea id="composer-input" rows="3"', english)
        self.assertIn('.ge-composer-row{display:flex;flex-direction:column;border:1px solid #c9bd97;', english)
        self.assertIn('padding:12px 12px 28px 12px;scrollbar-width:none}', english)
        self.assertIn('#composer-send{align-self:flex-end;flex:none;height:28px;padding:0 12px;', english)
        self.assertIn('<html lang="zh">', chinese)
        self.assertIn(
            '<div id="ge-chat-status" class="ge-chat-status" role="status">'
            '<span id="ge-chat-status-text" class="ge-chat-status-text">' +
            "\u6b63\u5728\u68c0\u67e5\u53ef\u7528\u6027" +
            '<span class="status-dots" aria-hidden="true">',
            chinese,
        )
        self.assertIn(
            '<button id="composer-send" type="button" disabled>'
            "\u53d1\u9001</button>",
            chinese,
        )
        self.assertIn('<textarea id="composer-input" rows="3"', chinese)
        self.assertIn('.ge-composer-row{display:flex;flex-direction:column;border:1px solid #c9bd97;', chinese)
        self.assertIn('padding:12px 12px 28px 12px;scrollbar-width:none}', chinese)
        self.assertIn('#composer-send{align-self:flex-end;flex:none;height:28px;padding:0 12px;', chinese)

        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:i18n', providers: [{category: 'safe', data_region: 'safe',
              training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
              deletion_scope: 'safe'}]}, history: []});
          window.geChat.submit('question', [], null);
          window.geChat.state(1, 'submitting', {price_credits: 0});
          const bot = nodes['ge-chat-scroll'].children.find((node) => node.className === 'msg bot');
          const price = bot.children.find((node) => node.className.indexOf('ge-price') === 0);
          const submitting = nodes['ge-chat-status'].textContent;
          window.geChat.state(1, 'running', {});
          const working = nodes['ge-chat-status'].textContent;
          window.geChat.done(1, 'answer');
          finish({send: nodes['composer-send'].textContent, submitting, working,
            completed: nodes['ge-chat-status'].textContent, price: price && price.textContent});
        """, language="zh-CN")

        self.assertEqual(
            result,
            {
                "send": "\u53d1\u9001",
                "submitting": "\u6b63\u5728\u53d1\u9001...",
                "working": "\u6b63\u5728\u5904\u7406...",
                "completed": "\u5df2\u5b8c\u6210",
                "price": "\u514d\u8d39",
            },
        )

    def test_chinese_chrome_localizes_all_client_owned_status_error_and_image_text(self):
        chinese = _render(locale="zh-CN")
        for marker in (
            '<div id="ge-chat-status" class="ge-chat-status" role="status">' +
            '<span id="ge-chat-status-text" class="ge-chat-status-text">' +
            "\u6b63\u5728\u68c0\u67e5\u53ef\u7528\u6027" +
            '<span class="status-dots" aria-hidden="true">',
            '<button id="composer-send" type="button" disabled>\u53d1\u9001</button>',
            '<button id="ge-retry" class="ge-retry" type="button" hidden aria-label="\u91cd\u8bd5" title="\u91cd\u8bd5"><svg',
            'aria-label="\u6240\u9009\u56fe\u7247\u9884\u89c8"',
            'aria-label="\u5173\u95ed\u56fe\u7247\u9884\u89c8"',
        ):
            self.assertIn(marker, chinese)

        result = _run_js(r"""
          function server() {
            return {ok: true, backend: 'server', state: 'ready',
              conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
              privacy_disclosure: {version: 'v:all-i18n', providers: [{category: 'safe',
                data_region: 'safe', training_enabled: false, cache_ttl_seconds: 0,
                provider_retention_hours: 0, deletion_scope: 'safe'}]}, history: []};
          }
          let uuidSeq = 1;
          window.crypto.randomUUID = () =>
            '123e4567-e89b-42d3-a456-' + String(uuidSeq++).padStart(12, '0');
          const initial = nodes['ge-chat-status'].textContent;
          window.geChat.bootstrap(server());
          const ready = nodes['ge-chat-status'].textContent;
          const stateNames = ['capability_checking', 'creating_conversation', 'restoring_history',
            'ready', 'submitting', 'queued', 'running', 'calling_model', 'completed',
            'cancelled', 'recovering', 'unavailable'];
          const states = {};
          stateNames.forEach((state) => {
            window.geChat.state(null, state, {});
            states[state] = nodes['ge-chat-status'].textContent;
          });

          const errorCodes = ['capability_disabled', 'server_unsupported', 'client_unsupported',
            'auth_required', 'privacy_confirmation_required', 'invalid_request', 'input_too_large',
            'turn_in_flight', 'conversation_busy', 'conversation_expired', 'rate_limited',
            'insufficient_credits', 'model_unavailable', 'timeout', 'init_timeout', 'cancelled',
            'network_error', 'bad_response', 'not_found', 'chat_unavailable', 'unknown_code'];
          const errors = {};
          errorCodes.forEach((code, index) => {
            window.geChat.bootstrap(server());
            window.geChat.submit('question', [], null);
            window.geChat.error(index + 1, code);
            errors[code] = nodes['ge-chat-status'].textContent;
          });
          window.geChat.bootstrap(server());
          window.geChat.submit('failed through state callback', [], null);
          window.geChat.state(22, 'failed', {error_code: 'model_unavailable'});
          const failedViaState = nodes['ge-chat-status'].textContent;

          const retry = {
            text: nodes['ge-retry'].textContent,
            label: nodes['ge-retry'].getAttribute('aria-label')
          };

          window.geChat.bootstrap(server());
          window.geChat.addImg('data:image/png;base64,iVBORw0KGgoBAg==');
          const chip = nodes['ge-imgchips'].children[0];
          const open = chip.children.find((child) => child.className === 'imgchip-open');
          const remove = chip.children.find((child) => child.className === 'imgchip-remove');
          open.click();
          const images = {
            thumbAlt: open.children[0].alt,
            viewTitle: open.title,
            viewLabel: open.getAttribute('aria-label'),
            removeTitle: remove.title,
            removeLabel: remove.getAttribute('aria-label'),
            previewAlt: nodes['ge-image-preview-image'].alt,
            dialogLabel: nodes['ge-image-preview'].getAttribute('aria-label'),
            closeTitle: nodes['ge-image-preview-close'].title,
            closeLabel: nodes['ge-image-preview-close'].getAttribute('aria-label')
          };
          nodes['ge-image-preview-close'].click();

          window.geChat.bootstrap(server());
          window.geChat.submit('free', [], null);
          window.geChat.state(23, 'submitting', {price_credits: 0});
          let bot = nodes['ge-chat-scroll'].children.find((node) => node.className === 'msg bot');
          const free = bot.children.find((node) => node.className.indexOf('ge-price') === 0).textContent;
          window.geChat.done(23, 'done');
          window.geChat.submit('paid', [], null);
          window.geChat.state(24, 'submitting', {price_credits: 2});
          bot = nodes['ge-chat-scroll'].children.filter((node) => node.className === 'msg bot').slice(-1)[0];
          const paid = bot.children.find((node) => node.className.indexOf('ge-price') === 0).textContent;
          finish({initial, ready, send: nodes['composer-send'].textContent,
            retry, states, errors, failedViaState, images, free, paid});
        """, language="zh-CN")

        self.assertEqual(result["initial"], "\u6b63\u5728\u68c0\u67e5\u53ef\u7528\u6027...")
        self.assertEqual(result["ready"], "\u5c31\u7eea")
        self.assertEqual(result["send"], "\u53d1\u9001")
        self.assertEqual(result["retry"]["text"], "")
        self.assertEqual(result["retry"]["label"], "\u91cd\u8bd5")
        self.assertEqual(
            result["states"],
            {
                "capability_checking": "\u6b63\u5728\u68c0\u67e5\u53ef\u7528\u6027...",
                "creating_conversation": "\u6b63\u5728\u5f00\u59cb\u8ffd\u95ee...",
                "restoring_history": "\u6b63\u5728\u6062\u590d\u5386\u53f2...",
                "ready": "\u5c31\u7eea",
                "submitting": "\u6b63\u5728\u53d1\u9001...",
                "queued": "\u6b63\u5728\u6392\u961f...",
                "running": "\u6b63\u5728\u5904\u7406...",
                "calling_model": "\u6b63\u5728\u5904\u7406...",
                "completed": "\u5df2\u5b8c\u6210",
                "cancelled": "\u5df2\u53d6\u6d88",
                "recovering": "\u8fde\u63a5\u4e2d\u65ad\uff0c\u8bf7\u91cd\u8bd5",
                "unavailable": "\u8ffd\u95ee\u6682\u4e0d\u53ef\u7528\u3002",
            },
        )
        self.assertEqual(
            result["errors"],
            {
                "capability_disabled": "\u8ffd\u95ee\u529f\u80fd\u5df2\u5173\u95ed\u3002",
                "server_unsupported": "\u5f53\u524d\u670d\u52a1\u7aef\u4e0d\u652f\u6301\u8ffd\u95ee\u3002",
                "client_unsupported": "\u5f53\u524d\u5ba2\u6237\u7aef\u4e0d\u652f\u6301\u8be5\u64cd\u4f5c\u3002",
                "auth_required": "\u8bf7\u4ece\u5bbf\u4e3b\u91cd\u65b0\u6253\u5f00\u6b64\u56fe\u540e\u7ee7\u7eed\u8ffd\u95ee\u3002",
                "privacy_confirmation_required": "\u5f53\u524d\u670d\u52a1\u7aef\u914d\u7f6e\u65e0\u6cd5\u4f7f\u7528\u8ffd\u95ee\u3002",
                "invalid_request": "\u8bf7\u68c0\u67e5\u95ee\u9898\u548c\u56fe\u7247\u540e\u91cd\u8bd5\u3002",
                "input_too_large": "\u95ee\u9898\u6216\u56fe\u7247\u8fc7\u5927\u3002",
                "turn_in_flight": "\u53e6\u4e00\u6761\u8ffd\u95ee\u4ecd\u5728\u5904\u7406\u4e2d\u3002",
                "conversation_busy": "\u5f53\u524d\u4f1a\u8bdd\u6b63\u5fd9\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002",
                "conversation_expired": "\u5f53\u524d\u8ffd\u95ee\u4f1a\u8bdd\u5df2\u8fc7\u671f\u3002",
                "rate_limited": "\u8bf7\u6c42\u8fc7\u4e8e\u9891\u7e41\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002",
                "insufficient_credits": "\u5f53\u524d\u79ef\u5206\u4e0d\u8db3\uff0c\u65e0\u6cd5\u8ffd\u95ee\u3002",
                "model_unavailable": "\u6a21\u578b\u6682\u65f6\u4e0d\u53ef\u7528\u3002",
                "timeout": "\u8ffd\u95ee\u8d85\u65f6\uff0c\u8bf7\u91cd\u8bd5\u3002",
                "init_timeout": "\u521d\u59cb\u5316\u8d85\u65f6\uff0c\u8bf7\u91cd\u8bd5\u3002",
                "cancelled": "\u8ffd\u95ee\u5df2\u53d6\u6d88\u3002",
                "network_error": "\u7f51\u7edc\u4e0d\u53ef\u7528\uff0c\u8bf7\u91cd\u8bd5\u3002",
                "bad_response": "\u8ffd\u95ee\u670d\u52a1\u8fd4\u56de\u4e86\u65e0\u6548\u54cd\u5e94\u3002",
                "not_found": "\u5f53\u524d\u8ffd\u95ee\u4f1a\u8bdd\u5df2\u4e0d\u53ef\u7528\u3002",
                "chat_unavailable": "\u8ffd\u95ee\u6682\u4e0d\u53ef\u7528\u3002",
                "unknown_code": "\u8ffd\u95ee\u6682\u4e0d\u53ef\u7528\u3002",
            },
        )
        self.assertEqual(result["failedViaState"], "\u6a21\u578b\u6682\u65f6\u4e0d\u53ef\u7528\u3002")
        self.assertEqual(
            result["images"],
            {
                "thumbAlt": "\u7b2c 1 \u5f20\u6240\u9009\u56fe\u7247",
                "viewTitle": "\u67e5\u770b\u7b2c 1 \u5f20\u56fe\u7247\u5927\u56fe",
                "viewLabel": "\u67e5\u770b\u7b2c 1 \u5f20\u56fe\u7247\u5927\u56fe",
                "removeTitle": "\u79fb\u9664\u7b2c 1 \u5f20\u56fe\u7247",
                "removeLabel": "\u79fb\u9664\u7b2c 1 \u5f20\u56fe\u7247",
                "previewAlt": "\u7b2c 1 \u5f20\u6240\u9009\u56fe\u7247",
                "dialogLabel": "\u6240\u9009\u56fe\u7247\u9884\u89c8",
                "closeTitle": "\u5173\u95ed\u56fe\u7247\u9884\u89c8",
                "closeLabel": "\u5173\u95ed\u56fe\u7247\u9884\u89c8",
            },
        )
        self.assertEqual(result["free"], "\u514d\u8d39")
        self.assertEqual(result["paid"], "2 \u79ef\u5206")

    def test_capability_checking_timeout_enters_unavailable_and_retry_restarts_bootstrap(self):
        result = _run_js(r"""
          function server() {
            return {ok: true, backend: 'server', state: 'ready',
              conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
              privacy_disclosure: {version: 'v:boot-timeout', providers: [{category: 'safe',
                data_region: 'safe', training_enabled: false, cache_ttl_seconds: 0,
                provider_retention_hours: 0, deletion_scope: 'safe'}]}, history: []};
          }
          const initial = {
            status: nodes['ge-chat-status'].textContent,
            statusClass: nodes['ge-chat-status'].className,
            retryHidden: nodes['ge-retry'].hidden,
            retryLabel: nodes['ge-retry'].getAttribute('aria-label'),
            inputDisabled: nodes['composer-input'].disabled,
            chatReadyCalls: calls.filter((call) => call[0] === 'chat_ready').length
          };
          runTimeouts();
          const timedOut = {
            status: nodes['ge-chat-status'].textContent,
            statusClass: nodes['ge-chat-status'].className,
            retryHidden: nodes['ge-retry'].hidden,
            inputDisabled: nodes['composer-input'].disabled,
            chatReadyCalls: calls.filter((call) => call[0] === 'chat_ready').length
          };
          nodes['ge-retry'].click();
          const retried = {
            status: nodes['ge-chat-status'].textContent,
            statusClass: nodes['ge-chat-status'].className,
            retryHidden: nodes['ge-retry'].hidden,
            inputDisabled: nodes['composer-input'].disabled,
            chatReadyCalls: calls.filter((call) => call[0] === 'chat_ready').length
          };
          runTimeouts();
          const timedOutAgain = {
            status: nodes['ge-chat-status'].textContent,
            retryHidden: nodes['ge-retry'].hidden,
            inputDisabled: nodes['composer-input'].disabled,
            chatReadyCalls: calls.filter((call) => call[0] === 'chat_ready').length
          };
          nodes['ge-retry'].click();
          const retriedAgain = {
            status: nodes['ge-chat-status'].textContent,
            retryHidden: nodes['ge-retry'].hidden,
            inputDisabled: nodes['composer-input'].disabled,
            chatReadyCalls: calls.filter((call) => call[0] === 'chat_ready').length
          };
          window.geChat.bootstrap(server());
          const ready = {
            status: nodes['ge-chat-status'].textContent,
            retryHidden: nodes['ge-retry'].hidden,
            inputDisabled: nodes['composer-input'].disabled,
            chatReadyCalls: calls.filter((call) => call[0] === 'chat_ready').length
          };
          runTimeouts();
          const afterDrain = {
            status: nodes['ge-chat-status'].textContent,
            retryHidden: nodes['ge-retry'].hidden,
            inputDisabled: nodes['composer-input'].disabled,
            chatReadyCalls: calls.filter((call) => call[0] === 'chat_ready').length
          };
          finish({initial, timedOut, retried, timedOutAgain, retriedAgain, ready, afterDrain});
        """)

        self.assertEqual(result["initial"]["status"], "Checking availability...")
        self.assertTrue(result["initial"]["retryHidden"])
        self.assertTrue(result["initial"]["inputDisabled"])
        self.assertEqual(result["initial"]["retryLabel"], "Check again")
        self.assertNotIn("is-warning", result["initial"]["statusClass"])
        self.assertEqual(result["timedOut"]["status"], "Initialization timed out. Please try again.")
        self.assertFalse(result["timedOut"]["retryHidden"])
        self.assertTrue(result["timedOut"]["inputDisabled"])
        self.assertIn("is-warning", result["timedOut"]["statusClass"])
        self.assertEqual(result["retried"]["status"], "Checking availability...")
        self.assertTrue(result["retried"]["retryHidden"])
        self.assertNotIn("is-warning", result["retried"]["statusClass"])
        self.assertEqual(result["retried"]["chatReadyCalls"], 2)
        self.assertEqual(result["timedOutAgain"]["status"], "Initialization timed out. Please try again.")
        self.assertFalse(result["timedOutAgain"]["retryHidden"])
        self.assertTrue(result["timedOutAgain"]["inputDisabled"])
        self.assertEqual(result["retriedAgain"]["status"], "Checking availability...")
        self.assertTrue(result["retriedAgain"]["retryHidden"])
        self.assertEqual(result["retriedAgain"]["chatReadyCalls"], 3)
        self.assertEqual(result["ready"]["status"], "Ready")
        self.assertTrue(result["ready"]["retryHidden"])
        self.assertFalse(result["ready"]["inputDisabled"])
        self.assertEqual(result["afterDrain"]["status"], "Ready")
        self.assertTrue(result["afterDrain"]["retryHidden"])
        self.assertFalse(result["afterDrain"]["inputDisabled"])
        self.assertEqual(result["afterDrain"]["chatReadyCalls"], 3)

    def test_resolved_chat_ready_unavailable_enters_terminal_without_watchdog(self):
        # A display-only popup (no chat bridge) resolves chat_ready() to {ok:false,unavailable}.
        # setup() must treat that resolved value as terminal unavailable IMMEDIATELY — not sit in
        # capability_checking until the 20s boot watchdog fires init_timeout. We assert the
        # terminal state is reached WITHOUT running any timeout (the watchdog never contributes).
        result = _run_js(
            r"""
              function readState() {
                return {
                  status: nodes['ge-chat-status'].textContent,
                  statusClass: nodes['ge-chat-status'].className,
                  retryHidden: nodes['ge-retry'].hidden,
                  inputDisabled: nodes['composer-input'].disabled,
                  chatReadyCalls: calls.filter((call) => call[0] === 'chat_ready').length
                };
              }
              // Defer past setup()'s resolved-chat_ready microtask, then snapshot — no runTimeouts().
              Promise.resolve().then(function () {
                Promise.resolve().then(function () { finish(readState()); });
              });
            """,
            preamble=(
                "api.chat_ready = function () { calls.push(['chat_ready']); "
                "return {ok: false, state: 'unavailable'}; };"
            ),
        )

        # Terminal "unavailable" reached from the resolved value alone: retry offered, input locked,
        # chat_ready was called exactly once, and it is NOT the watchdog's init_timeout text.
        self.assertFalse(result["retryHidden"])
        self.assertTrue(result["inputDisabled"])
        self.assertEqual(result["chatReadyCalls"], 1)
        self.assertNotEqual(result["status"], "Initialization timed out. Please try again.")
        self.assertEqual(result["status"], "Follow-up is unavailable.")

    def test_late_bootstrap_after_initial_timeout_restores_the_composer(self):
        result = _run_js(r"""
          runTimeouts();
          const timedOut = {
            status: nodes['ge-chat-status'].textContent,
            retryHidden: nodes['ge-retry'].hidden,
            inputDisabled: nodes['composer-input'].disabled
          };
          const accepted = window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:late-bootstrap', providers: [{category: 'safe',
              data_region: 'safe', training_enabled: false, cache_ttl_seconds: 0,
              provider_retention_hours: 0, deletion_scope: 'safe'}]}, history: []});
          finish({accepted, timedOut, ready: {
            status: nodes['ge-chat-status'].textContent,
            retryHidden: nodes['ge-retry'].hidden,
            inputDisabled: nodes['composer-input'].disabled
          }});
        """)

        self.assertTrue(result["accepted"])
        self.assertEqual(result["timedOut"]["status"], "Initialization timed out. Please try again.")
        self.assertFalse(result["timedOut"]["retryHidden"])
        self.assertTrue(result["timedOut"]["inputDisabled"])
        self.assertEqual(
            result["ready"],
            {"status": "Ready", "retryHidden": True, "inputDisabled": False},
        )

    def test_timeout_then_ready_state_hides_retry_again(self):
        result = _run_js(r"""
          runTimeouts();
          const timedOut = {
            status: nodes['ge-chat-status'].textContent,
            statusClass: nodes['ge-chat-status'].className,
            retryHidden: nodes['ge-retry'].hidden
          };
          window.geChat.state(null, 'ready', {});
          finish({
            timedOut,
            ready: {
              status: nodes['ge-chat-status'].textContent,
              statusClass: nodes['ge-chat-status'].className,
              retryHidden: nodes['ge-retry'].hidden
            }
          });
        """)

        self.assertEqual(result["timedOut"]["status"], "Initialization timed out. Please try again.")
        self.assertFalse(result["timedOut"]["retryHidden"])
        self.assertIn("is-warning", result["timedOut"]["statusClass"])
        self.assertEqual(result["ready"]["status"], "Ready")
        self.assertTrue(result["ready"]["retryHidden"])
        self.assertNotIn("is-warning", result["ready"]["statusClass"])

    def test_retry_button_is_compact_in_the_status_bar(self):
        page = _render()
        for marker in (
            ".ge-chat-status{padding:6px 10px 6px 12px;",
            ".ge-retry{flex:none;display:inline-flex;align-items:center;justify-content:center;",
            "padding:0;border:0;background:transparent;color:inherit;cursor:pointer;line-height:0;overflow:visible}",
            ".ge-retry svg{display:block;width:16px;height:16px}",
        ):
            self.assertIn(marker, page)

    def test_composer_spacing_keeps_send_button_bottom_right_and_text_tight(self):
        # The textarea and the send button share ONE rounded box (.ge-composer-row is a flex
        # column). The button is a flex sibling BELOW the textarea (align-self:flex-end, bottom
        # -right) so it never overlaps the text, letting the textarea keep symmetric 12px padding
        # (bottom 28px for breathing room, no side safe-zone). The textarea stays the scrollable
        # surface (overflow-y:auto, max-height 220px) with its scrollbar visually hidden.
        page = _render()
        for marker in (
            ".ge-composer-row{display:flex;flex-direction:column;border:1px solid #c9bd97;"
            "border-radius:12px;background:#fff}",
            "#composer-input{flex:1;display:block;box-sizing:border-box;width:100%;font:inherit;",
            "min-height:130px;max-height:220px;overflow-y:auto;padding:12px 12px 28px 12px;scrollbar-width:none}",
            "#composer-input::-webkit-scrollbar{display:none;width:0;height:0}",
            "#composer-send{align-self:flex-end;flex:none;height:28px;padding:0 12px;",
            "background:#0e7490;color:#fff;margin:4px 6px 6px}",
            ".ge-input-error{min-height:0;margin-top:4px;color:#8a2b1c;font-size:12px}",
        ):
            self.assertIn(marker, page)

    def test_waiting_status_uses_animated_dots_and_terminal_status_removes_them(self):
        rendered = _render()
        animation_rule = (
            ".status-dot{display:inline-block;"
            "animation:ge-status-dot 1.2s ease-in-out infinite}"
        )
        motion_override = (
            "@media (prefers-reduced-motion:reduce){"
            ".typing-dot,.status-dot{animation:none;opacity:1;transform:none}}"
        )
        self.assertEqual(rendered.count(animation_rule), 1)
        self.assertEqual(rendered.count("@keyframes ge-status-dot"), 1)
        self.assertEqual(rendered.count(motion_override), 1)

        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:status-dots', providers: [{category: 'safe',
              data_region: 'safe', training_enabled: false, cache_ttl_seconds: 0,
              provider_retention_hours: 0, deletion_scope: 'safe'}]}, history: []});
          window.geChat.submit('status', [], null);
          const waiting = nodes['ge-chat-status-text'].children.find((node) => node.className === 'status-dots');
          const pending = {text: nodes['ge-chat-status'].textContent,
            dots: waiting ? waiting.children.filter((node) => node.className === 'status-dot').length : 0,
            hidden: waiting && waiting.getAttribute('aria-hidden')};
          window.geChat.state(1, 'running', {});
          const running = nodes['ge-chat-status'].textContent;
          window.geChat.done(1, 'done');
          finish({pending, running, terminal: {text: nodes['ge-chat-status'].textContent,
            dots: nodes['ge-chat-status-text'].children.filter((node) => node.className === 'status-dots').length}});
        """)

        self.assertEqual(result["pending"], {"text": "Sending...", "dots": 3, "hidden": "true"})
        self.assertEqual(result["running"], "Working...")
        self.assertEqual(result["terminal"], {"text": "Completed", "dots": 0})

    def test_only_zero_credit_price_uses_the_standard_free_green(self):
        rendered = _render()
        self.assertIn(".ge-price.is-free{color:#15803d}", rendered)

        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:price-color', providers: [{category: 'safe',
              data_region: 'safe', training_enabled: false, cache_ttl_seconds: 0,
              provider_retention_hours: 0, deletion_scope: 'safe'}]}, history: []});
          const turnIds = ['123e4567-e89b-42d3-a456-426614174001',
                           '123e4567-e89b-42d3-a456-426614174002'];
          window.crypto.randomUUID = () => turnIds.shift();
          window.geChat.submit('free', [], null);
          window.geChat.state(1, 'submitting', {price_credits: 0});
          let bot = nodes['ge-chat-scroll'].children.filter((node) => node.className === 'msg bot')[0];
          const free = bot.children.find((node) => node.className.indexOf('ge-price') === 0);
          window.geChat.done(1, 'free answer');
          window.geChat.submit('paid', [], null);
          window.geChat.state(2, 'submitting', {price_credits: 2});
          bot = nodes['ge-chat-scroll'].children.filter((node) => node.className === 'msg bot')[1];
          const paid = bot.children.find((node) => node.className.indexOf('ge-price') === 0);
          finish({free: {text: free.textContent, cls: free.className},
            paid: {text: paid.textContent, cls: paid.className}});
        """)

        self.assertEqual(result["free"], {"text": "Free", "cls": "ge-price is-free"})
        self.assertEqual(result["paid"], {"text": "2 credits", "cls": "ge-price"})

    def test_image_preview_stacks_above_native_window_controls(self):
        rendered = _render()
        self.assertIn(
            ".ge-image-preview{position:fixed;inset:0;z-index:2147483647;",
            rendered,
        )
        self.assertIn(
            ".ge-image-preview-close{position:absolute;z-index:1;",
            rendered,
        )
        self.assertLess(
            rendered.index('id="close-btn"'),
            rendered.index('id="ge-image-preview"'),
        )

    def test_completed_answer_uses_existing_immediate_display_path(self):
        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:reveal', providers: [{category: 'safe',
              data_region: 'safe', training_enabled: false, cache_ttl_seconds: 0,
              provider_retention_hours: 0, deletion_scope: 'safe'}]}, history: []});
          window.geChat.submit('type it', [], null);
          window.geChat.state(1, 'running', {price_credits: 0});
          const answer = '**Fast** terminal response.';
          window.geChat.delta(1, answer);
          window.geChat.done(1, answer);
          const bot = nodes['ge-chat-scroll'].children.find((node) => node.className === 'msg bot');
          const snapshot = () => ({text: bot.textContent,
            strong: bot.children.reduce((total, child) =>
              total + child.children.filter((node) => node.tagName === 'STRONG').length, 0),
            frames: scheduledFrames.filter(Boolean).length,
            status: nodes['ge-chat-status'].textContent,
            inputDisabled: nodes['composer-input'].disabled});
          const immediate = snapshot();
          while (scheduledFrames.filter(Boolean).length) runAnimationFrames();
          finish({immediate, afterQueuedFrames: snapshot()});
        """)

        expected = {
            "text": "Fast terminal response.Free",
            "strong": 1,
            "frames": 0,
            "status": "Completed",
            "inputDisabled": False,
        }
        self.assertEqual(result["immediate"], expected)
        self.assertEqual(result["afterQueuedFrames"], expected)

    def test_live_markdown_coalesces_deltas_and_final_render_preserves_price(self):
        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:coalesce', providers: [{category: 'safe',
              data_region: 'safe', training_enabled: false, cache_ttl_seconds: 0,
              provider_retention_hours: 0, deletion_scope: 'safe'}]}, history: []});
          window.geChat.submit('stream', [], null);
          window.geChat.state(1, 'submitting', {price_credits: 0});
          window.geChat.delta(1, '# First');
          window.geChat.delta(1, '# Second');
          window.geChat.delta(1, '# Third');
          let bot = nodes['ge-chat-scroll'].children.filter((node) => node.className === 'msg bot')[0];
          const beforeFrame = {text: bot.textContent,
            frames: scheduledFrames.filter(Boolean).length};
          runAnimationFrames();
          const afterFrame = {text: bot.textContent,
            headings: bot.children.filter((node) => node.tagName === 'H1').length,
            prices: bot.children.filter((node) => node.className.indexOf('ge-price') === 0).length};
          window.geChat.delta(1, '**pending final**');
          const queuedBeforeDone = scheduledFrames.filter(Boolean).length;
          window.geChat.done(1, '**Final**');
          while (scheduledFrames.filter(Boolean).length) runAnimationFrames();
          bot = nodes['ge-chat-scroll'].children.filter((node) => node.className === 'msg bot')[0];
          finish({beforeFrame, afterFrame, queuedBeforeDone,
            final: {text: bot.textContent,
              strong: bot.children.reduce((total, child) =>
                total + child.children.filter((node) => node.tagName === 'STRONG').length, 0),
              prices: bot.children.filter((node) => node.className.indexOf('ge-price') === 0).length,
              frames: scheduledFrames.filter(Boolean).length}});
        """)

        self.assertEqual(result["beforeFrame"], {"text": "...Free", "frames": 1})
        self.assertEqual(result["afterFrame"], {"text": "ThirdFree", "headings": 1, "prices": 1})
        self.assertEqual(result["queuedBeforeDone"], 1)
        self.assertEqual(
            result["final"],
            {"text": "FinalFree", "strong": 1, "prices": 1, "frames": 0},
        )

    def test_unknown_training_policy_is_valid_but_training_enabled_is_rejected(self):
        result = _run_js(r"""
          const ok = window.geChat.bootstrap({
            ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:unknown-training', providers: [{
              category: 'safe-category', data_region: 'unverified', training_enabled: null,
              cache_ttl_seconds: 0, provider_retention_hours: 0, deletion_scope: 'unverified'
            }]},
            history: []
          });
          const enabledDisclosure = {
            version: 'v:enabled-training', providers: [{
              category: 'safe-category', data_region: 'safe', training_enabled: true,
              cache_ttl_seconds: 0, provider_retention_hours: 0, deletion_scope: 'safe'
            }]
          };
          finish({ok, inputDisabled: nodes['composer-input'].disabled,
            enabledAccepted: window.geChat.bootstrap({
              ok: true, backend: 'server', state: 'ready',
              conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
              privacy_disclosure: enabledDisclosure, history: []
            })});
        """)

        self.assertTrue(result["ok"])
        self.assertFalse(result["inputDisabled"])
        self.assertFalse(result["enabledAccepted"])

    def test_orphan_history_refresh_replaces_server_history_and_preserves_pending_turn(self):
        result = _run_js(r"""
          function envelope(history) {
            return {ok: true, backend: 'server', state: 'ready',
              conversation: {status: 'active', turn_count: history.length, max_turns: 40, expires_at: 1},
              privacy_disclosure: {version: 'v:orphan', providers: [{category: 'safe',
                data_region: 'safe', training_enabled: false, cache_ttl_seconds: 0,
                provider_retention_hours: 0, deletion_scope: 'safe'}]}, history};
          }
          function row(no, question, answer) {
            return {turn_no: no, status: 'completed', user_text: question,
              assistant_text: answer, error_code: null, completed_at: no, images: []};
          }
          window.geChat.bootstrap(envelope([row(1, 'stale question', 'stale answer')]));
          const baselineTimers = scheduledTimeouts.filter(Boolean).length;
          window.geChat.submit('pending current question', [], '📎 2');
          window.geChat.state(1, 'recovering', {});
          const refreshed = window.geChat.bootstrap(envelope([
            row(1, 'server orphan question', 'server authoritative answer')
          ]), true);
          const during = {refreshed, text: nodes['ge-chat-scroll'].textContent,
            busy: window.geChat.isBusy(), retryHidden: nodes['ge-retry'].hidden};
          window.geChat.state(1, 'completed', {turn_count: 2, remaining_turns: 38});
          window.geChat.done(1, 'current answer');
          finish({during, after: nodes['ge-chat-scroll'].textContent,
            busy: window.geChat.isBusy(), liveTimers: scheduledTimeouts.filter(Boolean).length,
            baselineTimers, calls});
        """)

        self.assertTrue(result["during"]["refreshed"])
        self.assertIn("server authoritative answer", result["during"]["text"])
        self.assertIn("pending current question", result["during"]["text"])
        self.assertIn("📎 2", result["during"]["text"])
        self.assertNotIn("stale answer", result["during"]["text"])
        self.assertTrue(result["during"]["busy"])
        self.assertFalse(result["during"]["retryHidden"])
        self.assertIn("current answer", result["after"])
        self.assertFalse(result["busy"])
        self.assertEqual(result["liveTimers"], result["baselineTimers"])
        self.assertEqual(len([call for call in result["calls"] if call[0] == "ask"]), 1)

    def test_orphan_history_refresh_preserves_pending_when_recovering_state_is_lost(self):
        result = _run_js(r"""
          function envelope(history) {
            return {ok: true, backend: 'server', state: 'ready',
              conversation: {status: 'active', turn_count: history.length, max_turns: 40, expires_at: 1},
              privacy_disclosure: {version: 'v:orphan-lost-state', providers: [{category: 'safe',
                data_region: 'safe', training_enabled: false, cache_ttl_seconds: 0,
                provider_retention_hours: 0, deletion_scope: 'safe'}]}, history};
          }
          const row = {turn_no: 1, status: 'completed', user_text: 'prior orphan',
            assistant_text: 'authoritative prior answer', error_code: null,
            completed_at: 1, images: []};
          window.geChat.bootstrap(envelope([]));
          window.geChat.submit('pending current question', [], null);
          // Simulate the separate recovering state evaluate_js call being lost.
          const refreshed = window.geChat.bootstrap(envelope([row]), true);
          finish({refreshed, text: nodes['ge-chat-scroll'].textContent,
            busy: window.geChat.isBusy(), retryHidden: nodes['ge-retry'].hidden,
            inputDisabled: nodes['composer-input'].disabled, calls});
        """)

        self.assertTrue(result["refreshed"])
        self.assertIn("authoritative prior answer", result["text"])
        self.assertIn("pending current question", result["text"])
        self.assertTrue(result["busy"])
        self.assertFalse(result["retryHidden"])
        self.assertTrue(result["inputDisabled"])
        self.assertEqual(len([call for call in result["calls"] if call[0] == "ask"]), 1)

    def test_orphan_history_refresh_marker_fails_closed_without_pending_turn(self):
        result = _run_js(r"""
          function envelope(history) {
            return {ok: true, backend: 'server', state: 'ready',
              conversation: {status: 'active', turn_count: history.length, max_turns: 40, expires_at: 1},
              privacy_disclosure: {version: 'v:orphan-marker', providers: [{category: 'safe',
                data_region: 'safe', training_enabled: false, cache_ttl_seconds: 0,
                provider_retention_hours: 0, deletion_scope: 'safe'}]}, history};
          }
          function row(no, question, answer) {
            return {turn_no: no, status: 'completed', user_text: question,
              assistant_text: answer, error_code: null, completed_at: no, images: []};
          }
          window.geChat.bootstrap(envelope([row(1, 'existing question', 'existing answer')]));
          const rejected = window.geChat.bootstrap(
            envelope([row(1, 'replacement question', 'replacement answer')]), true);
          finish({rejected, text: nodes['ge-chat-scroll'].textContent,
            inputDisabled: nodes['composer-input'].disabled});
        """)

        self.assertFalse(result["rejected"])
        self.assertIn("existing answer", result["text"])
        self.assertNotIn("replacement answer", result["text"])
        self.assertFalse(result["inputDisabled"])

    def test_rejected_orphan_history_refresh_preserves_pending_recovery_state(self):
        result = _run_js(r"""
          function envelope(history) {
            return {ok: true, backend: 'server', state: 'ready',
              conversation: {status: 'active', turn_count: history.length, max_turns: 40, expires_at: 1},
              privacy_disclosure: {version: 'v:orphan-reject', providers: [{category: 'safe',
                data_region: 'safe', training_enabled: false, cache_ttl_seconds: 0,
                provider_retention_hours: 0, deletion_scope: 'safe'}]}, history};
          }
          window.geChat.bootstrap(envelope([]));
          window.geChat.submit('pending after rejected refresh', [], '📎 1');
          const liveTimersBefore = scheduledTimeouts.filter(Boolean).length;
          const badMarkers = [
            window.geChat.bootstrap(envelope([]), 1),
            window.geChat.bootstrap(envelope([]), 'true'),
            window.geChat.bootstrap(envelope([]), false),
            window.geChat.bootstrap(envelope([]), true, 'extra')
          ];
          const invalidEnvelopeRejected = window.geChat.bootstrap(
            {ok: false, backend: 'server', error_code: 'bad_response'}, true);
          const legacyEnvelopeRejected = window.geChat.bootstrap(
            {ok: true, backend: 'legacy', state: 'ready', conversation: null,
              privacy_disclosure: null, history: []}, true);
          const privacyMismatch = envelope([]);
          privacyMismatch.privacy_disclosure.version = 'v:other';
          const privacyRejected = window.geChat.bootstrap(privacyMismatch, true);
          const rejected = window.geChat.bootstrap(envelope([
            {turn_no: 1, status: 'completed', user_text: 'bad server row',
              assistant_text: null, error_code: null, completed_at: 1, images: []}
          ]), true);
          const invalidDisclosure = envelope([
            {turn_no: 1, status: 'completed', user_text: 'must not render',
              assistant_text: 'must not replace pending', error_code: null, completed_at: 1, images: []}
          ]);
          invalidDisclosure.privacy_disclosure.providers[0].training_enabled = 'false';
          const disclosureRejected = window.geChat.bootstrap(invalidDisclosure, true);
          finish({badMarkers, invalidEnvelopeRejected, legacyEnvelopeRejected,
            privacyRejected, rejected, disclosureRejected, text: nodes['ge-chat-scroll'].textContent,
            busy: window.geChat.isBusy(), retryHidden: nodes['ge-retry'].hidden,
            inputDisabled: nodes['composer-input'].disabled,
            status: nodes['ge-chat-status'].textContent,
            liveTimersBefore, liveTimersAfter: scheduledTimeouts.filter(Boolean).length});
        """)

        self.assertFalse(result["rejected"])
        self.assertFalse(result["disclosureRejected"])
        self.assertEqual(result["badMarkers"], [False, False, False, False])
        self.assertFalse(result["invalidEnvelopeRejected"])
        self.assertFalse(result["legacyEnvelopeRejected"])
        self.assertFalse(result["privacyRejected"])
        self.assertIn("pending after rejected refresh", result["text"])
        self.assertIn("📎 1", result["text"])
        self.assertNotIn("must not replace pending", result["text"])
        self.assertTrue(result["busy"])
        self.assertFalse(result["retryHidden"])
        self.assertTrue(result["inputDisabled"])
        self.assertEqual(result["status"], "Connection interrupted. Check again.")
        self.assertEqual(result["liveTimersAfter"], result["liveTimersBefore"])

    def test_server_submit_is_single_flight_version_bound_priced_and_recoverable(self):
        result = _run_js(r"""
          window.geChat.bootstrap({
            ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1787587200},
            privacy_disclosure: {version: 'privacy:v7', providers: [{
              category: 'approved-category', data_region: 'approved-region', training_enabled: false,
              cache_ttl_seconds: 0, provider_retention_hours: 0, deletion_scope: 'contract-defined'
            }]}, history: []
          });
          nodes['composer-input'].value = 'Explain this';
          nodes['composer-send'].click();
          nodes['composer-send'].click();
          window.geChat.state(1, 'submitting', {price_credits: 0});
          window.geChat.state(1, 'running', {price_credits: 99});
          window.geChat.delta(1, '<img src=x onerror=owned()>');
          runAnimationFrames();
          window.geChat.state(1, 'recovering', {});
          const recovering = {
            retryHidden: nodes['ge-retry'].hidden,
            inputDisabled: nodes['composer-input'].disabled
          };
          nodes['ge-retry'].click();
          const beforeDone = nodes['ge-chat-scroll'].children
            .filter((node) => node.className.indexOf('msg ') === 0)
            .map((node) => ({cls: node.className, text: node.textContent}));
          window.geChat.done(1, '</div><script>safe text</script>');
          while (scheduledFrames.filter(Boolean).length) runAnimationFrames();
          finish({calls, recovering, beforeDone,
                  finalMessages: nodes['ge-chat-scroll'].children
                    .filter((node) => node.className.indexOf('msg ') === 0)
                    .map((node) => ({cls: node.className, text: node.textContent})),
                  busy: window.geChat.isBusy(), inputDisabled: nodes['composer-input'].disabled});
        """)

        asks = [call for call in result["calls"] if call[0] == "ask"]
        self.assertEqual(len(asks), 1)
        self.assertEqual(asks[0][2:4], ["Explain this", []])
        self.assertRegex(
            asks[0][4],
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        )
        self.assertEqual(asks[0][5], "privacy:v7")
        retries = [call for call in result["calls"] if call[0] == "retry_chat"]
        self.assertEqual(retries, [["retry_chat", asks[0][4]]])
        self.assertEqual(
            result["recovering"],
            {"retryHidden": False, "inputDisabled": True},
        )
        self.assertEqual(result["beforeDone"][-1]["text"], "<img src=x onerror=owned()>Free")
        self.assertEqual(result["finalMessages"][-1]["text"], "</div><script>safe text</script>Free")
        self.assertFalse(result["busy"])
        self.assertFalse(result["inputDisabled"])

    def test_server_submit_rate_limited_shows_error_and_allows_retry(self):
        result = _run_js(r"""
          window.geChat.bootstrap({
            ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1787587200},
            privacy_disclosure: {version: 'privacy:v429', providers: [{
              category: 'approved-category', data_region: 'approved-region', training_enabled: false,
              cache_ttl_seconds: 0, provider_retention_hours: 0, deletion_scope: 'contract-defined'
            }]}, history: []
          });
          const uuids = ['123e4567-e89b-42d3-a456-426614174000', '123e4567-e89b-42d3-a456-426614174001'];
          window.crypto.randomUUID = () => uuids.shift();
          window.geChat.submit('Explain this', [], null);
          window.geChat.state(1, 'recovering', {error_code: 'rate_limited'});
          const limited = {
            status: nodes['ge-chat-status'].textContent,
            retryHidden: nodes['ge-retry'].hidden,
            inputDisabled: nodes['composer-input'].disabled,
            busy: window.geChat.isBusy()
          };
          nodes['ge-retry'].click();
          finish({calls, limited});
        """, language="zh-CN")

        self.assertEqual(
            result["limited"],
            {
                "status": "\u8bf7\u6c42\u8fc7\u4e8e\u9891\u7e41\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002",
                "retryHidden": False,
                "inputDisabled": True,
                "busy": True,
            },
        )
        self.assertEqual(
            [call for call in result["calls"] if call[0] == "retry_chat"],
            [["retry_chat", "123e4567-e89b-42d3-a456-426614174000"]],
        )

    def test_stale_callbacks_cannot_unlock_or_relock_a_newer_active_turn(self):
        result = _run_js(r"""
          function envelope() { return {ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:callback-race', providers: [{category: 'safe', data_region: 'safe',
              training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
              deletion_scope: 'safe'}]}, history: []}; }
          window.geChat.bootstrap(envelope());
          window.crypto.randomUUID = () => '123e4567-e89b-42d3-a456-426614174001';
          window.geChat.submit('first', [], null); window.geChat.done(1, 'first answer');
          window.crypto.randomUUID = () => '123e4567-e89b-42d3-a456-426614174002';
          window.geChat.submit('second', [], null);
          window.geChat.state(1, 'running', {price_credits: 99});
          window.geChat.done(1, 'duplicate old answer');
          const duringSecond = {busy: window.geChat.isBusy(), inputDisabled: nodes['composer-input'].disabled,
            status: nodes['ge-chat-status'].textContent};
          window.geChat.done(2, 'second answer');
          window.geChat.state(1, 'running', {price_credits: 99});
          finish({duringSecond, afterSecond: {busy: window.geChat.isBusy(),
            inputDisabled: nodes['composer-input'].disabled, status: nodes['ge-chat-status'].textContent}, calls});
        """)

        self.assertEqual(
            result["duringSecond"],
            {"busy": True, "inputDisabled": True, "status": "Sending..."},
        )
        self.assertEqual(
            result["afterSecond"],
            {"busy": False, "inputDisabled": False, "status": "Completed"},
        )
        self.assertEqual(len([call for call in result["calls"] if call[0] == "ask"]), 2)

    def test_uncertain_submit_failure_keeps_the_key_for_recovery(self):
        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:uncertain', providers: [{category: 'safe', data_region: 'safe',
              training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
              deletion_scope: 'safe'}]}, history: []});
          api.ask = function () { calls.push(['ask'].concat(Array.from(arguments)));
            return Promise.reject(new Error('transport outcome unknown')); };
          window.geChat.submit('uncertain', [], null);
          Promise.resolve().then(function () { return Promise.resolve(); }).then(function () {
            nodes['ge-retry'].click();
            const ask = calls.find((call) => call[0] === 'ask');
            finish({calls, key: ask[4], busy: window.geChat.isBusy(),
              retryHidden: nodes['ge-retry'].hidden, status: nodes['ge-chat-status'].textContent});
          });
        """)

        self.assertTrue(result["busy"])
        self.assertFalse(result["retryHidden"])
        self.assertEqual(result["status"], "Connection interrupted. Check again.")
        self.assertIn(["retry_chat", result["key"]], result["calls"])

    def test_server_privacy_rejection_fails_closed_without_automatic_retry(self):
        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:privacy-mismatch', providers: [{category: 'safe', data_region: 'safe',
              training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
              deletion_scope: 'safe'}]}, history: []});
          const uuidValues = [
            '123e4567-e89b-42d3-a456-426614174000',
            '123e4567-e89b-42d3-a456-426614174001'
          ];
          window.crypto.randomUUID = function () { return uuidValues.shift(); };
          let askCount = 0;
          api.ask = function () {
            calls.push(['ask'].concat(Array.from(arguments)));
            askCount += 1;
            return askCount === 1
              ? {ok: false, error_code: 'privacy_confirmation_required'}
              : {ok: true};
          };
          let disabledInsideError = null;
          const originalError = window.geChat.error;
          window.geChat.error = function () {
            const handled = originalError.apply(window.geChat, arguments);
            disabledInsideError = nodes['composer-input'].disabled;
            return handled;
          };
          window.geChat.submit('confirm again', [], null);
          Promise.resolve().then(function () { return Promise.resolve(); }).then(function () {
            const afterMismatch = {
              asks: calls.filter((call) => call[0] === 'ask').length,
              busy: window.geChat.isBusy(),
              inputDisabled: nodes['composer-input'].disabled,
              status: nodes['ge-chat-status'].textContent
            };
            const secondAccepted = window.geChat.submit('manual retry', [], null);
            finish({calls, disabledInsideError, afterMismatch, secondAccepted});
          });
        """)

        self.assertTrue(result["disabledInsideError"])
        self.assertEqual(
            result["afterMismatch"],
            {
                "asks": 1,
                "busy": False,
                "inputDisabled": True,
                "status": "Follow-up chat is unavailable for this server configuration.",
            },
        )
        self.assertFalse(result["secondAccepted"])
        asks = [call for call in result["calls"] if call[0] == "ask"]
        self.assertEqual(len(asks), 1)
        self.assertEqual(asks[0][5], "v:privacy-mismatch")
        self.assertEqual(
            [call[0] for call in result["calls"]],
            ["chat_ready", "ask"],
        )

    def test_stale_privacy_rejection_cannot_lock_a_new_server_bootstrap(self):
        result = _run_js(r"""
          function envelope(version) { return {ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version, providers: [{category: 'safe', data_region: 'safe',
              training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
              deletion_scope: 'safe'}]}, history: []}; }
          let resolveFirst;
          api.ask = function () {
            calls.push(['ask'].concat(Array.from(arguments)));
            return new Promise((resolve) => { resolveFirst = resolve; });
          };
          window.geChat.bootstrap(envelope('v:old'));
          window.geChat.submit('old turn', [], null);
          window.geChat.done(1, 'authoritative answer');
          window.geChat.bootstrap(envelope('v:new'));
          const before = {busy: window.geChat.isBusy(),
            inputDisabled: nodes['composer-input'].disabled, status: nodes['ge-chat-status'].textContent};
          resolveFirst({ok: false, error_code: 'privacy_confirmation_required'});
          Promise.resolve().then(function () { return Promise.resolve(); }).then(function () {
            finish({before, after: {busy: window.geChat.isBusy(),
              inputDisabled: nodes['composer-input'].disabled, status: nodes['ge-chat-status'].textContent}});
          });
        """)

        self.assertEqual(
            result["after"],
            result["before"],
            "a stale privacy rejection must not make the new bootstrap unavailable",
        )

    def test_server_watchdog_enters_recovering_without_discarding_the_active_key(self):
        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:watchdog', providers: [{category: 'safe', data_region: 'safe',
              training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
              deletion_scope: 'safe'}]}, history: []});
          window.geChat.submit('slow', [], null);
          runTimeouts(); nodes['ge-retry'].click();
          finish({calls, status: nodes['ge-chat-status'].textContent,
            retryHidden: nodes['ge-retry'].hidden, busy: window.geChat.isBusy()});
        """)

        asks = [call for call in result["calls"] if call[0] == "ask"]
        retries = [call for call in result["calls"] if call[0] == "retry_chat"]
        self.assertEqual(len(asks), 1)
        self.assertEqual(retries, [["retry_chat", asks[0][4]]])
        self.assertEqual(result["status"], "The follow-up timed out. Check again.")
        self.assertFalse(result["retryHidden"])
        self.assertTrue(result["busy"])

    def test_legacy_uses_three_argument_ask_and_no_server_only_actions(self):
        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'legacy', state: 'ready', conversation: null,
            privacy_disclosure: null, history: []});
          nodes['composer-input'].value = 'legacy question';
          nodes['composer-send'].click();
          nodes['ge-retry'].click();
          window.geChat.error(1, '<b>legacy local message</b>');
          finish({calls, message: nodes['ge-chat-scroll'].children
            .filter((node) => node.className.indexOf('msg ') === 0).slice(-1)[0].textContent});
        """)

        self.assertEqual([call[0] for call in result["calls"]], ["chat_ready", "ask"])
        self.assertEqual(result["calls"][1], ["ask", 1, "legacy question", []])
        self.assertEqual(result["message"], "<b>legacy local message</b>")

    def test_server_positive_price_and_unknown_error_are_local_only(self):
        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:1', providers: [{category: 'safe', data_region: 'safe',
              training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
              deletion_scope: 'safe'}]}, history: []});
          nodes['composer-input'].value = 'priced'; nodes['composer-send'].click();
          window.geChat.state(1, 'queued', {price_credits: 7, provider: '<img onerror=owned()>'});
          const priced = nodes['ge-chat-scroll'].children
            .filter((node) => node.className === 'msg bot').slice(-1)[0].textContent;
          window.geChat.error(1, '<img src=x onerror=owned()>');
          const failure = nodes['ge-chat-scroll'].children
            .filter((node) => node.className === 'msg err').slice(-1)[0].textContent;
          finish({priced, failure, status: nodes['ge-chat-status'].textContent});
        """)

        self.assertEqual(result["priced"], "...7 credits")
        self.assertEqual(result["failure"], "Follow-up is unavailable.")
        self.assertEqual(result["status"], "Follow-up is unavailable.")

    def test_inherited_object_keys_are_not_treated_as_server_error_codes(self):
        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:error-map', providers: [{category: 'safe', data_region: 'safe',
              training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
              deletion_scope: 'safe'}]}, history: []});
          window.geChat.submit('fail safely', [], null);
          window.geChat.error(1, '__proto__');
          finish({message: nodes['ge-chat-scroll'].children
            .filter((node) => node.className === 'msg err').slice(-1)[0].textContent,
            status: nodes['ge-chat-status'].textContent});
        """)

        self.assertEqual(result["message"], "Follow-up is unavailable.")
        self.assertEqual(result["status"], "Follow-up is unavailable.")

    def test_null_turn_failure_is_ignored_without_a_delete_ui_state(self):
        ready = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:ready-ignore', providers: [{category: 'safe', data_region: 'safe',
              training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
              deletion_scope: 'safe'}]}, history: []});
          const before = {calls: calls.length, busy: window.geChat.isBusy(),
            inputDisabled: nodes['composer-input'].disabled,
            status: nodes['ge-chat-status'].textContent};
          window.geChat.state(null, 'failed', {error_code: 'conversation_busy'});
          finish({before, after: {calls: calls.length, busy: window.geChat.isBusy(),
            inputDisabled: nodes['composer-input'].disabled,
            status: nodes['ge-chat-status'].textContent}});
        """)
        self.assertEqual(ready["after"], ready["before"])

        recovering = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:recovering-ignore', providers: [{category: 'safe', data_region: 'safe',
              training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
              deletion_scope: 'safe'}]}, history: []});
          window.geChat.submit('slow', [], null); runTimeouts();
          const before = {calls: calls.length, busy: window.geChat.isBusy(),
            inputDisabled: nodes['composer-input'].disabled,
            status: nodes['ge-chat-status'].textContent};
          window.geChat.state(null, 'failed', {error_code: 'conversation_busy'});
          finish({before, after: {calls: calls.length, busy: window.geChat.isBusy(),
            inputDisabled: nodes['composer-input'].disabled,
            status: nodes['ge-chat-status'].textContent}});
        """)
        self.assertEqual(recovering["after"], recovering["before"])

    def test_removed_delete_states_are_rejected_without_mutating_the_page(self):
        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 1, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:no-delete-state', providers: [{category: 'safe', data_region: 'safe',
              training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
              deletion_scope: 'safe'}]},
            history: [{turn_no: 1, status: 'completed', user_text: 'old', assistant_text: 'answer',
              error_code: null, completed_at: 1, images: []}]});
          const before = {calls: calls.slice(), busy: window.geChat.isBusy(),
            inputDisabled: nodes['composer-input'].disabled,
            status: nodes['ge-chat-status'].textContent,
            messages: nodes['ge-chat-scroll'].children.map((node) => node.textContent)};
          const deletingAccepted = window.geChat.state(null, 'deleting', {});
          const deletedAccepted = window.geChat.state(null, 'deleted', {});
          finish({deletingAccepted, deletedAccepted, before,
            after: {calls: calls.slice(), busy: window.geChat.isBusy(),
              inputDisabled: nodes['composer-input'].disabled,
              status: nodes['ge-chat-status'].textContent,
              messages: nodes['ge-chat-scroll'].children.map((node) => node.textContent)}});
        """)

        self.assertFalse(result["deletingAccepted"])
        self.assertFalse(result["deletedAccepted"])
        self.assertEqual(result["after"], result["before"])

    def test_uuid_fallback_is_canonical_and_crypto_absence_fails_closed(self):
        fallback = _run_js(r"""
          window.crypto.randomUUID = undefined;
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:crypto', providers: [{category: 'safe', data_region: 'safe',
              training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
              deletion_scope: 'safe'}]}, history: []});
          window.geChat.submit('fallback', [], null);
          finish({calls});
        """)
        asks = [call for call in fallback["calls"] if call[0] == "ask"]
        self.assertEqual(asks[0][4], "00010203-0405-4607-8809-0a0b0c0d0e0f")

        no_crypto = _run_js(r"""
          window.crypto = {};
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:no-crypto', providers: [{category: 'safe', data_region: 'safe',
              training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
              deletion_scope: 'safe'}]}, history: []});
          const accepted = window.geChat.submit('must fail', [], null);
          finish({accepted, calls, status: nodes['ge-chat-status'].textContent,
                  messages: nodes['ge-chat-scroll'].children.filter((node) => node.className.indexOf('msg ') === 0).length});
        """)
        self.assertFalse(no_crypto["accepted"])
        self.assertEqual([call[0] for call in no_crypto["calls"]], ["chat_ready"])
        self.assertEqual(no_crypto["status"], "Follow-up is unavailable.")
        self.assertEqual(no_crypto["messages"], 0)

    def test_repeated_crypto_uuid_fails_closed_instead_of_reusing_an_idempotency_key(self):
        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:unique', providers: [{category: 'safe', data_region: 'safe',
              training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
              deletion_scope: 'safe'}]}, history: []});
          window.geChat.submit('first', [], null); window.geChat.done(1, 'done');
          const second = window.geChat.submit('second', [], null);
          finish({second, calls, status: nodes['ge-chat-status'].textContent});
        """)

        self.assertFalse(result["second"])
        self.assertEqual(len([call for call in result["calls"] if call[0] == "ask"]), 1)
        self.assertEqual(result["status"], "Follow-up is unavailable.")

    def test_new_bootstrap_binds_the_new_privacy_version_without_confirmation(self):
        result = _run_js(r"""
          function envelope(version) { return {ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version, providers: [{category: 'safe', data_region: 'safe',
              training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
              deletion_scope: 'safe'}]}, history: []}; }
          window.geChat.bootstrap(envelope('privacy:v1'));
          window.geChat.bootstrap(envelope('privacy:v2'));
          const before = {disabled: nodes['composer-input'].disabled,
            accepted: window.geChat.submit('sent without reconfirm', [], null)};
          finish({before, calls});
        """)

        self.assertEqual(result["before"], {"disabled": False, "accepted": True})
        asks = [call for call in result["calls"] if call[0] == "ask"]
        self.assertEqual(len(asks), 1)
        self.assertEqual(asks[0][5], "privacy:v2")

    def test_rebootstrap_keeps_uuid_history_and_rejects_server_to_legacy_downgrade(self):
        result = _run_js(r"""
          function server(version) { return {ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version, providers: [{category: 'safe', data_region: 'safe',
              training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
              deletion_scope: 'safe'}]}, history: []}; }
          window.geChat.bootstrap(server('v:one'));
          window.geChat.submit('first', [], null); window.geChat.done(1, 'done');
          window.geChat.bootstrap(server('v:two'));
          const repeatedUuidAccepted = window.geChat.submit('must not reuse key', [], null);
          const downgradeAccepted = window.geChat.bootstrap({ok: true, backend: 'legacy', state: 'ready',
            conversation: null, privacy_disclosure: null, history: []});
          finish({repeatedUuidAccepted, downgradeAccepted, busy: window.geChat.isBusy(),
            inputDisabled: nodes['composer-input'].disabled, status: nodes['ge-chat-status'].textContent, calls});
        """)

        self.assertFalse(result["repeatedUuidAccepted"])
        self.assertFalse(result["downgradeAccepted"])
        self.assertFalse(result["busy"])
        self.assertTrue(result["inputDisabled"])
        self.assertEqual(result["status"], "Follow-up is unavailable.")
        self.assertEqual(len([call for call in result["calls"] if call[0] == "ask"]), 1)

    def test_clicking_selected_image_opens_and_closes_an_accessible_large_preview(self):
        rendered = _render()
        for marker in (
            'id="ge-image-preview"',
            'id="ge-image-preview-image"',
            'id="ge-image-preview-close"',
            'role="dialog"',
            'aria-modal="true"',
            '.ge-image-preview[hidden]{display:none}',
        ):
            self.assertIn(marker, rendered)

        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:preview', providers: [{category: 'safe', data_region: 'safe',
              training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
              deletion_scope: 'safe'}]}, history: []});
          const png = 'data:image/png;base64,iVBORw0KGgoBAg==';
          window.geChat.addImg(png);
          const chip = nodes['ge-imgchips'].children[0];
          const openButton = chip && chip.children.find((child) => child.className === 'imgchip-open');
          const removeButton = chip && chip.children.find((child) => child.className === 'imgchip-remove');
          if (!openButton || !removeButton) {
            finish({contractMissing: true});
          } else {
            openButton.click();
            const opened = {
              hidden: nodes['ge-image-preview'].hidden,
              src: nodes['ge-image-preview-image'].src,
              alt: nodes['ge-image-preview-image'].alt,
              closeFocused: nodes['ge-image-preview-close'].focused === true,
              chips: nodes['ge-imgchips'].children.length
            };
            nodes['ge-image-preview'].dispatchEvent(
              {type: 'click', target: nodes['ge-image-preview-image']});
            const imageClickKeptPreviewOpen = !nodes['ge-image-preview'].hidden;
            nodes['ge-image-preview-close'].focused = false;
            const tabEvent = {type: 'keydown', key: 'Tab'};
            document.dispatchEvent(tabEvent);
            const tabTrapped = tabEvent.defaultPrevented === true && tabEvent.immediateStopped === true &&
              nodes['ge-image-preview-close'].focused === true;
            nodes['ge-image-preview-close'].focused = false;
            const shiftTabEvent = {type: 'keydown', key: 'Tab', shiftKey: true};
            document.dispatchEvent(shiftTabEvent);
            const shiftTabTrapped = shiftTabEvent.defaultPrevented === true &&
              shiftTabEvent.immediateStopped === true && nodes['ge-image-preview-close'].focused === true;
            nodes['ge-image-preview-close'].click();
            const closedByButton = {
              hidden: nodes['ge-image-preview'].hidden,
              src: nodes['ge-image-preview-image'].src,
              srcAttribute: nodes['ge-image-preview-image'].getAttribute('src'),
              openerFocused: openButton.focused === true
            };
            openButton.click();
            nodes['ge-image-preview'].dispatchEvent({type: 'click', target: nodes['ge-image-preview']});
            const closedByBackdrop = nodes['ge-image-preview'].hidden;
            openButton.click();
            document.addEventListener('keydown', function (event) {
              if (event.key === 'Escape') api.close();
            });
            const escapeEvent = {type: 'keydown', key: 'Escape'};
            document.dispatchEvent(escapeEvent);
            const closedByEscape = nodes['ge-image-preview'].hidden;
            const closeCalls = calls.filter((call) => call[0] === 'close').length;
            nodes['composer-input'].focused = false;
            openButton.click();
            removeButton.click();
            finish({contractMissing: false, opened, imageClickKeptPreviewOpen, tabTrapped, shiftTabTrapped,
              closedByButton, closedByBackdrop, closedByEscape,
              escapePrevented: escapeEvent.defaultPrevented === true,
              escapeStopped: escapeEvent.immediateStopped === true,
              closeCalls, removedWhileOpen: {hidden: nodes['ge-image-preview'].hidden,
                src: nodes['ge-image-preview-image'].src,
                composerFocused: nodes['composer-input'].focused === true,
                chips: nodes['ge-imgchips'].children.length}});
          }
        """)

        self.assertFalse(result["contractMissing"])
        self.assertEqual(
            result["opened"],
            {
                "hidden": False,
                "src": "data:image/png;base64,iVBORw0KGgoBAg==",
                "alt": "Selected image 1",
                "closeFocused": True,
                "chips": 1,
            },
        )
        self.assertTrue(result["imageClickKeptPreviewOpen"])
        self.assertTrue(result["tabTrapped"])
        self.assertTrue(result["shiftTabTrapped"])
        self.assertEqual(
            result["closedByButton"],
            {"hidden": True, "src": "", "srcAttribute": None, "openerFocused": True},
        )
        self.assertTrue(result["closedByBackdrop"])
        self.assertTrue(result["closedByEscape"])
        self.assertTrue(result["escapePrevented"])
        self.assertTrue(result["escapeStopped"])
        self.assertEqual(result["closeCalls"], 0)
        self.assertEqual(
            result["removedWhileOpen"],
            {"hidden": True, "src": "", "composerFocused": True, "chips": 0},
        )

    def test_large_preview_uses_localized_labels_and_escape_is_order_independent(self):
        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:preview-edges', providers: [{category: 'safe', data_region: 'safe',
              training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
              deletion_scope: 'safe'}]}, history: []});
          window.geChat.addImg('data:image/png;base64,iVBORw0KGgoBAg==');
          window.geChat.addImg('data:image/jpeg;base64,/9j/4AE=');
          const secondChip = nodes['ge-imgchips'].children[1];
          const secondOpen = secondChip.children.find((child) => child.className === 'imgchip-open');
          secondOpen.click();
          const labels = {
            imageAlt: nodes['ge-image-preview-image'].alt,
            dialog: nodes['ge-image-preview'].getAttribute('aria-label'),
            close: nodes['ge-image-preview-close'].getAttribute('aria-label')
          };
          documentListeners.keydown.reverse();
          const escapeEvent = {type: 'keydown', key: 'Escape'};
          document.dispatchEvent(escapeEvent);
          finish({labels, previewHidden: nodes['ge-image-preview'].hidden,
            closeCalls: calls.filter((call) => call[0] === 'close').length,
            escapeStopped: escapeEvent.immediateStopped === true});
        """, language="zh-CN")

        self.assertEqual(
            result["labels"],
            {
                "imageAlt": "\u7b2c 2 \u5f20\u6240\u9009\u56fe\u7247",
                "dialog": "\u6240\u9009\u56fe\u7247\u9884\u89c8",
                "close": "\u5173\u95ed\u56fe\u7247\u9884\u89c8",
            },
        )
        self.assertTrue(result["previewHidden"])
        self.assertEqual(result["closeCalls"], 0)
        self.assertTrue(result["escapeStopped"])

    def test_image_preview_wheel_scales_with_bounds_and_resets(self):
        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:preview-zoom', providers: [{category: 'safe', data_region: 'safe',
              training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
              deletion_scope: 'safe'}]}, history: []});
          window.geChat.addImg(TEST_VALID_PNG);
          const chip = nodes['ge-imgchips'].children[0];
          const openButton = chip.children.find((child) => child.className === 'imgchip-open');
          openButton.click();
          const initial = nodes['ge-image-preview-image'].style.transform || '';
          const initialOrigin = nodes['ge-image-preview-image'].style.transformOrigin || '';
          for (let i = 0; i < 5; i += 1) {
            nodes['ge-image-preview'].dispatchEvent({type: 'wheel', deltaY: -10, clientX: 60, clientY: 45});
          }
          const partialTrackpad = nodes['ge-image-preview-image'].style.transform || '';
          for (let i = 0; i < 5; i += 1) {
            nodes['ge-image-preview'].dispatchEvent({type: 'wheel', deltaY: -10, clientX: 60, clientY: 45});
          }
          const accumulatedTrackpad = nodes['ge-image-preview-image'].style.transform || '';
          nodes['ge-image-preview-close'].click(); openButton.click();
          const zoomIn = {type: 'wheel', deltaY: -120, clientX: 60, clientY: 45};
          nodes['ge-image-preview'].dispatchEvent(zoomIn);
          const afterZoomIn = nodes['ge-image-preview-image'].style.transform || '';
          const zoomOrigin = nodes['ge-image-preview-image'].style.transformOrigin || '';
          for (let i = 0; i < 100; i += 1) {
            nodes['ge-image-preview'].dispatchEvent({type: 'wheel', deltaY: -120});
          }
          const maximum = nodes['ge-image-preview-image'].style.transform || '';
          for (let i = 0; i < 200; i += 1) {
            nodes['ge-image-preview'].dispatchEvent({type: 'wheel', deltaY: 120});
          }
          const minimum = nodes['ge-image-preview-image'].style.transform || '';
          nodes['ge-image-preview-close'].click();
          const afterClose = nodes['ge-image-preview-image'].style.transform || '';
          const closedWheel = {type: 'wheel', deltaY: -120};
          nodes['ge-image-preview'].dispatchEvent(closedWheel);
          const afterClosedWheel = nodes['ge-image-preview-image'].style.transform || '';
          openButton.click();
          const reopened = nodes['ge-image-preview-image'].style.transform || '';
          const reopenedOrigin = nodes['ge-image-preview-image'].style.transformOrigin || '';
          const keyboardIn = {type: 'keydown', key: '+'}; document.dispatchEvent(keyboardIn);
          const afterKeyboardIn = nodes['ge-image-preview-image'].style.transform || '';
          const keyboardOut = {type: 'keydown', key: '-'}; document.dispatchEvent(keyboardOut);
          const afterKeyboardOut = nodes['ge-image-preview-image'].style.transform || '';
          finish({initial, initialOrigin, partialTrackpad, accumulatedTrackpad, afterZoomIn,
            zoomOrigin, maximum, minimum, afterClose, afterClosedWheel,
            reopened, reopenedOrigin, afterKeyboardIn, afterKeyboardOut,
            zoomPrevented: zoomIn.defaultPrevented === true,
            closedPrevented: closedWheel.defaultPrevented === true,
            keyboardPrevented: keyboardIn.defaultPrevented === true && keyboardOut.defaultPrevented === true});
        """)

        self.assertEqual(result["initial"], "scale(1)")
        self.assertEqual(result["initialOrigin"], "center center")
        self.assertEqual(result["partialTrackpad"], "scale(1)")
        self.assertEqual(result["accumulatedTrackpad"], "scale(1.1)")
        self.assertEqual(result["afterZoomIn"], "scale(1.1)")
        self.assertEqual(result["zoomOrigin"], "25% 25%")
        self.assertEqual(result["maximum"], "scale(5)")
        self.assertEqual(result["minimum"], "scale(0.5)")
        self.assertEqual(result["afterClose"], "scale(1)")
        self.assertEqual(result["afterClosedWheel"], "scale(1)")
        self.assertEqual(result["reopened"], "scale(1)")
        self.assertEqual(result["reopenedOrigin"], "center center")
        self.assertEqual(result["afterKeyboardIn"], "scale(1.1)")
        self.assertEqual(result["afterKeyboardOut"], "scale(1)")
        self.assertTrue(result["zoomPrevented"])
        self.assertFalse(result["closedPrevented"])
        self.assertTrue(result["keyboardPrevented"])

    def test_image_preview_wheel_locks_origin_after_zoom_begins(self):
        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:preview-wheel-origin', providers: [{category: 'safe',
              data_region: 'safe', training_enabled: false, cache_ttl_seconds: 0,
              provider_retention_hours: 0, deletion_scope: 'safe'}]}, history: []});
          window.geChat.addImg(TEST_VALID_PNG);
          const chip = nodes['ge-imgchips'].children[0];
          const openButton = chip.children.find((child) => child.className === 'imgchip-open');
          openButton.click();

          nodes['ge-image-preview'].dispatchEvent(
            {type: 'wheel', deltaY: -120, deltaMode: 0, clientX: 60, clientY: 45});
          const originAfterFirstZoom = nodes['ge-image-preview-image'].style.transformOrigin;
          nodes['ge-image-preview'].dispatchEvent(
            {type: 'wheel', deltaY: -10, deltaMode: 0, clientX: 120, clientY: 90});
          const originAfterMovedNoStep = nodes['ge-image-preview-image'].style.transformOrigin;
          nodes['ge-image-preview'].dispatchEvent(
            {type: 'wheel', deltaY: -120, deltaMode: 0, clientX: 120, clientY: 90});
          finish({originAfterFirstZoom, originAfterMovedNoStep,
            originAfterMovedZoom: nodes['ge-image-preview-image'].style.transformOrigin,
            scaleAfterMovedZoom: nodes['ge-image-preview-image'].style.transform});
        """)

        self.assertEqual(result["originAfterFirstZoom"], "25% 25%")
        self.assertEqual(result["originAfterMovedNoStep"], "25% 25%")
        self.assertEqual(result["originAfterMovedZoom"], "25% 25%")
        self.assertEqual(result["scaleAfterMovedZoom"], "scale(1.2)")

    def test_image_preview_wheel_normalizes_delta_modes(self):
        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:preview-wheel-modes', providers: [{category: 'safe',
              data_region: 'safe', training_enabled: false, cache_ttl_seconds: 0,
              provider_retention_hours: 0, deletion_scope: 'safe'}]}, history: []});
          window.geChat.addImg(TEST_VALID_PNG);
          const chip = nodes['ge-imgchips'].children[0];
          const openButton = chip.children.find((child) => child.className === 'imgchip-open');
          openButton.click();

          const lineZoomIn = {type: 'wheel', deltaY: -3, deltaMode: 1, clientX: 60, clientY: 45};
          nodes['ge-image-preview'].dispatchEvent(lineZoomIn);
          const afterLineZoomIn = nodes['ge-image-preview-image'].style.transform;
          const originAfterLineZoomIn = nodes['ge-image-preview-image'].style.transformOrigin;

          const movedNoStep = {type: 'wheel', deltaY: -10, deltaMode: 0, clientX: 120, clientY: 90};
          nodes['ge-image-preview'].dispatchEvent(movedNoStep);
          const afterMovedNoStep = nodes['ge-image-preview-image'].style.transform;
          const originAfterMovedNoStep = nodes['ge-image-preview-image'].style.transformOrigin;

          const pageZoomIn = {type: 'wheel', deltaY: -1, deltaMode: 2, clientX: 120, clientY: 90};
          nodes['ge-image-preview'].dispatchEvent(pageZoomIn);
          const afterPageZoomIn = nodes['ge-image-preview-image'].style.transform;
          const originAfterPageZoomIn = nodes['ge-image-preview-image'].style.transformOrigin;

          const lineZoomOut = {type: 'wheel', deltaY: 3, deltaMode: 1, clientX: 120, clientY: 90};
          nodes['ge-image-preview'].dispatchEvent(lineZoomOut);
          const afterLineZoomOut = nodes['ge-image-preview-image'].style.transform;
          finish({afterLineZoomIn, originAfterLineZoomIn, afterMovedNoStep, originAfterMovedNoStep,
            afterPageZoomIn, originAfterPageZoomIn,
            afterLineZoomOut, prevented: [lineZoomIn, movedNoStep, pageZoomIn, lineZoomOut]
              .every((event) => event.defaultPrevented === true)});
        """)

        self.assertEqual(result["afterLineZoomIn"], "scale(1.1)")
        self.assertEqual(result["originAfterLineZoomIn"], "25% 25%")
        self.assertEqual(result["afterMovedNoStep"], "scale(1.1)")
        self.assertEqual(result["originAfterMovedNoStep"], "25% 25%")
        self.assertEqual(result["afterPageZoomIn"], "scale(1.2)")
        self.assertEqual(result["originAfterPageZoomIn"], "25% 25%")
        self.assertEqual(result["afterLineZoomOut"], "scale(1.1)")
        self.assertTrue(result["prevented"])

    def test_image_preview_keyboard_reset_recenters_the_zoom_origin(self):
        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:preview-keyboard-reset', providers: [{category: 'safe',
              data_region: 'safe', training_enabled: false, cache_ttl_seconds: 0,
              provider_retention_hours: 0, deletion_scope: 'safe'}]}, history: []});
          window.geChat.addImg(TEST_VALID_PNG);
          const chip = nodes['ge-imgchips'].children[0];
          chip.children.find((child) => child.className === 'imgchip-open').click();
          nodes['ge-image-preview'].dispatchEvent(
            {type: 'wheel', deltaY: -120, deltaMode: 0, clientX: 60, clientY: 45});
          const reset = {type: 'keydown', key: '0'};
          document.dispatchEvent(reset);
          finish({scale: nodes['ge-image-preview-image'].style.transform,
            origin: nodes['ge-image-preview-image'].style.transformOrigin,
            prevented: reset.defaultPrevented === true});
        """)

        self.assertEqual(result["scale"], "scale(1)")
        self.assertEqual(result["origin"], "center center")
        self.assertTrue(result["prevented"])

    def test_image_preview_preserves_modified_page_zoom_shortcuts(self):
        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:preview-modified-keys', providers: [{category: 'safe',
              data_region: 'safe', training_enabled: false, cache_ttl_seconds: 0,
              provider_retention_hours: 0, deletion_scope: 'safe'}]}, history: []});
          window.geChat.addImg(TEST_VALID_PNG);
          const chip = nodes['ge-imgchips'].children[0];
          chip.children.find((child) => child.className === 'imgchip-open').click();
          const modified = {type: 'keydown', key: '+', ctrlKey: true};
          document.dispatchEvent(modified);
          finish({scale: nodes['ge-image-preview-image'].style.transform,
            prevented: modified.defaultPrevented === true,
            stopped: modified.immediateStopped === true});
        """)

        self.assertEqual(result["scale"], "scale(1)")
        self.assertFalse(result["prevented"])
        self.assertFalse(result["stopped"])

    def test_image_preview_close_falls_back_when_the_opener_was_detached(self):
        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:preview-detached-opener', providers: [{category: 'safe',
              data_region: 'safe', training_enabled: false, cache_ttl_seconds: 0,
              provider_retention_hours: 0, deletion_scope: 'safe'}]}, history: []});
          window.geChat.addImg(TEST_VALID_PNG);
          const chip = nodes['ge-imgchips'].children[0];
          const openButton = chip.children.find((child) => child.className === 'imgchip-open');
          openButton.click();
          nodes['composer-input'].focused = false;
          openButton.focused = false;
          openButton.isConnected = false;
          nodes['ge-image-preview-close'].click();
          finish({inputFocused: nodes['composer-input'].focused === true,
            detachedOpenerFocused: openButton.focused === true});
        """)

        self.assertTrue(result["inputFocused"])
        self.assertFalse(result["detachedOpenerFocused"])

    def test_full_page_escape_closes_preview_then_popup_without_cross_script_error(self):
        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:full-page-escape', providers: [{category: 'safe',
              data_region: 'safe', training_enabled: false, cache_ttl_seconds: 0,
              provider_retention_hours: 0, deletion_scope: 'safe'}]}, history: []});
          window.geChat.addImg(TEST_VALID_PNG);
          const chip = nodes['ge-imgchips'].children[0];
          chip.children.find((child) => child.className === 'imgchip-open').click();
          let firstError = null, secondError = null;
          try { document.dispatchEvent({type: 'keydown', key: 'Escape'}); }
          catch (error) { firstError = String(error && error.message ? error.message : error); }
          const hiddenAfterFirstEscape = nodes['ge-image-preview'].hidden;
          try { document.dispatchEvent({type: 'keydown', key: 'Escape'}); }
          catch (error) { secondError = String(error && error.message ? error.message : error); }
          finish({firstError, secondError, hiddenAfterFirstEscape,
            closeCalls: calls.filter((call) => call[0] === 'close').length});
        """, include_chrome=True)

        self.assertIsNone(result["firstError"])
        self.assertTrue(result["hiddenAfterFirstEscape"])
        self.assertIsNone(result["secondError"])
        self.assertEqual(result["closeCalls"], 1)

    def test_sent_message_reuses_clickable_image_thumbnails(self):
        result = _run_js(r"""
          function envelope() {
            return {ok: true, backend: 'server', state: 'ready',
              conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
              privacy_disclosure: {version: 'v:sent-images', providers: [{category: 'safe',
                data_region: 'safe', training_enabled: false, cache_ttl_seconds: 0,
                provider_retention_hours: 0, deletion_scope: 'safe'}]}, history: []};
          }
          function descendants(node, output) {
            node.children.forEach((child) => { output.push(child); descendants(child, output); });
            return output;
          }
          function userMessages() {
            return nodes['ge-chat-scroll'].children
              .filter((node) => node.className === 'msg user');
          }
          function snapshot(message) {
            const all = descendants(message, []);
            return {
              text: message.textContent,
              sources: all.filter((node) => node.className === 'imgchip-preview').map((node) => node.src),
              imageAlts: all.filter((node) => node.className === 'imgchip-preview').map((node) => node.alt),
              loading: all.filter((node) => node.className === 'imgchip-preview').map((node) => node.loading),
              decoding: all.filter((node) => node.className === 'imgchip-preview').map((node) => node.decoding),
              openButtons: all.filter((node) => node.className === 'imgchip-open'),
              openLabels: all.filter((node) => node.className === 'imgchip-open')
                .map((node) => node.getAttribute('aria-label')),
              removeButtons: all.filter((node) => node.className === 'imgchip-remove').length
            };
          }
          window.geChat.bootstrap(envelope());
          const png = TEST_VALID_PNG, jpeg = TEST_VALID_JPEG, webp = TEST_VALID_WEBP;
          window.geChat.addImg(png); window.geChat.addImg(jpeg); window.geChat.addImg(webp);
          nodes['composer-input'].value = 'first image message';
          nodes['composer-send'].click();
          const initial = snapshot(userMessages()[0]);
          if (initial.openButtons.length) initial.openButtons[0].click();
          const initialPreview = nodes['ge-image-preview-image'].src;
          nodes['ge-image-preview-close'].click();
          const composerChipsAfterSend = nodes['ge-imgchips'].children.length;

          const refreshAccepted = window.geChat.bootstrap(envelope(), true);
          const recovered = snapshot(userMessages()[0]);
          window.geChat.done(1, 'first answer');
          if (recovered.openButtons.length > 2) recovered.openButtons[2].click();
          const completedPreview = nodes['ge-image-preview-image'].src;
          nodes['ge-image-preview-close'].click();
          finish({
            initial: {text: initial.text, sources: initial.sources, imageAlts: initial.imageAlts,
              loading: initial.loading, decoding: initial.decoding,
              openButtons: initial.openButtons.length, openLabels: initial.openLabels,
              removeButtons: initial.removeButtons},
            initialPreview, composerChipsAfterSend, refreshAccepted,
            recovered: {text: recovered.text, sources: recovered.sources, imageAlts: recovered.imageAlts,
              loading: recovered.loading, decoding: recovered.decoding,
              openButtons: recovered.openButtons.length, openLabels: recovered.openLabels,
              removeButtons: recovered.removeButtons},
            completedPreview
          });
        """)

        failed_result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:failed-image', providers: [{category: 'safe',
              data_region: 'safe', training_enabled: false, cache_ttl_seconds: 0,
              provider_retention_hours: 0, deletion_scope: 'safe'}]}, history: []});
          function descendants(node, output) {
            node.children.forEach((child) => { output.push(child); descendants(child, output); });
            return output;
          }
          window.geChat.addImg(TEST_VALID_PNG);
          nodes['composer-input'].value = 'failed image message';
          nodes['composer-send'].click();
          window.geChat.error(1, 'model_unavailable');
          const messages = nodes['ge-chat-scroll'].children
            .filter((node) => node.className.indexOf('msg ') === 0);
          const user = messages.find((node) => node.className === 'msg user');
          const all = descendants(user, []);
          const openButtons = all.filter((node) => node.className === 'imgchip-open');
          if (openButtons.length) openButtons[0].click();
          finish({
            user: {text: user.textContent,
              sources: all.filter((node) => node.className === 'imgchip-preview').map((node) => node.src),
              openButtons: openButtons.length,
              removeButtons: all.filter((node) => node.className === 'imgchip-remove').length},
            preview: nodes['ge-image-preview-image'].src,
            errorClass: messages[messages.length - 1].className,
            errorText: messages[messages.length - 1].textContent,
            busy: window.geChat.isBusy()
          });
        """)

        expected_first = {
            "text": "first image message",
            "sources": [
                _VALID_PNG_DATA_URL,
                _VALID_JPEG_DATA_URL,
                _VALID_WEBP_DATA_URL,
            ],
            "imageAlts": ["Sent image 1", "Sent image 2", "Sent image 3"],
            "loading": ["lazy", "lazy", "lazy"],
            "decoding": ["async", "async", "async"],
            "openButtons": 3,
            "openLabels": ["View sent image 1", "View sent image 2", "View sent image 3"],
            "removeButtons": 0,
        }
        self.assertEqual(result["initial"], expected_first)
        self.assertEqual(result["initialPreview"], expected_first["sources"][0])
        self.assertEqual(result["composerChipsAfterSend"], 0)
        self.assertTrue(result["refreshAccepted"])
        self.assertEqual(result["recovered"], expected_first)
        self.assertEqual(result["completedPreview"], expected_first["sources"][2])
        self.assertEqual(
            failed_result["user"],
            {
                "text": "failed image message",
                "sources": [_VALID_PNG_DATA_URL],
                "openButtons": 1,
                "removeButtons": 0,
            },
        )
        self.assertEqual(failed_result["preview"], _VALID_PNG_DATA_URL)
        self.assertEqual(failed_result["errorClass"], "msg err")
        self.assertEqual(failed_result["errorText"], "The model is temporarily unavailable.")
        self.assertFalse(failed_result["busy"])

    def test_removing_a_closed_preview_restores_focus(self):
        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:remove-focus', providers: [{category: 'safe',
              data_region: 'safe', training_enabled: false, cache_ttl_seconds: 0,
              provider_retention_hours: 0, deletion_scope: 'safe'}]}, history: []});
          window.geChat.addImg(TEST_VALID_PNG); window.geChat.addImg(TEST_VALID_JPEG);
          nodes['ge-imgchips'].children[0].children
            .find((child) => child.className === 'imgchip-remove').click();
          const remainingOpen = nodes['ge-imgchips'].children[0].children
            .find((child) => child.className === 'imgchip-open');
          const nextFocused = remainingOpen.focused === true;
          nodes['ge-imgchips'].children[0].children
            .find((child) => child.className === 'imgchip-remove').click();
          finish({nextFocused, inputFocused: nodes['composer-input'].focused === true,
            chips: nodes['ge-imgchips'].children.length});
        """)

        self.assertTrue(result["nextFocused"])
        self.assertTrue(result["inputFocused"])
        self.assertEqual(result["chips"], 0)

    def test_current_turn_images_are_validated_previewed_removed_and_ordered(self):
        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:images', providers: [{category: 'safe', data_region: 'safe',
              training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
              deletion_scope: 'safe'}]}, history: []});
          function dataUrl(mime, bytes) { return 'data:' + mime + ';base64,' + Buffer.from(bytes).toString('base64'); }
          const png = dataUrl('image/png', [137,80,78,71,13,10,26,10,1,2]);
          const samePngAlternateBase64 = png.replace(/g==$/, 'h==');
          const jpeg = dataUrl('image/jpeg', [255,216,255,224,1]);
          const webp = dataUrl('image/webp', [82,73,70,70,1,0,0,0,87,69,66,80,1]);
          const validAdds = [window.geChat.addImg(png), window.geChat.addImg(jpeg), window.geChat.addImg(webp)];
          const badBytes = Buffer.alloc(4 * 1024 * 1024 + 1); [137,80,78,71,13,10,26,10].forEach((v,i) => badBytes[i]=v);
          const rejected = [
            window.geChat.addImg('javascript:owned()'),
            window.geChat.addImg('data:image/svg+xml;base64,PHN2Zz4='),
            window.geChat.addImg('data:image/png;base64,%%%'),
            window.geChat.addImg(dataUrl('image/png', [255,216,255,224])),
            window.geChat.addImg('data:image/png;base64,' + badBytes.toString('base64')),
            window.geChat.addImg(png),
            window.geChat.addImg(samePngAlternateBase64)
          ];
          const sourcesBefore = nodes['ge-imgchips'].children.map((chip) =>
            chip.children.find((child) => child.className === 'imgchip-open').children[0].src);
          nodes['ge-imgchips'].children[1].children.find((child) => child.className === 'imgchip-remove').click();
          const tooLongAccepted = window.geChat.submit('x'.repeat(20001), [], null);
          const longError = nodes['ge-input-error'].textContent;
          if (tooLongAccepted) window.geChat.done(1, 'reset');
          nodes['composer-input'].value = 'with images'; nodes['composer-send'].click();
          finish({validAdds, rejected, sourcesBefore, tooLongAccepted, longError, calls});
        """)

        self.assertEqual(result["validAdds"], [True, True, True])
        self.assertEqual(result["rejected"], [False] * 7)
        self.assertEqual(
            [source.split(";", 1)[0] for source in result["sourcesBefore"]],
            ["data:image/png", "data:image/jpeg", "data:image/webp"],
        )
        self.assertFalse(result["tooLongAccepted"])
        self.assertEqual(result["longError"], "The question or images are too large.")
        asks = [call for call in result["calls"] if call[0] == "ask"]
        self.assertEqual(len(asks), 1)
        self.assertEqual(
            [source.split(";", 1)[0] for source in asks[0][3]],
            ["data:image/png", "data:image/webp"],
        )

    def test_image_count_and_decoded_aggregate_limits_fail_before_bridge(self):
        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:limits', providers: [{category: 'safe', data_region: 'safe',
              training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
              deletion_scope: 'safe'}]}, history: []});
          function large(last) {
            const bytes = Buffer.alloc(4 * 1024 * 1024); [137,80,78,71,13,10,26,10].forEach((v,i) => bytes[i]=v);
            bytes[bytes.length - 1] = last; return 'data:image/png;base64,' + bytes.toString('base64');
          }
          const accepted = [window.geChat.addImg(large(1)), window.geChat.addImg(large(2)), window.geChat.addImg(large(3))];
          const aggregateRejected = window.geChat.addImg('data:image/png;base64,iVBORw0KGgoBAA==');
          finish({accepted, aggregateRejected, calls, error: nodes['ge-input-error'].textContent});
        """)

        self.assertEqual(result["accepted"], [True, True, True])
        self.assertFalse(result["aggregateRejected"])
        self.assertEqual([call[0] for call in result["calls"]], ["chat_ready"])

        count_result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'legacy', state: 'ready', conversation: null,
            privacy_disclosure: null, history: []});
          function image(last) { return 'data:image/png;base64,' +
            Buffer.from([137,80,78,71,13,10,26,10,last]).toString('base64'); }
          const adds = [];
          for (let i = 1; i <= 6; i += 1) adds.push(window.geChat.addImg(image(i)));
          finish({adds, chips: nodes['ge-imgchips'].children.length});
        """)
        self.assertEqual(count_result["adds"], [True, True, True, True, True, False])
        self.assertEqual(count_result["chips"], 5)

    def test_paste_and_drop_keep_file_reader_results_in_selection_order(self):
        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'legacy', state: 'ready', conversation: null,
            privacy_disclosure: null, history: []});
          const png = 'data:image/png;base64,iVBORw0KGgoBAg==';
          const samePngAlternateBase64 = 'data:image/png;base64,iVBORw0KGgoBAh==';
          const jpeg = 'data:image/jpeg;base64,/9j/4AE=';
          const webp = 'data:image/webp;base64,UklGRgEAAABXRUJQAQ==';
          function item(type, dataUrl) { return {type, getAsFile() { return {type, size: 20, dataUrl}; }}; }
          nodes['composer-input'].dispatchEvent({type: 'paste', clipboardData: {items: [
            item('image/png', png), item('image/jpeg', jpeg), item('image/svg+xml', 'data:image/svg+xml;base64,PHN2Zz4=')
          ]}});
          nodes['ge-composer'].dispatchEvent({type: 'drop', dataTransfer: {files: [
            {type: 'image/png', size: 10, dataUrl: samePngAlternateBase64},
            {type: 'image/webp', size: 20, dataUrl: webp},
            {type: 'text/html', size: 20, dataUrl: 'data:text/html;base64,PGI+'},
            {type: 'image/png', size: 4, dataUrl: 'data:image/png;base64,/9j/4A=='}
          ]}});
          flushReaders();
          const sources = nodes['ge-imgchips'].children.map((chip) =>
            chip.children.find((child) => child.className === 'imgchip-open').children[0].src);
          nodes['composer-input'].value = 'legacy images'; nodes['composer-send'].click();
          finish({sources, asks: calls.filter((call) => call[0] === 'ask')});
        """)

        self.assertEqual(result["sources"], [
            "data:image/png;base64,iVBORw0KGgoBAg==",
            "data:image/jpeg;base64,/9j/4AE=",
            "data:image/png;base64,iVBORw0KGgoBAh==",
            "data:image/webp;base64,UklGRgEAAABXRUJQAQ==",
        ])
        self.assertEqual(len(result["asks"]), 1)
        self.assertEqual(result["asks"][0][3], result["sources"])

    def test_only_explicit_current_turn_images_cross_the_bridge(self):
        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 1, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:current-only', providers: [{category: 'safe', data_region: 'safe',
              training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
              deletion_scope: 'safe'}]}, history: [{turn_no: 1, status: 'completed', user_text: 'history source text',
                assistant_text: 'history answer', error_code: null, completed_at: 1,
                images: [{image_index: 0, media_type: 'image/png', size_bytes: 10,
                  sha256: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'}]}]});
          const selected = 'data:image/png;base64,iVBORw0KGgoBAh==';
          const canonical = 'data:image/png;base64,iVBORw0KGgoBAg==';
          window.geChat.addImg(selected);
          const firstChip = nodes['ge-imgchips'].children[0].children
            .find((child) => child.className === 'imgchip-open').children[0].src;
          window.crypto.randomUUID = () => '123e4567-e89b-42d3-a456-426614174001';
          nodes['composer-input'].value = 'first current turn'; nodes['composer-send'].click();
          window.geChat.done(1, 'first answer');
          window.crypto.randomUUID = () => '123e4567-e89b-42d3-a456-426614174002';
          nodes['composer-input'].value = 'second current turn'; nodes['composer-send'].click();
          finish({canonical, firstChip, asks: calls.filter((call) => call[0] === 'ask')});
        """)

        self.assertEqual(len(result["asks"]), 2)
        self.assertEqual(result["firstChip"], result["canonical"])
        self.assertEqual(result["asks"][0][3], [result["canonical"]])
        self.assertEqual(result["asks"][1][3], [])
        serialized = json.dumps(result["asks"])
        self.assertNotIn("history source text", serialized)
        self.assertNotIn("history answer", serialized)

    def test_late_failed_turn_clears_composer_images_before_the_next_submit(self):
        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:failure-clear', providers: [{category: 'safe', data_region: 'safe',
              training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
              deletion_scope: 'safe'}]}, history: []});
          const turnIds = ['123e4567-e89b-42d3-a456-426614174001',
                           '123e4567-e89b-42d3-a456-426614174002'];
          window.crypto.randomUUID = () => turnIds.shift();
          api.ask = function () {
            calls.push(['ask'].concat(Array.from(arguments)));
            return {ok: true};
          };
          window.geChat.addImg('data:image/png;base64,iVBORw0KGgoBAg==');
          nodes['composer-input'].value = 'first turn'; nodes['composer-send'].click();
          const chipsAfterSend = nodes['ge-imgchips'].children.length;
          Promise.resolve().then(() => Promise.resolve()).then(() => {
            window.geChat.error(1, 'model_unavailable');
            const chipsAfterFailure = nodes['ge-imgchips'].children.length;
            nodes['composer-input'].value = 'second turn'; nodes['composer-send'].click();
            finish({chipsAfterSend, chipsAfterFailure,
                    asks: calls.filter((call) => call[0] === 'ask')});
          });
        """)

        self.assertEqual(result["chipsAfterSend"], 0)
        self.assertEqual(result["chipsAfterFailure"], 0)
        self.assertEqual(len(result["asks"]), 2)
        self.assertEqual(result["asks"][0][3], ["data:image/png;base64,iVBORw0KGgoBAg=="])
        self.assertEqual(result["asks"][1][3], [])

    def test_file_queue_reserves_count_before_reading(self):
        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'legacy', state: 'ready', conversation: null,
            privacy_disclosure: null, history: []});
          function file(last) { return {type: 'image/png', size: 9,
            dataUrl: 'data:image/png;base64,' + Buffer.from([137,80,78,71,13,10,26,10,last]).toString('base64')}; }
          const files = []; for (let i = 1; i <= 7; i += 1) files.push(file(i));
          nodes['ge-composer'].dispatchEvent({type: 'drop', dataTransfer: {files}});
          flushReaders();
          finish({reads: readFiles.length, chips: nodes['ge-imgchips'].children.length,
            error: nodes['ge-input-error'].textContent});
        """)

        self.assertEqual(result["reads"], 5)
        self.assertEqual(result["chips"], 5)

    def test_empty_history_keeps_welcome_and_localizes_placeholder(self):
        result = _run_js(r"""
          const ok = window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:welcome', providers: [{category: 'safe', data_region: 'safe',
              training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
              deletion_scope: 'safe'}]}, history: []});
          finish({ok, scroll: nodes['ge-chat-scroll'].textContent,
            placeholder: nodes['composer-input'].placeholder, inputDisabled: nodes['composer-input'].disabled});
        """)

        self.assertTrue(result["ok"])
        self.assertEqual(result["scroll"], "Welcome")
        self.assertEqual(result["placeholder"], "Ask")
        self.assertFalse(result["inputDisabled"])

    def test_malformed_disclosure_fails_closed_without_partial_history(self):
        result = _run_js(r"""
          const ok = window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 1, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:bad', providers: [{category: '', data_region: 'safe',
              training_enabled: false, cache_ttl_seconds: -1, provider_retention_hours: 0,
              deletion_scope: 'safe'}]},
            history: [{turn_no: 1, status: 'completed', user_text: 'must not render', assistant_text: 'partial',
              error_code: null, completed_at: 1, images: []}]});
          finish({ok, scroll: nodes['ge-chat-scroll'].textContent,
            inputDisabled: nodes['composer-input'].disabled, status: nodes['ge-chat-status'].textContent});
        """)

        self.assertFalse(result["ok"])
        self.assertEqual(result["scroll"], "")
        self.assertTrue(result["inputDisabled"])
        self.assertEqual(result["status"], "The follow-up service returned an invalid response.")

    def test_duplicate_or_malformed_history_fails_without_rendering_a_prefix(self):
        result = _run_js(r"""
          const ok = window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 2, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:history', providers: [{category: 'safe', data_region: 'safe',
              training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
              deletion_scope: 'safe'}]}, history: [
              {turn_no: 1, status: 'completed', user_text: 'valid prefix', assistant_text: 'answer',
                error_code: null, completed_at: 1, images: []},
              {turn_no: 1, status: 'failed', user_text: 'duplicate', assistant_text: 'must be null',
                error_code: '<raw error>', completed_at: -1, images: []}
            ]});
          finish({ok, scroll: nodes['ge-chat-scroll'].textContent, inputDisabled: nodes['composer-input'].disabled});
        """)

        self.assertFalse(result["ok"])
        self.assertEqual(result["scroll"], "")
        self.assertTrue(result["inputDisabled"])

    def test_non_object_history_row_fails_closed_without_throwing(self):
        result = _run_js(r"""
          let threw = false, ok = null;
          try {
            ok = window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
              conversation: {status: 'active', turn_count: 1, max_turns: 40, expires_at: 1},
              privacy_disclosure: {version: 'v:null-history', providers: [{category: 'safe', data_region: 'safe',
                training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
                deletion_scope: 'safe'}]}, history: [null, {turn_no: 1, status: 'completed',
                  user_text: 'valid', assistant_text: 'answer', error_code: null, completed_at: 1, images: []}]});
          } catch (error) { threw = true; }
          finish({threw, ok, status: nodes['ge-chat-status'].textContent,
            inputDisabled: nodes['composer-input'].disabled});
        """)

        self.assertFalse(result["threw"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "The follow-up service returned an invalid response.")
        self.assertTrue(result["inputDisabled"])

    def test_completed_result_is_not_overwritten_by_late_cancel(self):
        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:race', providers: [{category: 'safe', data_region: 'safe',
              training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
              deletion_scope: 'safe'}]}, history: []});
          window.geChat.submit('race', [], null);
          window.geChat.done(1, 'authoritative answer');
          window.geChat.state(1, 'cancelled', {});
          window.geChat.error(1, 'cancelled');
          finish({messages: nodes['ge-chat-scroll'].children
            .filter((node) => node.className.indexOf('msg ') === 0)
            .map((node) => ({cls: node.className, text: node.textContent})),
            status: nodes['ge-chat-status'].textContent});
        """)

        self.assertEqual(result["messages"][-1], {"cls": "msg bot", "text": "authoritative answer"})
        self.assertEqual(result["status"], "Completed")

    def test_server_cancelled_state_is_terminal_without_user_cancel_bridge(self):
        result = _run_js(r"""
          window.geChat.bootstrap({ok: true, backend: 'server', state: 'ready',
            conversation: {status: 'active', turn_count: 0, max_turns: 40, expires_at: 1},
            privacy_disclosure: {version: 'v:cancel', providers: [{category: 'safe', data_region: 'safe',
              training_enabled: false, cache_ttl_seconds: 0, provider_retention_hours: 0,
              deletion_scope: 'safe'}]}, history: []});
          window.geChat.submit('cancel me', [], null);
          window.geChat.state(1, 'cancelled', {});
          finish({calls, busy: window.geChat.isBusy(), inputDisabled: nodes['composer-input'].disabled,
            last: nodes['ge-chat-scroll'].children.filter((node) => node.className.indexOf('msg ') === 0).slice(-1)[0].textContent});
        """)

        self.assertEqual([call[0] for call in result["calls"]], ["chat_ready", "ask"])
        self.assertFalse(result["busy"])
        self.assertFalse(result["inputDisabled"])
        self.assertEqual(result["last"], "The follow-up was cancelled.")


if __name__ == "__main__":
    unittest.main()
