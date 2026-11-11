# Configuration

Provision the CLI configuration at `/etc/jj-repo-sync.json`, or pass `--config`:

```json
{
  "host": "desktop",
  "home": "/home/alice",
  "state": "/home/alice/.local/state/jj-repo-sync",
  "repositoryRoots": {"dev": "/home/alice/dev"},
  "repositories": ["dev/nix-config"],
  "peers": {"laptop": {"address": "192.0.2.2", "sshAlias": "laptop"}},
  "sshConfig": "/etc/jj-repo-sync-ssh.conf",
  "remoteCommand": "/run/current-system/sw/bin/jj-repo-sync"
}
```

Root names and relative paths match between hosts. `repositories` is an exact
allowlist; an absent or empty list permits no repositories. Discovery, serving,
incoming catalogs, and GitHub backup respect it. Hidden directories, symlinks,
`node_modules`, `target`, and `vendor` are skipped. The legacy unrestricted `roots`
configuration is rejected.

## Native JJ behavior

Configure JJ on each host, including the user running scheduled cycles:

```toml
[git]
abandon-unreachable-commits = true
record-synthetic-predecessors = true
write-change-id-header = true

[revset-aliases]
"immutable_heads()" = "tags() | remote_tags(remote=glob:sync-*) | present(published@origin)"
```

These settings enable native rewrite inference, abandonment, and stable change IDs
through Git transport. The helper uses JJ's configuration without overriding these
settings. Its package and interactive JJ should use the same version (at least
0.47.1). The nix-config module selects the helper's JJ package for Home Manager.

Peer bookmarks are untracked, preventing automatic name adoption. The helper sets
`remotes.sync-<peer>.auto-track-bookmarks = "glob:* ~ glob:*"` in each repository
and refuses already tracked bookmarks on that remote. This does not disable
native rewrite inference. Ordinary named branches and workspace markers are
fetched. Markers advertise a workspace's recorded commit; other workspaces retain
JJ's normal stale-workspace handling.

Fetch snapshots and refreshes the current workspace. Native reconciliation can
change ancestry and working files, and can introduce normal JJ rebase conflicts.
Use one editing host per active change. Bookmark parked work to keep it published;
removing its last published ref may cause readers to abandon it and rebase children
onto its parents. Republishing it makes it available again but need not restore
those children's previous ancestry. After splits, inspect local descendants and
rebase them explicitly if they need both parts. Independent versions remain
available as divergence rather than being automatically merged.

The old `reconcile` field is unused. There is no helper-specific reconciliation
mode or history protocol. To choose different behavior, configure JJ itself.
Disabling `git.abandon-unreachable-commits` also disables native rewrite
application in JJ 0.47.1.

## Deployment retention

An optional `postFetchCommand` is an absolute executable path. The helper invokes
it directly, without a shell, passing the repository path as its only argument.
It runs after successful peer and GitHub fetches; failure is reported as a cycle
error. It is trusted local configuration and is never read from peer catalogs.

The nix-config script archives observed deployment tags as
`archive/<tag>/<commit-id>`, including both local and remote `nixos/` and `darwin/`
tags. These ordinary local tags preserve exact commits when upstream tags move or
are deleted. The immutable-heads rule above protects their ancestry. Mutable
children of a deployment can still follow rewritten versions of their parent.
Deployment creation and archive naming policy are outside this helper.

## Restricted transport

Provision a dedicated SSH daemon that forces `jj-repo-sync serve`, uses the state's
`authorized_keys`, and prohibits shells, forwarding, PTYs, and SSH startup scripts.
The handler accepts only `jj-repo-sync-list` and validated `git-upload-pack` requests
for allowlisted repositories. Pin host keys and use the state's `identity` software
key for this endpoint. Regular SSH and GitHub credentials stay separate.

The endpoint lists repositories; it does not snapshot them. Schedule publication
on each host. Repositories absent locally are fetched into the state cache.
Status reports the latest pass while retaining offline success timestamps.
