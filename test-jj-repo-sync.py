"""Real JJ repositories exercise transport, rewrites, parked work and immutability."""


import json
from pathlib import Path
import sys

import pytest

import jj_repo_sync as s


@pytest.fixture
def config(tmp_path, monkeypatch):
    jj_config = tmp_path / "jj.toml"
    jj_config.write_text(
        'user.name = "Sync test"\nuser.email = "test@example.org"\n'
        '[revset-aliases]\n"immutable_heads()" = '
        '"tags() | remote_tags(remote=glob:sync-*)"\n'
    )
    monkeypatch.setenv("JJ_CONFIG", str(jj_config))
    root = tmp_path / "dev"
    root.mkdir()
    return {
        "host": "thastrom",
        "home": str(tmp_path),
        "state": str(tmp_path / "state"),
        "repositoryRoots": {"dev": str(root)},
        "repositories": ["dev/project", "dev/extra", "dev/jj", "dev/git"],
        "peers": {"remover": {"sshAlias": "remover", "address": "localhost"}},
        "sshConfig": str(tmp_path / "ssh.conf"),
        "remoteCommand": "/etc/profiles/per-user/kjekac/bin/jj-repo-sync",
    }


def repo(path, colocate=False):
    s.run([s.JJ, "git", "init", "--colocate" if colocate else "--no-colocate", path])
    (path / "file").write_text("initial\n")
    s.jj(path, "describe", "-m", "Initial work", snapshot=True)
    return path


def commit(path, revision="@"):
    return s.jj(path, "log", "-r", revision, "--no-graph", "-T", "commit_id")


def test_post_fetch_command_observes_imported_refs_and_reports_failure(config, tmp_path):
    source = repo(tmp_path / "source")
    target = repo(tmp_path / "target")
    marker = tmp_path / "observed"
    hook = tmp_path / "after-fetch"
    hook.write_text(
        f"#!{sys.executable}\nimport subprocess, sys\nfrom pathlib import Path\n"
        f"value = subprocess.check_output([{s.JJ!r}, '-R', sys.argv[1], 'log', "
        "'-r', 'hosts/thastrom/work/default@sync-remover', '--no-graph', '-T', 'commit_id'], text=True)\n"
        f"Path({str(marker)!r}).write_text(value)\n"
    )
    hook.chmod(0o755)
    sync = s.Sync(dict(config, postFetchCommand=str(hook)))
    sync.snapshot(source)
    sync.fetch(target, "sync-remover", s.jj(source, "git", "root"))
    assert marker.read_text().strip() == commit(source)
    hook.write_text(f"#!{sys.executable}\nraise SystemExit('archive failed')\n")
    with pytest.raises(s.SyncError, match="archive failed"):
        sync.fetch(target, "sync-remover", s.jj(source, "git", "root"))


@pytest.mark.parametrize("colocate", [False, True])
def test_snapshots_keep_work_and_parked_heads(config, colocate):
    sync = s.Sync(config)
    path = repo(Path(config["repositoryRoots"]["dev"]) / "project", colocate)
    s.jj(path, "bookmark", "set", "feature")
    first = commit(path)
    s.jj(path, "new", "-m", "Parked work", snapshot=True)
    (path / "parked").write_text("parked")
    s.jj(path, "status", snapshot=True)
    parked = commit(path)
    s.jj(path, "bookmark", "set", "parked")
    s.jj(path, "new", first, "-m", "Active work", snapshot=True)
    (path / "file").write_text("unfinished edit\n")
    sync.snapshot(path)
    assert commit(path, "hosts/thastrom/work/default") == commit(path)
    assert commit(path, "feature") == first
    assert not s.jj(path, "bookmark", "list", "glob:hosts/thastrom/bookmarks/*")
    refs = s.jj(path, "bookmark", "list", "-T", "normal_target.commit_id() ++ '\n'")
    assert parked in refs
    operation = s.jj(path, "op", "log", "--limit", "1", "--no-graph", "-T", "id")
    sync.snapshot(path)
    assert (
        s.jj(path, "op", "log", "--limit", "1", "--no-graph", "-T", "id") == operation
    )
    assert (path / "file").read_text() == "unfinished edit\n"


