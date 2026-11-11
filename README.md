# jj-repo-sync

A small helper for publishing unfinished Jujutsu work and fetching it between
allowlisted development repositories. JJ owns reconciliation: the helper carries
no rewrite planner, evolution ledger, or independent ancestry policy.

A scheduled `jj-repo-sync cycle` snapshots local working copies, publishes
`hosts/<host>/work/<workspace>` bookmarks, exports refs, and fetches peers through
read-only SSH. Ordinary bookmarks publish parked branches. Give work a bookmark
before leaving it if it should remain advertised. Unbookmarked work stays in the
originating JJ repository but is not a cross-host backup.

Use JJ 0.47.1 or newer. Native fetch can rebase mutable descendants and update
working files. Independent edits can become divergent changes; reconcile those
with JJ before continuing the same change on another machine. Split changes can
require manual ancestry repair: a reader's child may follow only one split part,
even though all published parts are fetched. A disappearing ref can be interpreted
as abandonment. See [CONFIG.md](CONFIG.md) for the relevant JJ settings.

The helper preserves exact repository allowlists, restricted pairing keys, cached
peer repositories, status reporting, and explicit guarded GitHub backups. The
empty allowlist permits no repositories. Missing local repositories are fetched
into `~/.local/state/jj-repo-sync/repos/<peer>/<root>/<path>`; offline peers keep
the last fetched objects. `.jj-sync-ignore` excludes a directory subtree.

The dedicated SSH endpoint permits repository listing and `git-upload-pack` only.
It uses a separate software key with no shell, forwarding, or PTY access. Pair via
normal authenticated SSH using `jj-repo-sync pair <peer>`; revoke with
`jj-repo-sync revoke <peer>`. Regular SSH and GitHub authentication keep their
existing policy. Provision the endpoint and scheduler in your system configuration.

`jj-repo-sync github` backs up host workspace and feature bookmarks and local tags
to an existing GitHub remote, using normal user authentication and JJ's guarded
pushes. Fetch and reconcile unexpected remote changes before retrying.

Deployment tagging, archive retention, and immutability rules belong to the system
configuration. An optional `postFetchCommand` receives the repository path after
each successful fetch. In nix-config this runs `archive-deployments`, which retains
exact `nixos/` and `darwin/` deployment commits in local archive tags.

The catalog remains compatible with older peers: it advertises repository IDs and
`jj` flags; extra fields from old peers are ignored. Pairing keys, caches, and status
state remain usable. Old `history-v2/` and `heads/` files are unused. Older hosts
continue using their own reconciliation policy until upgraded. Existing archive
tags are ordinary JJ tags and remain intact. JJ operation logs remain local.

Run `nix flake check` to exercise real disposable repositories, including repeated
peer cycles, offline rewrites, independent edits, deployment tags, the restricted
endpoint, and guarded backup. `nix develop` provides JJ and the test dependencies.
