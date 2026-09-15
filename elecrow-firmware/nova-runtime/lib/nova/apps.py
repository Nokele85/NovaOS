# Nova's apps: one folder per app, `main.py` inside, on the TF card when one is
# mounted and in flash otherwise. `_lib/` beside them holds modules she wrote
# to reuse; it is on sys.path for every app.
#
# Names are checked here as well as on the host: this module is the last
# thing between a model-written string and the filesystem.
import os
import sys

from . import sd

LIB = "_lib"
MAX_NAME = 32


def _ok_name(name, first_extra=""):
    if not name or len(name) > MAX_NAME:
        return False
    first = "abcdefghijklmnopqrstuvwxyz0123456789" + first_extra
    rest = first + "_-."
    return name[0] in first and all(c in rest for c in name) and ".." not in name


def root():
    return sd.MOUNT + "/apps" if sd.status() == "mounted" else "/apps"


def _mkdirs(path):
    cur = ""
    for part in path.strip("/").split("/"):
        cur += "/" + part
        try:
            os.mkdir(cur)
        except OSError:
            pass


def app_dir(name):
    if name != LIB and not _ok_name(name):
        raise ValueError("bad app name: %r" % name)
    return root() + "/" + name


def file_path(name, filename):
    if not _ok_name(filename, first_extra="ABCDEFGHIJKLMNOPQRSTUVWXYZ_"):
        raise ValueError("bad file name: %r" % filename)
    return app_dir(name) + "/" + filename


def list_apps():
    out = []
    try:
        names = sorted(os.listdir(root()))
    except OSError:
        return out
    for name in names:
        d = root() + "/" + name
        files, size = [], 0
        try:
            for f in sorted(os.listdir(d)):
                s = os.stat(d + "/" + f)[6]
                files.append(f)
                size += s
        except OSError:
            continue
        out.append({"name": name, "files": files, "bytes": size})
    return out


def run(name):
    """Run an app's main.py (and its main(), if it defines one)."""
    import nova
    d = app_dir(name)
    path = d + "/main.py"
    with open(path) as f:
        src = f.read()
    added = [p for p in (d, root() + "/" + LIB) if p not in sys.path]
    for p in added:
        sys.path.insert(0, p)
    g = {"__name__": "__main__", "nova": nova, "APP_DIR": d}
    try:
        exec(compile(src, path, "exec"), g)
        if callable(g.get("main")):
            g["main"]()
    finally:
        for p in added:
            try:
                sys.path.remove(p)
            except ValueError:
                pass


def ensure_root():
    _mkdirs(root() + "/" + LIB)
    return root()
