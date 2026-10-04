"""
setup_runtime.py — one-shot setup of the native binaries the bot needs, so it
runs on a plain Python host (Render's native Python service, NO Docker).

Pure stdlib + pip. Does two things:
  1. Downloads a standalone Node.js Linux binary -> engines/node/node
     (used by the Prometheus engine). ~25 MB download.
  2. Builds the patched Luau runtime (`luau` + `luau-ast`) from source ->
     engines/deobf/deobf/bin/. Installs cmake + ninja via pip, clones
     Luau 0.739, applies the Vector3-metatable patch (via build_luau.py),
     and compiles. ~3-5 min.

Run once at build time:  python setup_runtime.py
Safe to re-run; existing binaries are kept.
"""
import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = os.path.join(HERE, "engines", "deobf", "deobf", "bin")
NODE_DIR = os.path.join(HERE, "engines", "node")
BUILD_LUAU = os.path.join(HERE, "engines", "deobf", "deobf", "build_luau.py")
LUAU_TAG = "0.739"

NODE_URL = "https://nodejs.org/dist/v20.18.1/node-v20.18.1-linux-x64.tar.xz"


def log(msg):
    print("[setup] " + msg, flush=True)


def _chmod_x(path):
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


# ---------------------------------------------------------------- node ------
def fetch_node():
    node_path = os.path.join(NODE_DIR, "node")
    if os.path.exists(node_path):
        log("node already present, skipping")
        return
    os.makedirs(NODE_DIR, exist_ok=True)
    log("downloading Node.js from " + NODE_URL)
    req = urllib.request.Request(NODE_URL, headers={"User-Agent": "deobfbot-setup"})
    data = urllib.request.urlopen(req, timeout=180).read()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:xz") as tf:
        member = next(m for m in tf.getmembers()
                      if m.name.endswith("/bin/node") and m.isfile())
        src = tf.extractfile(member)
        with open(node_path, "wb") as f:
            shutil.copyfileobj(src, f)
    _chmod_x(node_path)
    log("wrote: " + node_path)


# ---------------------------------------------------------------- luau ------
def fetch_luau():
    luau_path = os.path.join(BIN_DIR, "luau")
    if os.path.exists(luau_path):
        log("luau already present, skipping")
        return
    os.makedirs(BIN_DIR, exist_ok=True)

    # 1. make sure cmake + ninja are available (via pip if missing)
    for tool in ("cmake", "ninja"):
        if shutil.which(tool):
            continue
        log("installing %s via pip" % tool)
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", tool],
                       check=True)

    # 2. clone Luau at the pinned tag into a temp dir
    src = tempfile.mkdtemp(prefix="luau-src-")
    log("cloning Luau %s" % LUAU_TAG)
    subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", LUAU_TAG,
                    "https://github.com/luau-lang/luau.git", src], check=True)

    # 3. build_luau.py patches the source (Vector3 metatable writable) and
    #    builds + installs `luau` into bin/. --src keeps the checkout so we
    #    can build luau-ast in the same build dir; --portable = no -march=native.
    log("building patched luau (this takes a few minutes)...")
    subprocess.run([sys.executable, BUILD_LUAU, "--portable", "--src", src],
                   check=True, cwd=os.path.join(HERE, "engines", "deobf"))

    # 4. build every remaining target in the same dir -> gives luau-ast
    build_dir = os.path.join(src, "build-deobf")
    try:
        subprocess.run(["cmake", "--build", build_dir, "--config", "Release",
                        "--parallel"], check=True)
    except subprocess.CalledProcessError:
        log("  (full build had errors; luau itself was already installed)")

    # 5. copy luau-ast if it was produced
    for cand in (os.path.join(build_dir, "luau-ast"),
                 os.path.join(build_dir, "Release", "luau-ast")):
        if os.path.exists(cand):
            dest = os.path.join(BIN_DIR, "luau-ast")
            shutil.copy2(cand, dest)
            _chmod_x(dest)
            log("wrote: " + dest)
            break
    else:
        log("  luau-ast not produced (names polish step will be skipped)")

    shutil.rmtree(src, ignore_errors=True)
    log("luau setup complete: %s" % os.listdir(BIN_DIR))


def main():
    log("setting up native binaries (pure Python runtime mode)")
    fetch_node()
    fetch_luau()
    log("done.")


if __name__ == "__main__":
    main()
