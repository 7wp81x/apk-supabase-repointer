# supabase-apk-patcher

A small tool for a very specific, narrow situation: you own/administer a
Flutter (often FlutterFlow-generated) Android app whose Supabase project
URL and anon key are hardcoded into the compiled release binary
(`libapp.so`), the **original Supabase project is gone** (paused,
deleted, or otherwise unrecoverable), you have **no access to the
original build project** to change the setting and re-publish normally,
and you have **legitimate authorization** (as the owner/administrator,
or on the client's behalf) to keep the existing installed app working
against a new backend.

This repo exists to document that migration and keep the tooling
around in case it's needed again — e.g. for sibling apps built from the
same original project (an admin app, a field-worker app, etc. sharing
one backend).

**This is a last-resort path.** If you can get access to the original
FlutterFlow/build project, or the original signing keystore, prefer
that — see "Why this is a last resort" below.

## What it actually does

1. Decompiles the APK with `apktool` (unpacks manifest, resources, and
   copies `lib/<abi>/libapp.so` — the Dart AOT-compiled app logic —
   untouched, as an opaque binary).
2. Scans `libapp.so` for `https://*.supabase.co` URLs and JWT-looking
   strings (`eyJ...`), so you can sanity-check what's actually embedded
   before changing anything.
3. Performs a **same-byte-length** find/replace of the old URL and old
   anon key with new ones, across every ABI's `libapp.so`
   (`arm64-v8a`, `armeabi-v7a`, `x86_64`, `x86` — whichever exist).
4. Rebuilds the APK with `apktool b`.
5. Aligns it with `zipalign`.
6. Signs it with `apksigner`, using a keystore you provide (or a fresh
   one it can generate for you).

## Why same-length only, and why this is fragile

Dart's AOT compiler bakes string literals into the compiled snapshot
with explicit length metadata, not as null-terminated C strings. If you
swap in a string of a *different* byte length, you don't get a clean
truncation or a harmless extra byte — you corrupt the snapshot's
internal object layout, which typically manifests as a crash on launch
or, worse, silent misbehavior. There is no reliable way to "just pad it
with nulls" and have that be safe.

This is why the tool **refuses to patch anything unless the old and new
strings are exactly the same length**, and why that's usually
achievable for Supabase specifically:

- Supabase project URLs are `https://<20-char-ref>.supabase.co` — the
  ref is always 20 characters, so old and new project URLs are always
  the same total length.
- Supabase anon keys are JWTs with the same claim shape
  (`iss`, `ref`, `role`, `iat`, `exp`) signed with HS256, which produces
  a fixed-length signature. In practice old/new anon keys usually end
  up the same length too — but **check this every time**, don't assume
  it. A digit-count change in the `iat`/`exp` Unix timestamps (e.g.
  crossing from a 9-digit to 10-digit epoch value) will throw this off.

If lengths don't match, the tool stops and refuses to patch that value.
Do not try to force it by hand-editing around the length check.

## Why this is a last resort

- **Signing.** If you sign the patched APK with a different key than
  whatever signed the copy currently installed on users' devices,
  Android will not let it install as an *update* — users must uninstall
  the existing app first (losing any local-only app data — cached
  auth sessions, local DB, prefs — anything not stored server-side).
  If you don't have the original keystore, this is unavoidable.
- **Fragility.** Every future backend change (rotating the anon key,
  moving projects again) means repeating this whole process on a
  compiled binary instead of just editing a config value.
- **No source of truth.** You're editing a build artifact, not source.
  If you ever do get FlutterFlow project access back, that becomes the
  real fix and this patched APK should be considered a bridge, not the
  permanent solution.

Prefer, in order:
1. Getting access to the original FlutterFlow/build project and
   republishing normally.
2. Getting the *original signing keystore* even without full project
   access, so future patches can install as normal updates.
3. This binary patch, as done here.

## Scope and intent

This tool only performs a same-length string substitution of values you
supply — it doesn't discover, brute-force, or extract anything from an
app you don't already control. It's meant for owners/administrators (or
someone acting on their explicit behalf) repointing an app they're
responsible for to a backend they control, in situations like:

- The original backend project is gone and there's no way to update the
  published config through normal channels.
- The original build tooling/project access is unavailable.

It is **not** intended for, and should not be used for, modifying an
app you don't have the rights to modify, redirecting someone else's
app to infrastructure they didn't authorize, or bypassing licensing/DRM
protections. Standard software licensing and computer-use laws in your
jurisdiction still apply to how you use this.

## Requirements

Install and ensure these are on your `PATH`:

- [`apktool`](https://apktool.org/) — decompile/rebuild
- Android SDK **build-tools** (`zipalign`, `apksigner`) — usually under
  `~/Android/Sdk/build-tools/<version>/`
- JDK (`keytool`), only needed if generating a brand new keystore
- Python 3.8+

Quick check:
```bash
apktool --version
zipalign 2>&1 | head -n1
apksigner --version
keytool -help >/dev/null && echo "keytool OK"
```

## Usage

### 1. Scan an APK first (no changes made)

```bash
python3 patch_apk.py --apk app-release.apk --scan-only
```

This decompiles (if not already done) and prints every candidate
Supabase URL and JWT it can find in each ABI's `libapp.so`, along with
the JWT's `role` claim (`anon` / `service_role` / etc). Use this to:

- Confirm the app actually has the old URL/key you expect.
- Make sure a `service_role` key is **not** present (it never should be
  in a client app — if you see one, that's a separate security issue
  to raise with whoever owns the project, independent of this
  migration).
- Copy the exact old URL/key strings to use in step 2 if auto-detection
  picks up more than one candidate.

### 2. Patch, rebuild, and sign in one go

```bash
python3 patch_apk.py \
  --apk app-release.apk \
  --old-url https://oldprojectref00000.supabase.co \
  --new-url https://newprojectref00000.supabase.co \
  --old-anon-key "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...." \
  --new-anon-key "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...." \
  --keystore /path/to/your-release.keystore \
  --ks-alias your-key-alias \
  --ks-storepass "$KEYSTORE_STOREPASS" \
  --ks-keypass "$KEYSTORE_KEYPASS"
```

Or run it with fewer flags and answer prompts interactively (recommended,
so passwords aren't left in your shell history):

```bash
python3 patch_apk.py --apk app-release.apk
```

It will auto-detect the old URL/key if there's exactly one unambiguous
candidate in the binary, and prompt you for anything it can't resolve
on its own (new URL, new key, keystore alias/passwords).

If you have multiple sibling apps sharing the same backend (e.g. an
admin app and a field-worker app built from the same FlutterFlow
project), repeat per-APK — the old/new URL and key are typically the
same across all of them since they share one Supabase project; only
the `--apk` argument changes.

**Passwords:** avoid putting keystore passwords directly on the command
line where possible (they end up in shell history and process lists).
Prefer the interactive prompts, or export them as environment variables
in a shell that isn't logged/history-tracked, e.g.:

```bash
read -rs KEYSTORE_STOREPASS
export KEYSTORE_STOREPASS
```

### 3. Verifying your keystore alias/password before running

If you're not sure of the alias or password on an existing keystore:

```bash
keytool -list -v -keystore /path/to/your-release.keystore
```

It'll prompt for the store password and then list every alias inside.
To check one specific alias non-interactively:

```bash
keytool -list -v \
  -keystore /path/to/your-release.keystore \
  -alias your-key-alias \
  -storepass "$KEYSTORE_STOREPASS" \
  -keypass "$KEYSTORE_KEYPASS"
```

If this fails with `keystore password was incorrect` or
`Alias <name> does not exist`, fix that before running the patcher —
it'll fail at the signing step with the same underlying error otherwise.

### 4. Only rebuild/align, sign separately later

```bash
python3 patch_apk.py --apk app-release.apk ... --skip-sign
```

Produces `app-release-patched-aligned.apk`, unsigned. Useful if
signing happens on a different machine that holds the keystore.

## Output files

For an input `app-release.apk`, you'll get, alongside it:

| File | What it is |
|---|---|
| `app-release-decompiled/` | apktool's decompiled output (kept around so you can re-run without re-decompiling; delete or pass `--force` to redo) |
| `app-release-patched-unsigned.apk` | rebuilt from decompiled sources, no zipalign/signature |
| `app-release-patched-aligned.apk` | zipaligned, still unsigned |
| `app-release-patched-signed.apk` | final installable artifact |

## Errors you might hit, and what they mean

**`Required tool 'apktool' not found on PATH`**
(or `zipalign`/`apksigner`) — install it / add Android SDK
`build-tools/<version>/` to your `PATH`.

**`No libapp.so found under .../lib/<abi>/`**
Either this isn't a Flutter app, it's a debug/profile build without an
AOT snapshot, or apktool's output layout is unexpected for this APK.
Run `find <decompiled_dir> -iname "*.so"` manually to check.

**`No supabase.co URLs found in any libapp.so` (scan warning)**
The config may be loaded from `assets/flutter_assets/` instead of
compiled into Dart, may be built at runtime from separate string
fragments (harder to patch reliably), or the app may simply not use
Supabase in the way expected. Check
`grep -r supabase <decompiled_dir>/assets/` before assuming this tool
can't help.

**`No JWT-looking strings found` (scan warning)**
Anon key may be assembled/obfuscated rather than a single literal, or
this specific APK variant doesn't embed one directly (e.g. it's fetched
from a remote config endpoint at first launch — check for that
possibility, since it would actually make this whole problem easier to
solve without any binary patching).

**`URL length mismatch: old=N chars, new=M chars` — patch refused**
The new project's URL is a different length from the old one. Since
Supabase refs are always 20 characters this is unusual — double check
you copied the *base* URL correctly (no trailing path, no typo). If
you deliberately want a custom domain instead of the raw
`*.supabase.co` URL, it must be exactly N characters including
`https://` and no trailing slash.

**`Anon key length mismatch: old=N chars, new=M chars` — patch refused**
This can genuinely happen if `iat`/`exp` epoch values differ in digit
count between the two keys. Options if you hit this:
- Regenerate the new project's JWT secret/anon key isn't controllable
  in length via the dashboard, so this isn't something you can just
  "fix" from Supabase's side.
- Fall back to a small server-side proxy in front of the new project
  that swaps the `apikey`/`Authorization` header server-side, so the
  binary keeps its original (still valid at the network layer, since
  your proxy accepts it) anon key untouched. Ask for help designing
  this if you hit this case — it's a different approach from binary
  patching entirely.

**`replaced some but N occurrence(s) still remain — investigate manually!`**
This means the replace ran but somehow the byte count of the "old"
value after replacement isn't zero, which shouldn't be possible with a
straight `bytes.replace()` unless the old value overlaps itself in a
weird way. Treat this as a hard stop — do not proceed to rebuild/sign.
Re-run `--scan-only` and inspect the file by hand
(`strings lib/<abi>/libapp.so | grep supabase`).

**`Signing failed` / `keystore password was incorrect` / `Alias ... does not exist`**
Verify alias and passwords with the `keytool -list -v` command in step
3 above before re-running.

**App installs but crashes immediately on launch after patching**
This is the "length check passed but something is still wrong" case.
Possible causes:
- A string was found and replaced in a context that wasn't actually a
  standalone literal (e.g. part of a larger constant table with
  cross-references) — rare, but possible in AOT snapshots.
- Something else entirely unrelated to this patch broke during
  decompile/rebuild (apktool rebuild issues are usually resource-table
  related, not `.so`-related, but check `apktool b` output for
  warnings).
- Get a logcat: `adb logcat | grep -i flutter` right after launch, and
  look for a snapshot/heap corruption message versus a normal
  Dart exception (a normal Dart exception, e.g. an actual network
  error from hitting the new backend for a genuinely different
  reason, is a completely different, much less scary problem).

**App launches fine but Realtime subscriptions never fire**
Not a patcher bug — this means the new Supabase project doesn't have
the relevant tables added to the `supabase_realtime` publication, or
your data sync into the new project isn't triggering WAL-level change
events the way the original schema did. Check
Database → Replication in the new project's dashboard.

## Security notes

- Never patch in a `service_role` key. This tool is meant only for
  `anon` (public) keys — if `--old-anon-key`/`--new-anon-key` point at
  a `service_role` JWT, decode it first
  (`echo '<payload_b64>' | base64 -d`) and confirm `"role":"anon"`
  before proceeding. A `service_role` key found embedded in a client
  app is a serious, unrelated security issue that should be rotated
  and reported to whoever owns that project regardless of this tool.
- **Never commit real secrets to this repo.** That means no real
  keystore files, no keystore passwords, no anon/service keys, and no
  actual client APKs containing them. The `.gitignore` in this repo
  excludes `*.apk`, `*.keystore`/`*.jks`, and generated build
  directories by default — don't remove those entries.
- If a secret is ever committed accidentally, rotating it (regenerating
  the Supabase anon key, re-keying the keystore) is necessary — removing
  it from a later commit does not remove it from git history.
- Treat any keystore as a long-lived credential: store it outside the
  repo entirely (e.g. a password manager or secrets vault) and pass its
  path via `--keystore` at run time rather than checking it in anywhere.
