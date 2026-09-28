"""Fetch a formats repository from GitHub and list its formats.

A repository is named "owner/name", optionally "owner/name@ref" (branch, tag or
commit), or pasted as a github.com URL. Formats live in formats/<id>/format.json.
Each commit is downloaded once, as a tarball, into cache/<owner>/<name>/<sha>/.
A private repository needs GITHUB_TOKEN in settings.conf (or a logged-in `gh`).
"""
import io, json, os, re, shutil, subprocess, tarfile, time, urllib.error, urllib.request

HOME = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HOME, "cache")
MAX_TARBALL = 60 * 1024 * 1024
MAX_UNPACKED = 150 * 1024 * 1024
KEEP = 3                     # commits kept per repository
_resolved = {}               # (repo, ref) -> (sha, time), so a list then a post costs one API call


class RepoError(Exception):
    """Why a repository cannot be used; the message goes back to the Shortcut."""


def conf():
    out = {}
    path = os.path.join(HOME, "settings.conf")
    if not os.path.exists(path):
        path += ".example"
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"')
    return out


def parse(spec):
    """'owner/name[@ref]' or a github.com URL -> (owner, name, ref or None)."""
    spec = (spec or "").strip()
    m = re.fullmatch(r"(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/tree/([\w./-]+))?/?", spec)
    if m:
        return m.group(1), m.group(2), m.group(3)
    m = re.fullmatch(r"([\w.-]+)/([\w.-]+)(?:@([\w./-]+))?", spec)
    if not m or ".." in spec:
        raise RepoError(f"リポジトリの書き方が違います: {spec!r}（owner/name か owner/name@branch）")
    return m.group(1), m.group(2), m.group(3)


def allowed(owner, name):
    allow = conf().get("ALLOWED_REPOS", "").split()
    if allow and not any(a in (f"{owner}/{name}", f"{owner}/*") for a in allow):
        raise RepoError(f"{owner}/{name} は ALLOWED_REPOS にありません")


def token():
    t = os.environ.get("GITHUB_TOKEN") or conf().get("GITHUB_TOKEN", "")
    if t:
        return t
    gh = shutil.which("gh") or os.path.expanduser("~/.local/bin/gh")
    if os.path.exists(gh):
        try:
            r = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=10)
            return r.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None
    return None


def _get(url, accept, limit):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "setlog-remix"})
    t = token()
    if t:
        req.add_header("Authorization", f"Bearer {t}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read(limit + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RepoError("リポジトリか ref が見つかりません（private なら GITHUB_TOKEN を設定）") from None
        if e.code in (401, 403):
            raise RepoError(f"GitHub に断られました（{e.code}）。トークンか API の回数制限を確認") from None
        raise RepoError(f"GitHub がエラーを返しました（{e.code}）") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise RepoError(f"GitHub に届きません: {e}") from None
    if len(data) > limit:
        raise RepoError("リポジトリが大きすぎます")
    return data


def resolve(owner, name, ref):
    key = (owner, name, ref)
    hit = _resolved.get(key)
    if hit and time.time() - hit[1] < 60:
        return hit[0]
    if ref and re.fullmatch(r"[0-9a-f]{40}", ref):
        sha = ref
    else:
        sha = _get(f"https://api.github.com/repos/{owner}/{name}/commits/{ref or 'HEAD'}",
                   "application/vnd.github.sha", 100).decode().strip()
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise RepoError("コミットを特定できませんでした")
    _resolved[key] = (sha, time.time())
    return sha


def _unpack(data, dest):
    """Regular files and folders only, nothing outside dest, the top folder stripped."""
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            parts = m.name.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue
            rel = parts[1]
            if rel.startswith("/") or ".." in rel.split("/"):
                continue
            path = os.path.join(dest, rel)
            if m.isdir():
                os.makedirs(path, exist_ok=True)
            elif m.isfile():
                total += m.size
                if total > MAX_UNPACKED:
                    raise RepoError("リポジトリが大きすぎます")
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with tar.extractfile(m) as src, open(path, "wb") as out:
                    shutil.copyfileobj(src, out)
            # symlinks, devices and the like are skipped


def fetch(spec, sha=None):
    """The local folder holding the repository at that commit (downloaded if needed)."""
    owner, name, ref = parse(spec)
    allowed(owner, name)
    sha = sha or resolve(owner, name, ref)
    base = os.path.join(CACHE, owner, name)
    dest = os.path.join(base, sha)
    if not os.path.isdir(dest):
        data = _get(f"https://api.github.com/repos/{owner}/{name}/tarball/{sha}", "application/vnd.github+json", MAX_TARBALL)
        tmp = dest + f".part{os.getpid()}"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        try:
            _unpack(data, tmp)
            os.rename(tmp, dest)
        except FileExistsError:
            shutil.rmtree(tmp, ignore_errors=True)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        old = sorted((d for d in os.listdir(base) if ".part" not in d and d != sha),
                     key=lambda d: os.path.getmtime(os.path.join(base, d)))
        for d in old[:max(0, len(old) - (KEEP - 1))]:
            shutil.rmtree(os.path.join(base, d), ignore_errors=True)
    os.utime(dest)
    return dest


def formats(spec):
    """(sha, [format dicts], {id: error}) for the repository's formats/ folder."""
    import render
    owner, name, ref = parse(spec)
    allowed(owner, name)
    sha = resolve(owner, name, ref)
    root = fetch(spec, sha)
    top = os.path.join(root, "formats")
    good, bad = [], {}
    for fid in sorted(os.listdir(top)) if os.path.isdir(top) else []:
        d = os.path.join(top, fid)
        if not os.path.isfile(os.path.join(d, "format.json")):
            continue
        try:
            good.append(render.load(d, root))
        except (render.FormatError, OSError) as e:
            bad[fid] = str(e)
    if not good and not bad:
        raise RepoError(f"{owner}/{name} に formats/<名前>/format.json がありません")
    good.sort(key=lambda f: (f["order"], f["id"]))
    return sha, good, bad


def label(f):
    """What the Shortcut's list shows; the server maps it back to the format."""
    return f"{f['name']} — {f['description']}" if f["description"] else f["name"]


if __name__ == "__main__":
    import sys
    sha, good, bad = formats(sys.argv[1])
    print(sha)
    for f in good:
        print(f"  {f['id']:12} {label(f)}")
    for k, v in bad.items():
        print(f"  {k:12} ERROR {v}")
    print(json.dumps({"ok": True}))