def test_native_fetch_rebases_local_work_and_tags_freeze(config, tmp_path):
    sync = s.Sync(config)
    source = repo(Path(config["repositoryRoots"]["dev"]) / "source")
    target = repo(tmp_path / "target")
    sync.snapshot(source)
    old = commit(source)
    url = str(sync.git_dir({"path": source, "jj": True}))
    sync.fetch(target, "sync-remover", url)
    s.jj(target, "new", old, "-m", "Local child", snapshot=True)
    (target / "local").write_text("keep this")
    s.jj(target, "status", snapshot=True)
    child = commit(target)
    (source / "file").write_text("rewritten source")
    sync.snapshot(source)
    new = commit(source)
    assert new != old
    sync.fetch(target, "sync-remover", url)
    assert commit(target) != child
    assert commit(target, "@-") == new
    assert commit(target, "hosts/thastrom/work/default@sync-remover") == new
    assert s.jj(target, "log", "-r", new, "--no-graph", "-T", "immutable") == "false"
    s.jj(source, "tag", "set", "nixos/Thastrom/123", "-r", new)
    s.jj(source, "git", "export")
    sync.fetch(target, "sync-remover", url)
    assert s.jj(target, "log", "-r", new, "--no-graph", "-T", "immutable") == "true"
    assert (target / "local").read_text() == "keep this"


def test_rewrite_moves_active_workspace_and_survives_offline_rewrites(config, tmp_path):
    sync = s.Sync(config)
    source = repo(tmp_path / "offline-source")
    target = repo(tmp_path / "offline-target")
    sync.snapshot(source)
    old = commit(source)
    url = s.jj(source, "git", "root")
    sync.fetch(target, "sync-remover", url)
    s.jj(target, "edit", old, snapshot=True)
    s.jj(target, "bookmark", "set", "local-name")
    for index in range(3):
        (source / "file").write_text(f"replacement {index}")
        sync.snapshot(source)
    replacement = commit(source)
    sync.fetch(target, "sync-remover", url)
    assert commit(target) == replacement
    assert commit(target, "local-name") == replacement
    assert (target / "file").read_text() == "replacement 2"
    sync.snapshot(target)
    assert not (sync.state / "history-v2").exists()


def test_independent_local_rewrite_is_preserved(config, tmp_path):
    sync = s.Sync(config)
    source = repo(tmp_path / "independent-source")
    target = repo(tmp_path / "independent-target")
    sync.snapshot(source)
    old = commit(source)
    url = s.jj(source, "git", "root")
    sync.fetch(target, "sync-remover", url)
    s.jj(target, "edit", old, snapshot=True)
    (target / "file").write_text("local independent edit")
    s.jj(target, "status", snapshot=True)
    local = commit(target)
    (source / "file").write_text("peer independent edit")
    sync.snapshot(source)
    sync.fetch(target, "sync-remover", url)
    assert commit(target) == local
    assert (target / "file").read_text() == "local independent edit"
    sync.snapshot(target)
    assert not (sync.state / "history-v2").exists()


def test_native_abandonment_rebases_child_through_repeated_fetches(config, tmp_path):
    sync = s.Sync(config)
    source = repo(tmp_path / "source")
    target = repo(tmp_path / "target")
    sync.snapshot(source)
    old = commit(source)
    url = s.jj(source, "git", "root")
    sync.fetch(target, "sync-remover", url)
    s.jj(target, "new", old, snapshot=True)
    (target / "local").write_text("reader work")
    s.jj(source, "new", "root()", snapshot=True)
    s.jj(source, "abandon", old)
    sync.snapshot(source)
    for _ in range(3):
        sync.fetch(target, "sync-remover", url)
        assert commit(target, "@-") == commit(target, "root()")
        assert (target / "local").read_text() == "reader work"
        assert not (target / "file").exists()


