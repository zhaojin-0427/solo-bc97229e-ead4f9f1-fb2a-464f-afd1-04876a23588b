"""Minimal Flask/werkzeug-compatible shim for offline web-layer testing.

This is TEST-ONLY scaffolding (the real deployment installs genuine Flask via
requirements.txt). It implements just enough of the Flask surface used by
app.server.app to drive the app over a real WSGI call, including Jinja-style
template rendering, multipart uploads, flash messages and error handlers.
"""
import io
import json
import os
import re
import threading
import urllib.parse
from http.cookies import SimpleCookie


# --------------------------------------------------------------------- Jinja
class _Template:
    def __init__(self, source, name, loader=None):
        self.name = name
        self.source = source
        self.loader = loader

    def render(self, ctx):
        blocks, child = self._extract_blocks(self.source)
        parent_m = re.search(r'\{\%\s*extends\s+[\'"]([^\'"]+)[\'"]\s*\%\}', self.source)
        if parent_m and self.loader is not None:
            parent = self.loader.get_template(parent_m.group(1))
            # Render the parent, but have {% block name %} nodes resolve to
            # the child's overridden block bodies.
            return _render(parent.source, ctx, parent.name,
                           blocks=blocks, loader=self.loader)
        return _render(self.source, ctx, self.name,
                       blocks=blocks, loader=self.loader)

    @staticmethod
    def _extract_blocks(source):
        """Return ({block_name: body_source}, source_without_block_wrappers).

        Only top-level blocks matter for the single-level inheritance used by
        these templates.
        """
        blocks = {}
        for m in re.finditer(
            r"\{\%\s*block\s+(\w+)\s*\%\}(.*?)\{\%\s*endblock\s*\%\}",
            source, re.S,
        ):
            blocks[m.group(1)] = m.group(2)
        return blocks, source


class _JinjaLike:
    def __init__(self, folder):
        self.folder = folder

    def get_template(self, name):
        with open(os.path.join(self.folder, name), encoding="utf-8") as fh:
            return _Template(fh.read(), name, loader=self)


_VAR_RE = re.compile(r"\{\{\s*(.*?)\s*\}\}")
_BLOCK_RE = re.compile(
    r"\{\%\s*(if|for|with|endwith|endif|endfor|else|elif|extends|block|endblock)\b(.*?)\%\}", re.S
)


def _eval(expr, ctx):
    expr = expr.strip()
    if expr in ("True", "true"):
        return True
    if expr in ("False", "false"):
        return False
    if expr in ("None", "none", "null"):
        return None
    if (expr.startswith("'") and expr.endswith("'")) or (
        expr.startswith('"') and expr.endswith('"')
    ):
        return expr[1:-1]
    # Comparison operators (lowest precedence we need).
    for op in ("==", "!=", ">=", "<=", ">", "<"):
        if op in expr:
            left, right = expr.split(op, 1)
            lv, rv = _eval(left, ctx), _eval(right, ctx)
            return {
                "==": lv == rv, "!=": lv != rv, ">=": lv >= rv,
                "<=": lv <= rv, ">": lv > rv, "<": lv < rv,
            }[op]
    if expr.startswith("not "):
        return not _eval(expr[4:], ctx)
    if " and " in expr:
        parts = expr.split(" and ")
        return all(_eval(p, ctx) for p in parts)
    if " or " in expr:
        parts = expr.split(" or ")
        return any(_eval(p, ctx) for p in parts)
    # function call: url_for(...) / state_label(...)
    m = re.match(r"([A-Za-z_][\w.]*)\((.*)\)$", expr)
    if m:
        fn = _resolve(m.group(1), ctx)
        raw_args = m.group(2)
        args, kwargs = _parse_call_args(raw_args, ctx)
        return fn(*args, **kwargs)
    m = re.match(r"(.+?)\s+if\s+(.+?)\s+else\s+(.+)$", expr)
    if m:
        return _eval(m.group(1), ctx) if _eval(m.group(2), ctx) else _eval(m.group(3), ctx)
    return _resolve(expr, ctx)


def _parse_call_args(raw, ctx):
    args, kwargs = [], {}
    if not raw.strip():
        return args, kwargs
    for part in _split_top(raw):
        part = part.strip()
        if "=" in part and not part.startswith(("'", '"')):
            k, v = part.split("=", 1)
            kwargs[k.strip()] = _eval(v, ctx)
        else:
            args.append(_eval(part, ctx))
    return args, kwargs


