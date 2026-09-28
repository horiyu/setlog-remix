#!/usr/bin/env python3
"""Receive a video from the iOS Shortcut, render it in a format, and pass it on.

GET  /formats  ?repo=owner/name[@ref]   the repository's formats, in list order:
               {"choices": [label, ...], "formats": [{id, name, description, label}]}
               (repo defaults to DEFAULT_REPO)
GET  /rooms                             the setlog rooms one can pick, default first
POST /log      ?repo=&format=&room=&caption=[&dry=1]   body: the video
               render in the background, then post (the usual one-step path)
POST /preview  ?repo=&format=&caption=  body: the video
               render now and hold it: {"job": ..., "video": "/video/<job>?token=..."}
GET  /video/<job>                       the rendered mp4 of a held job
POST /send     ?job=&room=&caption=[&dry=1]   post a held job
GET  /health   "ok"
Every call but /health needs the token (?token= or an X-Token header). format may be
the format's id or its label from /formats. worker.sh renders and hands the video to
setlog-post (SETLOG_POST in settings.conf), which posts it.
Runs under systemd socket activation and quits after IDLE_SECONDS without a request.
"""
import datetime, email, email.policy, json, os, re, shutil, socket, subprocess, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit, parse_qs, quote

HOME = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HOME)
import render, repo  # noqa: E402

QUEUE, HELD, STATE = (os.path.join(HOME, d) for d in ("queue", "held", "state"))
MAX_BYTES = 400 * 1024 * 1024
conf = repo.conf


def token():
    try:
        with open(os.path.join(STATE, "token"), encoding="utf-8") as fh:
            return fh.read().strip() or None
    except FileNotFoundError:          # no token yet: refuse everything (README)
        return None


def log(msg):
    with open(os.path.join(STATE, "server.log"), "a", encoding="utf-8") as fh:
        fh.write(f"{datetime.datetime.now():%F %T} {msg}\n")


def setlog_post():
    return os.path.expanduser(conf().get("SETLOG_POST", "~/dev/setlog-post/setlog-post"))


def rooms():
    """(default, rooms) as setlog-post knows them: the rooms it can tick, default first."""
    try:
        r = subprocess.run([setlog_post(), "rooms"], capture_output=True, text=True, timeout=20)
        d = json.loads(r.stdout)
        return d["default"], d["rooms"]
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as e:
        log(f"setlog-post rooms failed: {e}")
        raise repo.RepoError("PC の setlog-post が見つかりません（settings.conf の SETLOG_POST）") from None


def pick_format(repo_spec, wanted):
    """The format named by id or by its /formats label -> (sha, format)."""
    sha, good, bad = repo.formats(repo_spec)
    wanted = (wanted or "").strip()
    for f in good:
        if wanted in (f["id"], repo.label(f), f["name"]):
            return sha, f
    if wanted in bad:
        raise repo.RepoError(f"フォーマット {wanted} は壊れています: {bad[wanted]}")
    raise repo.RepoError(f"フォーマット {wanted!r} がありません（{', '.join(f['id'] for f in good)}）")


def new_job(base):
    job = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    n = 0
    while os.path.exists(os.path.join(base, job + (f"-{n}" if n else ""))):
        n += 1
    job += f"-{n}" if n else ""
    os.makedirs(os.path.join(base, job))
    return job