@pytest.mark.parametrize("mode", ["serial", "parallel", "onto"])
def test_native_split_fetch_keeps_all_published_parts_available(config, tmp_path, mode):
    sync = s.Sync(config)
    source = repo(tmp_path / "source")
    target = repo(tmp_path / "target")
    (source / "other").write_text("second part")
    sync.snapshot(source)
    old = commit(source)
    url = s.jj(source, "git", "root")
    sync.fetch(target, "sync-remover", url)
    s.jj(target, "new", old, snapshot=True)
    (target / "local").write_text("reader work")
    extra = {"serial": [], "parallel": ["--parallel"], "onto": ["--onto", "root()"]}[mode]
    s.jj(source, "split", "-r", old, "-m", "First part", *extra, "root:file", snapshot=True)
    parts = s.jj(source, "log", "--no-graph", "-r", "heads(all())", "-T", 'commit_id ++ "\\n"').splitlines()
    for i, part in enumerate(parts):
        s.jj(source, "bookmark", "set", f"part-{i}", "-r", part)
    sync.snapshot(source)
    for _ in range(3):
        sync.fetch(target, "sync-remover", url)
        for i, part in enumerate(parts):
            assert commit(target, f"part-{i}@sync-remover") == part
        assert (target / "local").read_text() == "reader work"
        # JJ chooses ancestry. This helper guarantees publication and fetching,
        # not that the local child inherits both split components.


def test_stale_peer_reference_does_not_veto_a_known_rewrite(config, tmp_path):
    writer = s.Sync(config)
    reader = s.Sync(dict(config, host="remover", state=str(tmp_path / "reader-state")))
    source = repo(tmp_path / "writer")
    target = repo(tmp_path / "reader")
    writer.snapshot(source)
    old = commit(source)
    source_url = s.jj(source, "git", "root")
    reader.fetch(target, "sync-thastrom", source_url)
    s.jj(target, "edit", old, snapshot=True)
    reader.snapshot(target)
    writer.fetch(source, "sync-remover", s.jj(target, "git", "root"))
    (source / "file").write_text("writer replacement")
    writer.snapshot(source)
    replacement = commit(source)
    # The receiver's old workspace marker keeps the predecessor visible on the
    # writer. It must not prevent the writer from announcing its own rewrite.
    assert s.jj(source, "log", "-r", f"{old} & all()", "--no-graph", "-T", "commit_id") == old
    reader.fetch(target, "sync-thastrom", source_url)
    assert commit(target) == replacement
    assert not s.jj(target, "log", "-r", "divergent()", "--no-graph", "-T", "commit_id")


def test_allowlist_defaults_to_empty_and_leaves_unlisted_repo_untouched(config):
    path = repo(Path(config["repositoryRoots"]["dev"]) / "project")
    original = commit(path)
    (path / "file").write_text("unrecorded work")
    restricted = dict(config, peers={})
    restricted.pop("repositories")
    sync = s.Sync(restricted)
    assert sync.discover() == {}
    sync.cycle()
    assert commit(path) == original
    with pytest.raises(s.SyncError, match="Unknown repository"):
        sync.serve("git-upload-pack /dev/project")


def test_allowlist_restricts_discovery_and_serving(config, monkeypatch):
    root = Path(config["repositoryRoots"]["dev"])
    allowed = repo(root / "project")
    blocked = repo(root / "sewo")
    before = commit(blocked)
    (blocked / "file").write_text("private unrecorded edits")
    visited = []
    original_walk = s.os.walk
    def walking(*args, **kwargs):
        for entry in original_walk(*args, **kwargs):
            visited.append(entry[0])
            yield entry
    monkeypatch.setattr(s.os, "walk", walking)
    sync = s.Sync(dict(config, repositories=["dev/project"], peers={}))
    assert set(sync.discover()) == {"dev/project"}
    sync.cycle()
    assert set(sync.catalog()) == {"dev/project"}
    assert commit(blocked) == before
    assert (blocked / "file").read_text() == "private unrecorded edits"
    with pytest.raises(s.SyncError, match="Unknown repository"):
        sync.serve("git-upload-pack /dev/sewo")
    assert str(blocked) not in visited


def test_allowlist_restricts_incoming_peer_catalog(config, tmp_path, monkeypatch):
    path = repo(Path(config["repositoryRoots"]["dev"]) / "project")
    sync = s.Sync(dict(config, repositories=["dev/project"]))
    s.write_atomic(sync.state / "peers/remover.pub", "paired")
    original = s.run
    def listing(args, **kwargs):
        if args[-1] == "jj-repo-sync-list":
            return json.dumps({"dev/project": {"jj": True}, "dev/sewo": {"jj": True}})
        return original(args, **kwargs)
    monkeypatch.setattr(s, "run", listing)
    calls = []
    monkeypatch.setattr(sync, "fetch", lambda target, *args, **kwargs: calls.append(str(target)))
    status = {"remover/dev/sewo": {"error": "obsolete"}}
    sync.sync_peer("remover", sync.discover(), status)
    assert calls == [str(path)]
    assert "remover/dev/sewo" not in status
    assert not (sync.state / "repos/remover/dev/sewo").exists()