def _split_top(s):
    out, depth, cur, quote = [], 0, "", None
    for ch in s:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
            cur += ch
        elif ch in "([{":
            depth += 1
            cur += ch
        elif ch in ")]}":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return out


def _resolve(path, ctx):
    cur = ctx
    for token in path.split("."):
        if isinstance(cur, dict) and token in cur:
            cur = cur[token]
        elif hasattr(cur, token):
            cur = getattr(cur, token)
        else:
            # dict access on a non-dict fallback: try index
            try:
                cur = cur[token]
            except Exception as exc:  # noqa: BLE001
                raise UndefinedError(f"{path} ({token})") from exc
    return cur


class UndefinedError(Exception):
    pass


# ------------------------------------------------------------- template AST
def _tokenize(source):
    """Turn a template into a flat list of (kind, text) tokens."""
    tokens = []
    pos = 0
    for m in _BLOCK_RE.finditer(source):
        if m.start() > pos:
            tokens.append(("text", source[pos:m.start()]))
        tokens.append((m.group(1), m.group(2).strip()))
        pos = m.end()
    if pos < len(source):
        tokens.append(("text", source[pos:]))
    return tokens


def _parse(tokens):
    """Parse tokens into nested nodes: ('root', nodes)."""
    pos = 0

    def parse_block(stop):
        nonlocal pos
        nodes = []
        while pos < len(tokens):
            kind, payload = tokens[pos]
            if kind in stop:
                return nodes, kind, payload
            if kind in ("if", "for", "with"):
                opener = (kind, payload)
                pos += 1
                children, term_kind, term_payload = parse_block(
                    _ENDERS[kind] | {"elif", "else"}
                    if kind == "if" else _ENDERS[kind]
                )
                branches = []
                if kind == "if":
                    branches = [(payload, children)]
                    while term_kind in ("elif", "else"):
                        cond = term_payload if term_kind == "elif" else "True"
                        pos += 1
                        more, term_kind, term_payload = parse_block(
                            {"elif", "else", "endif"}
                        )
                        branches.append((cond, more))
                    # term_kind == 'endif'; consume it
                    pos += 1
                    nodes.append(("if", branches))
                else:
                    pos += 1  # consume end/endfor/endwith
                    nodes.append((kind, opener[1], children))
            elif kind == "text":
                nodes.append(("text", payload))
                pos += 1
            else:
                # unexpected lone control token; skip
                pos += 1
        return nodes, None, None

    nodes, _, _ = parse_block(set())
    return ("root", nodes)


_ENDERS = {"if": {"endif"}, "for": {"endfor"}, "with": {"endwith"}}


def _eval_node(node, ctx, name):
    kind = node[0]
    if kind == "root" or kind == "fragment":
        return "".join(_eval_node(n, ctx, name) for n in node[1])
    if kind == "text":
        return _render_vars(node[1], ctx)
    if kind == "if":
        for cond, children in node[1]:
            if _eval(cond, ctx):
                return "".join(_eval_node(n, ctx, name) for n in children)
        return ""
    if kind == "for":
        m = re.match(r"(.+?)\s+in\s+(.+)$", node[1])
        targets, iter_expr = m.group(1).strip(), m.group(2).strip()
        iterable = _eval(iter_expr, ctx)
        out = []
        target_names = [t.strip() for t in targets.split(",")]
        for n, item in enumerate(iterable):
            local = dict(ctx)
            if len(target_names) == 1:
                local[target_names[0]] = item
            else:
                # Jinja-style multi-target unpacking (e.g. "k, v in items").
                for tname, value in zip(target_names, item):
                    local[tname] = value
            local["loop"] = {"index0": n, "index": n + 1}
            out.append("".join(_eval_node(c, local, name) for c in node[2]))
        return "".join(out)
    if kind == "with":
        local = dict(ctx)
        m = re.match(r"(\w+)\s*=\s*(.+)$", node[1])
        if m:
            local[m.group(1)] = _eval(m.group(2), local)
        return "".join(_eval_node(c, local, name) for c in node[2])
    return ""


def _render(source, ctx, name, blocks=None, loader=None, block_stack=None):
    blocks = blocks or {}
    # Resolve template inheritance by substituting parent block nodes with
    # the child's overridden bodies (falling back to the parent's defaults).
    def block_repl(match):
        block_name, default_body = match.group(1), match.group(2)
        return blocks.get(block_name, default_body)

    source = re.sub(
        r"\{\%\s*block\s+(\w+)\s*\%\}(.*?)\{\%\s*endblock\s*\%\}",
        block_repl, source, flags=re.S,
    )
    # Any leftover extends directive in a child body is inert here.
    source = re.sub(r"\{\%\s*extends.*?\%\}", "", source, flags=re.S)
    tree = _parse(_tokenize(source))
    return _eval_node(tree, ctx, name)


