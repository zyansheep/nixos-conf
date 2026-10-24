# secrets/

Holds secrets that must never be committed to GitHub. Files here are synced
between machines by syncthing (syncthing copies gitignored files too).

## root-password-hash

A single-line crypt(3) hash (no trailing newline) used as root's
`initialHashedPassword` in `flake.nix`. If this file is missing, the flake
falls back to a locked root account (`"!"`).

### Create / rotate the hash file

```console
$ mkpasswd -m yescrypt | tr -d '\n' > ~/nixos-conf/secrets/root-password-hash
$ chmod 600 ~/nixos-conf/secrets/root-password-hash
```

(`mkpasswd` comes from the `whois` package; `openssl passwd -6` also works —
keep the `-m yescrypt` / `-6` prefix intact, the whole `$y$...` / `$6$...`
string is the hash.)

Save the plaintext password in vaultwarden while you have it — the file only
contains the hash.

### Apply to an existing machine

`initialHashedPassword` only applies when the root account is first created
(e.g. at install). On an existing system, set root's password to the exact
hash in the file:

```console
$ printf 'root:%s\n' "$(cat ~/nixos-conf/secrets/root-password-hash)" | sudo chpasswd -e
```

or interactively with `sudo passwd root` (then re-generate the file from the
new password so fresh installs match).

Then rebuild so the flake-side settings apply:

```console
$ sudo nixos-rebuild switch --flake ~/nixos-conf#functor
```

### History note

The previous root hash was committed to the public GitHub repo (removed
2026-10-24 but still in git history). Treat every root password used before
this rotation as burned, and purge the history with `git filter-repo` if the
repo will ever be reused by others.