def test_old_unrestricted_configuration_is_rejected(config):
    legacy = dict(config, roots=config["repositoryRoots"])
    with pytest.raises(s.SyncError, match="Legacy unrestricted roots config"):
        s.Sync(legacy)


def test_refuses_tracked_peer_refs_or_remote_replacement(config, tmp_path):
    sync = s.Sync(config)
    source = repo(tmp_path / "source")
    target = repo(tmp_path / "target")
    sync.snapshot(source)
    url = s.jj(source, "git", "root")
    sync.fetch(target, "sync-remover", url)
    s.jj(target, "bookmark", "track", "hosts/thastrom/work/default@sync-remover")
    with pytest.raises(s.SyncError, match="Untrack"):
        sync.fetch(target, "sync-remover", url)
    with pytest.raises(s.SyncError, match="different URL"):
        sync.ensure_remote(target, "sync-remover", str(tmp_path / "unrelated"))


def test_local_feature_snapshot_ignores_differing_tracked_remote(config, tmp_path):
    sync = s.Sync(config)
    source = repo(tmp_path / "source")
    target = repo(tmp_path / "target")
    s.jj(source, "bookmark", "set", "feature")
    s.jj(source, "git", "export")
    sync.fetch(target, "sync-remover", s.jj(source, "git", "root"))
    s.jj(target, "bookmark", "track", "feature@sync-remover")
    s.jj(target, "bookmark", "set", "feature", "-r", "@", "--allow-backwards")
    sync.snapshot(target)
    assert commit(target, "feature") == commit(target)
    assert not s.jj(target, "bookmark", "list", "glob:hosts/thastrom/bookmarks/*")


def test_round_trips_do_not_duplicate_features_or_relay_peer_refs(config, tmp_path):
    sync = s.Sync(config)
    source = repo(tmp_path / "source")
    target = repo(tmp_path / "target")
    remote = s.Sync(dict(config, host="remover"))
    s.jj(source, "bookmark", "set", "feature")
    remote.snapshot(source)
    sync.fetch(target, "sync-remover", s.jj(source, "git", "root"))
    s.jj(
        target, "bookmark", "set",
        "hosts/thastrom/peers/sync-remover/hosts/remover/work/default",
        "-r", "hosts/remover/work/default@sync-remover",
    )
    for _ in range(4):
        sync.snapshot(target)
        remote.fetch(source, "sync-thastrom", s.jj(target, "git", "root"))
        remote.snapshot(source)
        sync.fetch(target, "sync-remover", s.jj(source, "git", "root"))
    assert commit(target, "feature@sync-remover") == commit(source, "feature")
    local = s.jj(target, "bookmark", "list", "-T", 'if(!remote, name ++ "\\n")')
    assert "hosts/remover/work/default" not in local.splitlines()
    assert "/peers/" not in local
    assert "/bookmarks/" not in local


def test_rewritten_peer_heads_do_not_accumulate_during_handoffs(config, tmp_path):
    sync = s.Sync(config)
    source = repo(tmp_path / "source")
    target = repo(tmp_path / "target")
    remote = s.Sync(dict(config, host="remover"))
    remote.snapshot(source)
    sync.fetch(target, "sync-remover", s.jj(source, "git", "root"))
    s.jj(target, "edit", "hosts/remover/work/default@sync-remover", snapshot=True)
    sync.snapshot(target)
    local_refs = 'if(!remote, name ++ "\\n")'
    original_target_refs = s.jj(target, "bookmark", "list", "-T", local_refs)
    for index in range(5):
        (target / "file").write_text(f"local edit {index}\n")
        sync.snapshot(target)
        remote.fetch(source, "sync-thastrom", s.jj(target, "git", "root"))
        s.jj(source, "edit", "hosts/thastrom/work/default@sync-thastrom", snapshot=True)
        remote.snapshot(source)
        sync.fetch(target, "sync-remover", s.jj(source, "git", "root"))
    local = s.jj(source, "bookmark", "list", "-T", local_refs)
    assert local.splitlines() == ["hosts/remover/work/default"]
    # The target's unrelated initial branch remains parked; handoffs add no refs.
    assert s.jj(target, "bookmark", "list", "-T", local_refs) == original_target_refs