def start_worker():
    subprocess.Popen(["setsid", os.path.join(HOME, "worker.sh")], cwd=HOME,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "setlog-remix/1"

    def reply(self, code, obj):
        body = (json.dumps(obj, ensure_ascii=False) + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def fail(self, code, message, **extra):
        log(f"  -> {code} {message}")
        self.reply(code, {"ok": False, "error": message, "message": message, **extra})

    def log_message(self, fmt, *args):  # server.log has what matters
        pass

    def query(self):
        url = urlsplit(self.path)
        q = {k: v[0].strip() for k, v in parse_qs(url.query).items()}
        for key in ("token", "repo", "format", "room", "caption", "job", "dry"):
            h = self.headers.get("X-" + key.capitalize())
            if not q.get(key) and h:
                try:
                    h = h.encode("latin-1").decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
                from urllib.parse import unquote
                q[key] = unquote(h).strip()
        return url.path, q

    def authorized(self, q):
        if token() and q.get("token") == token():
            return True
        log(f"  rejected: bad token from {self.client_address[0]}")
        self.reply(403, {"ok": False, "error": "bad token", "message": "合言葉が違います。"})
        return False

    def do_GET(self):
        path, q = self.query()
        log(f"GET {path} from {self.client_address[0]}")
        if path == "/health":
            return self.reply(200, {"ok": True})
        if not self.authorized(q):
            return
        try:
            if path == "/formats":
                spec = q.get("repo") or conf().get("DEFAULT_REPO", "horiyu/setlog-formats")
                sha, good, bad = repo.formats(spec)
                items = [{"id": f["id"], "name": f["name"], "description": f["description"],
                          "label": repo.label(f), "seconds": f["clip"]["duration"]} for f in good]
                log(f"  {spec}@{sha[:7]}: {len(good)} formats, {len(bad)} broken")
                return self.reply(200, {"ok": True, "repo": spec, "sha": sha, "formats": items,
                                        "choices": [i["label"] for i in items], "broken": bad})
            if path == "/rooms":
                default, names = rooms()
                return self.reply(200, {"ok": True, "default": default, "rooms": names})
            m = re.fullmatch(r"/video/([\w-]+)(?:\.mp4)?", path)
            if m:
                f = os.path.join(HELD, m.group(1), "out.mp4")
                if not os.path.isfile(f):
                    return self.fail(404, "その動画はありません（もう送ったか、古くて消えました）")
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(os.path.getsize(f)))
                self.end_headers()
                with open(f, "rb") as fh:
                    shutil.copyfileobj(fh, self.wfile)
                return
        except repo.RepoError as e:
            return self.fail(400, str(e))
        self.fail(404, "not found")

    def read_video(self, q):
        """(bytes, ext) from a raw body or a multipart form (whose fields join q)."""
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BYTES:
            return None, "動画が大きすぎます（400MB まで）。"
        body = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type", "")
        log(f"  {len(body)} bytes, {ctype[:40]}")
        video, ext = body, "mov" if "quicktime" in ctype else "mp4"
        if ctype.startswith("multipart/form-data"):
            video = None
            msg = email.message_from_bytes(b"Content-Type: " + ctype.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body,
                                           policy=email.policy.HTTP)
            for part in msg.iter_parts():
                name = part.get_param("name", header="content-disposition")
                data = part.get_payload(decode=True)
                if part.get_filename() or name == "video":
                    video = data
                    m = re.search(r"\.(mp4|mov|m4v)$", (part.get_filename() or "").lower())
                    ext = m.group(1) if m else ext
                elif name and not q.get(name):
                    q[name] = (data or b"").decode("utf-8", "replace").strip()
        if not video or len(video) < 10_000:
            return None, "動画が届いていません。本文を「ファイル」にして、撮った動画を入れてください。"
        return video, ext

    def do_POST(self):
        path, q = self.query()
        log(f"POST {path} from {self.client_address[0]}: {self.headers.get('Content-Length', 0)} bytes")
        if path not in ("/log", "/preview", "/send"):
            return self.fail(404, "not found")
        # A multipart form carries the token among its fields, so read the body first.
        video, ext = self.read_video(q) if path != "/send" else (None, "")
        if not self.authorized(q):
            return
        try:
            if path == "/send":
                return self.send_held(q)
            if video is None:
                return self.fail(400, ext)
            spec = q.get("repo") or conf().get("DEFAULT_REPO", "horiyu/setlog-formats")
            sha, fmt = pick_format(spec, q.get("format") or "plain")
            chosen = self.rooms(q) if path == "/log" else []
        except repo.RepoError as e:
            return self.fail(400, str(e))
        job = new_job(HELD if path == "/preview" else QUEUE)
        d = os.path.join(HELD if path == "/preview" else QUEUE, job)
        with open(os.path.join(d, "input." + ext), "wb") as fh:
            fh.write(video)
        meta = {"repo": spec, "sha": sha, "format": fmt["id"], "format_name": fmt["name"],
                "caption": q.get("caption", ""), "rooms": chosen, "dry": q.get("dry") == "1",
                "received": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}
        write_job(d, meta)
        log(f"  job {job}: {fmt['id']} from {spec}@{sha[:7]}, rooms={meta['rooms']}, caption={meta['caption']!r}")
        if path == "/log":
            start_worker()
            return self.reply(200, {"ok": True, "job": job, "format": fmt["name"], "rooms": meta["rooms"],
                                    "message": f"受け取りました（{fmt['name']} → {', '.join(meta['rooms'])}）。"
                                               "数分で setlog に上がります。"})
        # /preview: render now so the Shortcut can show it before sending.
        t0 = time.time()
        try:
            render.run_job(d)
        except (render.FormatError, RuntimeError, repo.RepoError) as e:
            log(f"  render failed: {e}")
            shutil.rmtree(d, ignore_errors=True)
            return self.fail(500, f"描けませんでした: {str(e)[:300]}")
        log(f"  rendered in {time.time() - t0:.1f}s")
        prune(HELD, 5)
        return self.reply(200, {"ok": True, "job": job, "format": fmt["name"],
                                "video": f"/video/{job}?token={quote(q['token'])}",
                                "message": f"{fmt['name']} で描きました。"})

    def rooms(self, q):
        names = [r.strip() for r in re.split(r"[,\n]", q.get("room", "")) if r.strip()]
        default, known = rooms()
        unknown = [n for n in names if n not in known]
        if unknown:
            raise repo.RepoError(f"ルーム {', '.join(unknown)} は登録されていません")
        return names or [default]

    def send_held(self, q):
        job = q.get("job", "")
        src = os.path.join(HELD, job)
        if not re.fullmatch(r"[\w-]+", job) or not os.path.isfile(os.path.join(src, "out.mp4")):
            return self.fail(404, "その動画はありません（もう送ったか、古くて消えました）")
        meta = read_job(src)
        meta["rooms"] = self.rooms(q)
        if "caption" in q:
            meta["caption"] = q["caption"]
        meta["dry"] = q.get("dry") == "1"
        write_job(src, meta)
        shutil.move(src, os.path.join(QUEUE, job))
        start_worker()
        log(f"  send {job}: rooms={meta['rooms']}")
        return self.reply(200, {"ok": True, "job": job, "rooms": meta["rooms"],
                                "message": f"送ります（{', '.join(meta['rooms'])}）。数分で setlog に上がります。"})


def read_job(d):
    with open(os.path.join(d, "job.json"), encoding="utf-8") as fh:
        return json.load(fh)


def write_job(d, meta):
    with open(os.path.join(d, "job.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=1)


def prune(base, keep):
    jobs = sorted(os.listdir(base))
    for j in jobs[:max(0, len(jobs) - keep)]:
        shutil.rmtree(os.path.join(base, j), ignore_errors=True)


class Server(HTTPServer):
    allow_reuse_address = True

    def handle_timeout(self):
        log("idle; exiting")
        raise SystemExit(0)


def main():
    for d in (STATE, QUEUE, HELD):
        os.makedirs(d, exist_ok=True)
    if not os.path.exists(os.path.join(HOME, "settings.conf")):
        shutil.copyfile(os.path.join(HOME, "settings.conf.example"), os.path.join(HOME, "settings.conf"))
    c = conf()
    if "--port" in sys.argv:
        srv = Server(("127.0.0.1", int(sys.argv[sys.argv.index("--port") + 1])), Handler)
    elif os.environ.get("LISTEN_FDS"):
        srv = Server(("127.0.0.1", 0), Handler, bind_and_activate=False)
        srv.socket = socket.fromfd(3, socket.AF_INET, socket.SOCK_STREAM)
        srv.server_address = srv.socket.getsockname()
    else:
        srv = Server(("127.0.0.1", int(c.get("PORT", 8091))), Handler)
    srv.timeout = int(c.get("IDLE_SECONDS", 600))
    log(f"listening on {srv.server_address}")
    while True:
        srv.handle_request()


if __name__ == "__main__":
    main()