def _render_vars(text, ctx):
    def repl(m):
        try:
            val = _eval(m.group(1), ctx)
        except UndefinedError:
            val = ""
        if val is None:
            return ""
        return str(val)
    return _VAR_RE.sub(repl, text)


# ----------------------------------------------------------------- requests
class _File:
    def __init__(self, filename, data):
        self.filename = filename
        self.stream = io.BytesIO(data)

    def save(self, path):
        with open(path, "wb") as fh:
            fh.write(self.stream.read())


class _MultiDict(dict):
    def get(self, key, default=None):
        return self[key] if key in self else default

    def getlist(self, key):
        return self.get(key, [])


class Request:
    def __init__(self, env, body):
        self.env = env
        self.method = env["REQUEST_METHOD"]
        self.path = env["PATH_INFO"] or "/"
        self.query = urllib.parse.parse_qs(env.get("QUERY_STRING", ""))
        self.files = _MultiDict()
        self.form = _MultiDict()
        self._parse(body, env)

    def _parse(self, body, env):
        ctype = env.get("CONTENT_TYPE", "")
        if ctype.startswith("multipart/form-data"):
            boundary = ctype.split("boundary=", 1)[1].encode()
            for part in body.split(b"--" + boundary):
                part = part.strip(b"\r\n")
                if not part or part == b"--":
                    continue
                head, _, content = part.partition(b"\r\n\r\n")
                headers = head.decode("utf-8", "replace")
                name_m = re.search(r'name="([^"]+)"', headers)
                if not name_m:
                    continue
                name = name_m.group(1)
                fn_m = re.search(r'filename="([^"]*)"', headers)
                if fn_m:
                    self.files[name] = _File(fn_m.group(1), content)
                else:
                    self.form[name] = content.decode("utf-8")
        elif ctype.startswith("application/x-www-form-urlencoded"):
            for k, vals in urllib.parse.parse_qs(body.decode()).items():
                self.form[k] = vals[0]


# -------------------------------------------------------------------- Flask
class _Rule:
    def __init__(self, rule, endpoint, methods):
        self.rule = rule
        self.endpoint = endpoint
        self.methods = set(methods or ["GET"])
        self.regex, self.params = self._compile(rule)

    @staticmethod
    def _compile(rule):
        params = re.findall(r"<(?:[^:>]+:)?(\w+)>", rule)
        pat = re.sub(r"<(?:[^:>]+:)?\w+>", r"([^/]+)", rule)
        return re.compile("^" + pat + "$"), params


def redirect(location):
    return ("", 302, {"Location": location})


def url_for(endpoint, **kwargs):
    rule = Flask._current.url_map_by_endpoint.get(endpoint)
    if rule is None:
        return f"/__endpoint__/{endpoint}"
    path = rule.rule
    for k, v in kwargs.items():
        path = re.sub(r"<(?:[^:>]+:)?%s>" % re.escape(k), str(v), path)
    return path


def render_template(name, **ctx):
    app = Flask._current
    full = dict(app.template_globals)
    # Names Jinja/Flask expose to every template.
    full.setdefault("url_for", url_for)
    full.setdefault("get_flashed_messages", get_flashed_messages)
    full.update(ctx)
    return app.jinja.get_template(name).render(full)


def send_file(path, as_attachment=False, download_name=None, mimetype=None):
    with open(path, "rb") as fh:
        data = fh.read()
    headers = {"Content-Type": mimetype or "application/octet-stream"}
    if as_attachment and download_name:
        headers["Content-Disposition"] = (
            f"attachment; filename*=UTF-8''{urllib.parse.quote(download_name)}"
        )
    return (data, 200, headers)


def abort(code, description=None):
    raise HttpError(code, description or "")


def jsonify(payload):
    import json
    return (json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            200, {"Content-Type": "application/json"})


class HttpError(Exception):
    def __init__(self, code, description):
        super().__init__(description)
        self.code = code
        self.description = description