def test_discovery_opt_out_git_and_symlink_escape(config, tmp_path):
    sync = s.Sync(config)
    root = Path(config["repositoryRoots"]["dev"])
    repo(root / "jj")
    plain = repo(root / "git", colocate=True)
    (plain / ".jj").rename(tmp_path / "removed-jj")
    ignored = repo(root / "ignored")
    (ignored / ".jj-sync-ignore").touch()
    outside = repo(tmp_path / "outside")
    (root / "escape").symlink_to(outside)
    assert {key: value["jj"] for key, value in sync.catalog().items()} == {
        "dev/git": False, "dev/jj": True,
    }
    (plain / ".git").rename(tmp_path / "outside-git")
    (plain / ".git").symlink_to(tmp_path / "outside-git")
    with pytest.raises(s.SyncError, match="outside"):
        sync.git_dir({"path": plain, "jj": False})


@pytest.mark.parametrize(
    "command",
    [
        "",
        "bash",
        "git-receive-pack '/dev/project'",
        "jj-repo-sync-list; id",
        "git-upload-pack '/dev/../secret'",
        "git-upload-pack /etc",
        "git-upload-pack '/dev/project'; id",
        "git-upload-pack --help /dev/project",
        "git-upload-pack '/dev/project' extra",
        "git-upload-pack '",
    ],
)
def test_server_rejects_non_read_commands(config, command):
    with pytest.raises(s.SyncError):
        s.Sync(config).serve(command)


def test_pairing_validates_keys_and_refuses_silent_replacement(config, tmp_path):
    sync = s.Sync(config)
    key = sync.public_key()
    sync.accept_key("remover", key)
    assert (sync.state / "authorized_keys").read_text() == f"restrict {key} remover\n"
    sync.accept_key("remover", key)
    other = s.Sync(dict(config, state=str(tmp_path / "other"))).public_key()
    with pytest.raises(s.SyncError, match="Key changed"):
        sync.accept_key("remover", other)
    for invalid in [key + "\n" + key, 'command="sh" ' + key, "ssh-ed25519 bad", ""]:
        with pytest.raises(s.SyncError, match="Expected"):
            sync.accept_key("remover", invalid)
    with pytest.raises(s.SyncError, match="Unknown"):
        sync.accept_key("../../bad", key)


def test_cycle_fetches_over_protocol_and_caches_missing_repos(
    config, tmp_path, monkeypatch
):
    remote_root = tmp_path / "remote-dev"
    remote_root.mkdir()
    source = repo(remote_root / "project")
    local_root = Path(config["repositoryRoots"]["dev"])
    target = repo(local_root / "project")
    extra = repo(remote_root / "extra")
    remote_config = dict(
        config,
        host="remover",
        repositoryRoots={"dev": str(remote_root)},
        state=str(tmp_path / "remote-state"),
    )
    remote = s.Sync(remote_config)
    remote.snapshot(source)
    remote.snapshot(extra)
    # A repo-local pack hook must not execute through the read-only endpoint.
    git_config = remote.git_dir({"path": source, "jj": True}) / "config"
    with git_config.open("a") as file:
        file.write("\n[uploadpack]\npackObjectsHook = /must-not-execute\n")
    remote_file = tmp_path / "remote.json"
    remote_file.write_text(json.dumps(remote_config))
    ssh = tmp_path / "fake-ssh"
    ssh.write_text(
        f"#!{sys.executable}\nimport os, sys\n"
        "os.environ['SSH_ORIGINAL_COMMAND'] = sys.argv[-1]\n"
        f"os.execv({sys.executable!r}, [{sys.executable!r}, {s.__file__!r}, "
        f"'--config', {str(remote_file)!r}, 'serve'])\n"
    )
    ssh.chmod(0o755)
    monkeypatch.setattr(s, "SSH", str(ssh))
    monkeypatch.setenv("GIT_SSH_VARIANT", "ssh")
    sync = s.Sync(config)
    sync.accept_key("remover", remote.public_key())
    before = commit(target)
    s.write_atomic(sync.state / "status.json", json.dumps({
        "remover/dev/junk": {"error": "Unknown repository"},
        "local/dev/gone": {"error": "Missing checkout"},
    }))
    sync.cycle()
    status = json.loads((sync.state / "status.json").read_text())
    assert not [entry for entry in status.values() if "error" in entry], status
    assert "remover/dev/junk" not in status
    assert "local/dev/gone" not in status
    assert commit(target) == before
    assert commit(target, "hosts/remover/work/default@sync-remover") == commit(source)
    cached = sync.state / "repos/remover/dev/extra"
    assert commit(cached, "hosts/remover/work/default@sync-remover") == commit(extra)
    assert not (local_root / "extra").exists()
    extra_revision = commit(extra)
    extra.rename(tmp_path / "removed-extra")
    sync.cycle()
    status = json.loads((sync.state / "status.json").read_text())
    assert "remover/dev/extra" not in status
    assert cached.exists()
    # An offline peer preserves its last successful timestamp and fetched objects.
    monkeypatch.setattr(s, "SSH", "/missing-ssh")
    sync.cycle()
    after = json.loads((sync.state / "status.json").read_text())
    assert after["remover"]["lastSuccess"] == status["remover"]["lastSuccess"]
    assert "error" in after["remover"]
    assert commit(cached, "hosts/remover/work/default@sync-remover") == extra_revision


