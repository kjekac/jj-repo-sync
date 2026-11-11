"""Publish workspace tips and fetch peer repositories through Jujutsu."""

import argparse
import base64
import contextlib
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
from urllib.parse import quote

JJ = "@jj@"
GIT = "@git@"
SSH = "@ssh@"
KEYGEN = "@keygen@"


class SyncError(Exception):
    pass


def run(args, show_stderr=False, timeout=120, **kwargs):
    result = subprocess.run(
        [str(arg) for arg in args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=None if show_stderr else subprocess.PIPE,
        timeout=timeout,
        **kwargs,
    )
    if result.returncode:
        raise SyncError((result.stderr or "").strip() or f"Command failed: {args[0]}")
    return result.stdout.strip()


def jj(repo, *args, snapshot=False, env=None, interactive=False):
    return run(
        [
            JJ,
            "-R",
            repo,
            "--no-pager",
            "--color=never",
            *([] if snapshot else ["--ignore-working-copy"]),
            *args,
        ],
        env=env,
        show_stderr=interactive,
        timeout=None if interactive else 120,
    )


def write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
        try:
            file.write(text)
            file.close()
            os.replace(file.name, path)
        finally:
            Path(file.name).unlink(missing_ok=True)


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def valid_id(value):
    # Paths are passed as data, never evaluated by a shell.
    return (
        isinstance(value, str)
        and len(value) < 1024
        and not any(ord(c) < 32 or ord(c) == 127 for c in value)
        and all(p not in {"", ".", ".."} for p in value.split("/"))
    )


class Sync:
    def __init__(self, config):
        self.config = config
        self.host = config["host"]
        self.state = Path(config["state"])
        if "roots" in config:
            raise SyncError("Legacy unrestricted roots config: use repositoryRoots and an explicit repositories list")
        self.roots = {name: Path(path) for name, path in config.get("repositoryRoots", {}).items()}
        repositories = config.get("repositories", [])
        if not isinstance(repositories, list) or any(not valid_id(value) for value in repositories):
            raise SyncError("repositories must be an explicit list of repository IDs")
        self.repositories = frozenset(repositories)
        self.peers = config["peers"]
        self.key = self.state / "identity"

    @contextlib.contextmanager
    def lock(self):
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.state / "lock").open("a") as file:
            fcntl.flock(file, fcntl.LOCK_EX)
            yield

    def setup(self):
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.key.exists():
            run(
                [
                    KEYGEN,
                    "-q",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-C",
                    f"jj-repo-sync@{self.host}",
                    "-f",
                    self.key,
                ]
            )

    def public_key(self):
        # Derive it from the existing private key, including after interrupted setup.
        self.setup()
        return " ".join(run([KEYGEN, "-y", "-f", self.key]).split()[:2])

    def accept_key(self, peer, value):
        if peer not in self.peers:
            raise SyncError(f"Unknown peer: {peer}")
        parts = value.split()
        try:
            raw = base64.b64decode(parts[1], validate=True)
            valid = (
                parts[0] == "ssh-ed25519"
                and len(parts) == 2
                and raw[:19] == b"\0\0\0\x0bssh-ed25519\0\0\0\x20"
                and len(raw) == 51
            )
        except (ValueError, IndexError):
            valid = False
        if not valid:
            raise SyncError("Expected one Ed25519 public key")
        target = self.state / "peers" / f"{peer}.pub"
        if target.exists() and target.read_text().strip() != value.strip():
            raise SyncError(f"Key changed for {peer}; revoke the old pairing first")
        write_atomic(target, value.strip() + "\n")
        self.authorized_keys()

    def authorized_keys(self):
        keys = []
        for peer in sorted(self.peers):
            path = self.state / "peers" / f"{peer}.pub"
            if path.exists():
                keys.append("restrict " + path.read_text().strip() + f" {peer}\n")
        write_atomic(self.state / "authorized_keys", "".join(keys))

    def pair(self, peer):
        if peer not in self.peers:
            raise SyncError(f"Unknown peer: {peer}")
        # This connection deliberately uses the normal, touch-required SSH config.
        command = shlex.join([self.config["remoteCommand"], "pair-receive", self.host])
        value = run(
            [
                SSH,
                "-oControlMaster=no",
                "-oControlPath=none",
                "-oForwardAgent=no",
                "-oControlPersist=no",
                self.peers[peer]["sshAlias"],
                command,
            ],
            input=self.public_key() + "\n",
            show_stderr=True,
            timeout=None,
        )
        self.accept_key(peer, value)

    def discover(self):
        repos = {}
        for name, root in self.roots.items():
            if not root.is_dir():
                continue
            for path, dirs, files in os.walk(root, followlinks=False):
                current = Path(path)
                relative = current.relative_to(root).as_posix()
                repo_id = name if relative == "." else f"{name}/{relative}"
                if not any(value == repo_id or value.startswith(repo_id + "/")
                           for value in self.repositories):
                    dirs[:] = []
                    continue
                if ".jj-sync-ignore" in files:
                    dirs[:] = []
                    continue
                is_jj = (current / ".jj").is_dir()
                is_git = (current / ".git").exists()
                is_bare = {"objects", "refs"} <= set(dirs) and "HEAD" in files
                if is_jj or is_git or is_bare:
                    if repo_id in self.repositories:
                        repos[repo_id] = {"path": current, "jj": is_jj}
                    dirs[:] = []
                else:
                    dirs[:] = sorted(
                        d
                        for d in dirs
                        if not d.startswith(".")
                        and d not in {"node_modules", "target", "vendor"}
                        and not (current / d).is_symlink()
                        and any(value == f"{repo_id}/{d}" or value.startswith(f"{repo_id}/{d}/")
                                for value in self.repositories)
                    )
        return repos

    def git_dir(self, repo):
        path = repo["path"]
        if repo["jj"]:
            result = Path(jj(path, "--at-op=@", "git", "root"))
        elif (path / ".git").is_file():
            text = (path / ".git").read_text()
            if not text.startswith("gitdir: "):
                raise SyncError("Invalid Git directory pointer")
            result = path / text[8:].strip()
        else:
            result = path / ".git" if (path / ".git").is_dir() else path
        result = result.resolve(strict=True)
        if not any(
            result.is_relative_to(root.resolve()) for root in self.roots.values()
        ):
            raise SyncError("Git directory is outside configured repository roots")
        return result

    def catalog(self):
        return {repo_id: {"jj": repo["jj"]} for repo_id, repo in self.discover().items()}

    def serve(self, command):
        # No shell evaluation. The only protocol operations are list and upload-pack.
        if command == "jj-repo-sync-list":
            print(json.dumps(self.catalog()))
            return
        try:
            args = shlex.split(command)
        except ValueError as error:
            raise SyncError("Invalid command") from error
        if len(args) != 2 or args[0] != "git-upload-pack":
            raise SyncError("Only repository listing and fetching are permitted")
        repo_id = args[1].removeprefix("/")
        if not valid_id(repo_id) or repo_id not in (repos := self.discover()):
            raise SyncError("Unknown repository")
        directory = self.git_dir(repos[repo_id])
        # Ignore user/system Git settings and prohibit lazy object fetches or hooks.
        env = {
            "PATH": str(Path(GIT).parent),
            "HOME": self.config["home"],
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        # packObjectsHook is read only from protected configuration; both global
        # and system configuration are disabled above.
        os.execve(
            GIT,
            [
                GIT,
                "-c",
                "uploadpack.allowAnySHA1InWant=false",
                "-c",
                "uploadpack.allowReachableSHA1InWant=false",
                "-c",
                "uploadpack.allowTipSHA1InWant=false",
                "upload-pack",
                "--strict",
                "--timeout=60",
                str(directory),
            ],
            env,
        )

    def snapshot(self, repo, backup=False):
        # Snapshot just once. Subsequent operations neither read nor update files.
        jj(repo, "status", snapshot=True)
        prefix = f"hosts/{self.host}/"
        template = 'if(!remote && present, name ++ "\\t" ++ if(conflict, "CONFLICT", normal_target.commit_id()) ++ "\\n")'
        refs = dict(
            line.split("\t")
            for line in jj(repo, "bookmark", "list", "-T", template).splitlines()
        )
        desired = {}
        workspaces = jj(
            repo,
            "workspace",
            "list",
            "-T",
            'name ++ "\\t" ++ target.commit_id() ++ "\\n"',
        )
        for line in workspaces.splitlines():
            name, commit = line.split("\t")
            desired[prefix + "work/" + name] = commit
        features = {
            name: commit for name, commit in refs.items()
            if not name.startswith("hosts/") and commit != "CONFLICT"
        }
        # Ordinary bookmarks already export as Git branches. Host-qualified copies
        # are needed only when explicitly backing up separate hosts to GitHub.
        if backup:
            desired.update({
                prefix + "bookmarks/" + name: commit
                for name, commit in features.items()
            })
        # Parked branches require ordinary bookmarks. Fetching an unreferenced
        # version never establishes another host as its retention authority.
        for name, commit in desired.items():
            if refs.get(name) != commit:
                jj(repo, "bookmark", "set", name, "-r", commit, "--allow-backwards")
        for name in refs.keys() - desired.keys():
            if name.startswith(prefix):
                jj(repo, "bookmark", "delete", name)
        jj(repo, "git", "export")

    def ensure_remote(self, repo, name, url):
        remotes = dict(
            line.split(" ", 1)
            for line in jj(repo, "git", "remote", "list").splitlines()
        )
        if name in remotes and remotes[name] != url:
            raise SyncError(f"Remote {name} already has a different URL in {repo}")
        if name not in remotes:
            jj(repo, "git", "remote", "add", name, url)
        # Also applies to interactive fetches, and never changes origin's policy.
        jj(
            repo,
            "config",
            "set",
            "--repo",
            f"remotes.{name}.auto-track-bookmarks",
            "glob:* ~ glob:*",
        )
        tracked = jj(
            repo,
            "bookmark",
            "list",
            "--remote",
            name,
            "--tracked",
            "-T",
            'name ++ "\\n"',
        )
        if tracked:
            raise SyncError(
                f"Untrack bookmarks on {name} before unattended fetching: {repo}"
            )

    def fetch(self, repo, name, url, env=None, interactive=False):
        self.ensure_remote(repo, name, url)
        # Native JJ owns rewrite inference and ancestry changes. Snapshot and
        # refresh the current workspace as part of its fetch transaction.
        jj(
            repo, "git", "fetch", "--remote", name,
            "--branch", "glob:*", "--tag", "glob:*",
            env=env, interactive=interactive, snapshot=True,
        )
        if command := self.config.get("postFetchCommand"):
            run([command, repo])

    def peer_env(self):
        env = os.environ.copy()
        env["GIT_SSH_COMMAND"] = shlex.join([SSH, "-F", self.config["sshConfig"]])
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    def sync_peer(self, peer, local, status):
        if not (self.state / "peers" / f"{peer}.pub").exists():
            status[peer] = {"error": "Not paired"}
            return
        catalog = json.loads(
            run([SSH, "-F", self.config["sshConfig"], peer, "jj-repo-sync-list"])
        )
        if not isinstance(catalog, dict) or any(not valid_id(key) for key in catalog):
            raise SyncError("Invalid peer repository catalog")
        catalog = {repo_id: entry for repo_id, entry in catalog.items()
                   if repo_id in self.repositories}
        for key in list(status):
            if key.startswith(f"{peer}/") and key.removeprefix(f"{peer}/") not in catalog:
                del status[key]
        for repo_id in sorted(catalog):
            entry = status.setdefault(f"{peer}/{repo_id}", {})
            try:
                repo = local.get(repo_id)
                if repo and repo["jj"]:
                    target = repo["path"]
                else:
                    target = self.state / "repos" / peer / repo_id
                    if not (target / ".jj").exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        run([JJ, "git", "init", "--no-colocate", target])
                url = f"ssh://{peer}/{quote(repo_id, safe='/')}"
                self.fetch(target, f"sync-{peer}", url, env=self.peer_env())
                entry.update(path=str(target), lastSuccess=now())
                entry.pop("error", None)
            except (SyncError, OSError, subprocess.TimeoutExpired) as error:
                entry["error"] = str(error)
        status[peer] = {"lastSuccess": now()}

    def cycle(self):
        self.setup()
        path = self.state / "status.json"
        status = json.loads(path.read_text()) if path.exists() else {}
        # Errors describe this pass. Preserve timestamps and cached objects while
        # a peer is offline, without replaying failures from earlier catalogs.
        for entry in status.values():
            entry.pop("error", None)
        local = self.discover()
        for key in list(status):
            if key.startswith("local/") and key.removeprefix("local/") not in local:
                del status[key]
        for repo_id, repo in local.items():
            if repo["jj"]:
                try:
                    self.snapshot(repo["path"])
                    status[f"local/{repo_id}"] = {"lastSuccess": now()}
                except (SyncError, OSError, subprocess.TimeoutExpired) as error:
                    status[f"local/{repo_id}"] = {"error": str(error)}
        for peer in self.peers:
            try:
                self.sync_peer(peer, local, status)
            except (SyncError, OSError, ValueError, subprocess.TimeoutExpired) as error:
                status.setdefault(peer, {})["error"] = str(error)
        write_atomic(path, json.dumps(status, indent=2) + "\n")
        errors = {
            key: value["error"] for key, value in status.items() if "error" in value
        }
        if errors:
            print(json.dumps(errors, indent=2), file=sys.stderr)

    def github(self):
        # Explicit command only. Use the user's normal SSH agent and touch policy.
        for repo_id, repo in self.discover().items():
            if not repo["jj"]:
                continue
            remotes = jj(repo["path"], "git", "remote", "list").splitlines()
            github = [
                line.split(" ", 1)
                for line in remotes
                if re.search(r" (git@github\.com:|ssh://git@github\.com/)", line)
            ]
            if not github:
                continue
            # One existing GitHub destination per repository; origin takes precedence.
            remote, url = next((r for r in github if r[0] == "origin"), github[0])
            print(f"Backing up {repo_id} to {remote}", flush=True)
            # The separate untracked remote imports GitHub's view without moving
            # the user's origin-tracking bookmarks. Authentication still requires touch.
            self.fetch(repo["path"], "sync-github", url, interactive=True)
            self.snapshot(repo["path"], backup=True)
            # A guarded push fails if remote work changed unexpectedly. Fetch and
            # reconcile explicitly; never turn that failure into an automatic retry.
            jj(
                repo["path"],
                "git",
                "push",
                "--remote",
                remote,
                "--bookmark",
                f"glob:hosts/{self.host}/*",
                "--tag",
                "glob:*",
                "--allow-empty-description",
                interactive=True,
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/jj-repo-sync.json")
    parser.add_argument(
        "command",
        choices=[
            "cycle",
            "serve",
            "setup",
            "public-key",
            "pair",
            "pair-receive",
            "revoke",
            "status",
            "github",
        ],
    )
    parser.add_argument("peer", nargs="?")
    args = parser.parse_args()
    try:
        sync = Sync(json.loads(Path(args.config).read_text()))
        if args.command == "serve":
            sync.serve(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
            return
        with sync.lock():
            if args.command == "cycle":
                sync.cycle()
            elif args.command == "setup":
                sync.setup()
            elif args.command == "public-key":
                print(sync.public_key())
            elif args.command == "pair":
                sync.pair(args.peer)
            elif args.command == "pair-receive":
                sync.accept_key(args.peer, sys.stdin.read(4096).strip())
                print(sync.public_key())
            elif args.command == "revoke":
                if args.peer not in sync.peers:
                    raise SyncError("Unknown peer")
                (sync.state / "peers" / f"{args.peer}.pub").unlink(missing_ok=True)
                sync.authorized_keys()
            elif args.command == "github":
                sync.github()
            else:
                path = sync.state / "status.json"
                print(
                    path.read_text() if path.exists() else "No sync cycle has run yet."
                )
    except (SyncError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"jj-repo-sync: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