class _Flash:
    """Cookie-backed flash session, mirroring Flask's session-cookie behavior.

    Messages stored during one request are serialized into a Set-Cookie on the
    response; the next request reads them back and the cookie is cleared. This
    makes flash survive the redirect-then-GET pattern the app relies on.
    """

    COOKIE = "anon_flash"

    def __init__(self):
        self.incoming = []
        self.outgoing = []

    def load(self, cookie_header):
        jar = SimpleCookie()
        if cookie_header:
            jar.load(cookie_header)
        if self.COOKIE in jar:
            import base64
            try:
                raw = base64.b64decode(jar[self.COOKIE].value).decode("utf-8")
                self.incoming = json.loads(raw)
            except Exception:  # noqa: BLE001
                self.incoming = []

    def add(self, category, message):
        self.outgoing.append([category, message])

    def take(self, with_categories):
        items = list(self.incoming)
        self.incoming = []
        return items if with_categories else [m for _, m in items]

    def cookie_headers(self):
        import base64
        if not self.outgoing:
            return {}
        raw = base64.b64encode(
            json.dumps(self.outgoing, ensure_ascii=False).encode("utf-8")
        ).decode()
        return {"Set-Cookie": f"{self.COOKIE}={raw}; Path=/"}


def flash(message, category="message"):
    Flask._current._flash.add(category, message)


def get_flashed_messages(with_categories=False):
    return Flask._current._flash.take(with_categories)


class Flask:
    _current = None

    def __init__(self, name):
        self.name = name
        self.config = {}
        self.rules = []
        self.url_map_by_endpoint = {}
        self.error_handlers = {}
        self.context_processors = []
        self.jinja = _JinjaLike(
            os.path.join(os.path.dirname(__file__), "..", "..",
                         "app", "server", "templates")
        )
        self.jinja.folder = os.path.abspath(self.jinja.folder)
        self._flash = _Flash()
        self.template_globals = {}
        self._views = {}
        self._request = threading.local()
        Flask._current = self

    @property
    def request(self):
        return self._request.value

    def route(self, rule, methods=None, **_kw):
        def deco(fn):
            endpoint = fn.__name__
            r = _Rule(rule, endpoint, methods)
            self.rules.append(r)
            self.url_map_by_endpoint[endpoint] = r
            self._views[endpoint] = fn
            return fn
        return deco

    def post(self, rule, **kw):
        return self.route(rule, methods=["POST"], **kw)

    def errorhandler(self, code):
        def deco(fn):
            self.error_handlers[code] = fn
            return fn
        return deco

    def context_processor(self, fn):
        self.context_processors.append(fn)
        return fn

    def run_wsgi(self, env, body):
        self._flash = _Flash()
        self._flash.load(env.get("HTTP_COOKIE", ""))
        for proc in self.context_processors:
            self.template_globals.update(proc())
        req = Request(env, body)
        self._request.value = req
        max_len = self.config.get("MAX_CONTENT_LENGTH")
        if max_len and int(env.get("CONTENT_LENGTH", 0)) > max_len:
            handler = self.error_handlers.get(413)
            if handler:
                return self._normalize(handler(None))
        for rule in self.rules:
            m = rule.regex.match(req.path)
            if m and req.method in rule.methods:
                kwargs = dict(zip(rule.params, m.groups()))
                Flask._current = self
                try:
                    result = self._normalize(
                        self.view_for(rule.endpoint)(**kwargs)
                    )
                except HttpError as exc:
                    handler = self.error_handlers.get(exc.code)
                    if handler:
                        result = self._normalize(handler(exc))
                    else:
                        result = (exc.description.encode(), exc.code, {})
                result[2].update(self._flash.cookie_headers())
                return result
        return (b"not found", 404, {})

    @staticmethod
    def _normalize(result):
        # Accept a bare string/template body, or a (body, status, headers)
        # tuple, and always return the three-element WSGI-shaped form.
        if isinstance(result, tuple):
            data = result[0]
            status = result[1] if len(result) > 1 else 200
            headers = result[2] if len(result) > 2 else {}
        else:
            data, status, headers = result, 200, {}
        if isinstance(data, str):
            data = data.encode("utf-8")
        return data, status, dict(headers)

    def view_for(self, endpoint):
        return self._views[endpoint]

    def register_views(self, module_globals):
        self._views = {
            name: fn for name, fn in module_globals.items()
            if callable(fn) and name in self.url_map_by_endpoint
        }


# Expose names the application imports.
Response = object


class _RequestProxy:
    def __getattr__(self, item):
        return getattr(Flask._current.request, item)


request = _RequestProxy()