@pytest.mark.parametrize("deployed", [False, True])
@pytest.mark.parametrize("dependent", [False, True])
def test_two_host_rebase_cycles_preserve_independent_work(
    config, tmp_path, monkeypatch, deployed, dependent
):
    """Reproduce a timezone rebase while the other host edits Alacritty."""
    writer_root = Path(config["repositoryRoots"]["dev"])
    reader_root = tmp_path / "reader-dev"
    reader_root.mkdir()
    settings = {
        "thastrom": dict(config),
        "remover": dict(
            config, host="remover",
            repositoryRoots={"dev": str(reader_root)},
            state=str(tmp_path / "reader-state"),
            peers={"thastrom": {"address": "localhost", "sshAlias": "thastrom"}},
        ),
    }
    config_files = {}
    for host, value in settings.items():
        file = tmp_path / f"{host}.json"
        file.write_text(json.dumps(value))
        config_files[host] = str(file)
    ssh = tmp_path / "fake-ssh-both-hosts"
    ssh.write_text(
        f"#!{sys.executable}\nimport os, sys\n"
        f"configs = {config_files!r}\n"
        "os.environ['SSH_ORIGINAL_COMMAND'] = sys.argv[-1]\n"
        f"os.execv({sys.executable!r}, [{sys.executable!r}, {s.__file__!r}, "
        "'--config', configs[sys.argv[-2]], 'serve'])\n"
    )
    ssh.chmod(0o755)
    monkeypatch.setattr(s, "SSH", str(ssh))
    monkeypatch.setenv("GIT_SSH_VARIANT", "ssh")
    writer, reader = (s.Sync(settings[name]) for name in ("thastrom", "remover"))
    writer.accept_key("remover", reader.public_key())
    reader.accept_key("thastrom", writer.public_key())
    source = repo(writer_root / "project")
    target = repo(reader_root / "project")
    base = commit(source)
    s.jj(source, "new", base, "-m", "Timezone feature", snapshot=True)
    (source / "timezone").write_text("correct timezone\n")
    s.jj(source, "bookmark", "set", "timezone", snapshot=True)
    old = commit(source)

    def cycle(sync):
        sync.cycle()
        status = json.loads((sync.state / "status.json").read_text())
        assert not [entry for entry in status.values() if "error" in entry], status

    cycle(writer)
    cycle(reader)
    s.jj(target, "new", old if dependent else base,
         "-m", "Alacritty feature", snapshot=True)
    (target / "alacritty").write_text("local network access\n")
    s.jj(target, "bookmark", "set", "alacritty", snapshot=True)
    feature = commit(target)
    feature_change = s.jj(target, "log", "-r", "@", "--no-graph", "-T", "change_id")
    cycle(reader)
    cycle(writer)
    if deployed:
        # Deployment happens while the writer is offline: it has not yet
        # fetched the tag that would prohibit its own rebase.
        s.jj(target, "tag", "set", "nixos/Thastrom/123", "-r", old)

    s.jj(source, "new", base, "-m", "Containment", snapshot=True)
    (source / "containment").write_text("allowlist\n")
    s.jj(source, "bookmark", "set", "containment", snapshot=True)
    containment = commit(source)
    s.jj(source, "rebase", "-r", old, "-d", containment)
    replacement = commit(source, "timezone")
    assert replacement != old
    # The writer returns to its integration; the feature is exported by name.
    for iteration in range(3):
        cycle(writer)
        cycle(reader)
        moved = dependent
        assert (commit(target) != feature) == moved
        assert s.jj(target, "log", "-r", "@", "--no-graph", "-T", "change_id") == feature_change
        if dependent:
            assert commit(target, "@-") == (replacement if moved else old)
            assert (target / "timezone").read_text() == "correct timezone\n"
        assert (target / "alacritty").read_text() == "local network access\n"
        assert commit(source, "timezone") == replacement
        assert commit(target, "timezone@sync-thastrom") == replacement
        if deployed:
            assert commit(target, "nixos/Thastrom/123") == old
            assert s.jj(target, "log", "-r", old, "--no-graph", "-T", "immutable") == "true"
        # A deployment tag pins the exact commit, while a mutable child may
        # follow a replacement. JJ owns visibility and divergence decisions.



def test_github_backup_uses_guarded_host_refs_and_user_auth(
    config, tmp_path, monkeypatch
):
    sync = s.Sync(config)
    path = repo(Path(config["repositoryRoots"]["dev"]) / "project")
    s.jj(path, "git", "remote", "add", "origin", "git@github.com:owner/project.git")
    calls = []
    original = s.jj

    def record(repo, *args, **kwargs):
        if args[:2] == ("git", "fetch"):
            return ""
        if args[:2] == ("git", "push"):
            calls.append((args, kwargs))
            return ""
        return original(repo, *args, **kwargs)

    monkeypatch.setattr(s, "jj", record)
    sync.github()
    args, kwargs = calls.pop()
    assert "glob:hosts/thastrom/*" in args
    assert "--tag" in args
    assert "--all" not in args
    assert "env" not in kwargs
    assert not calls


def test_github_round_trip_and_unexpected_remote_update(config, tmp_path, monkeypatch):
    sync = s.Sync(config)
    source = repo(Path(config["repositoryRoots"]["dev"]) / "project")
    destination = repo(tmp_path / "github")
    url = s.jj(destination, "git", "root")
    s.jj(source, "git", "remote", "add", "origin", url)
    s.jj(source, "bookmark", "set", "feature")
    accepted = commit(source)
    s.jj(source, "tag", "set", "nixos/Thastrom/7")
    s.jj(source, "new", "-m", "Unfinished next version", snapshot=True)
    original = s.run
    github_url = "git@github.com:owner/project.git"

    # Only substitute the network address. All fetches and guarded pushes run
    # through the real JJ/Git transport against a disposable Git repository.
    def local_transport(args, **kwargs):
        result = original([url if arg == github_url else arg for arg in args], **kwargs)
        if args[-3:] == ["git", "remote", "list"]:
            return result.replace(url, github_url)
        return result

    monkeypatch.setattr(s, "run", local_transport)
    sync.github()
    sync.snapshot(source)
    assert not s.jj(
        source, "bookmark", "list", "glob:hosts/thastrom/bookmarks/*", "-T",
        'if(!remote && present, name ++ "\\n")',
    )
    s.jj(destination, "git", "import")
    assert commit(destination, "hosts/thastrom/work/default") == commit(source)
    assert commit(destination, "nixos/Thastrom/7") == accepted
    (source / "file").write_text("newer unfinished work")
    sync.github()
    s.jj(destination, "git", "import")
    assert commit(destination, "hosts/thastrom/work/default") == commit(source)
    # A foreign edit to our namespace is observed but never silently overwritten.
    s.jj(
        destination,
        "bookmark",
        "set",
        "hosts/thastrom/work/default",
        "-r",
        "@",
        "--allow-backwards",
    )
    s.jj(destination, "git", "export")
    foreign = commit(destination)
    (source / "file").write_text("another local version to back up")
    with pytest.raises(s.SyncError):
        sync.github()
    assert commit(destination, "hosts/thastrom/work/default") == foreign

