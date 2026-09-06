#!/usr/bin/env python3
"""
supabase-apk-patcher
---------------------
Patches a hardcoded Supabase project URL + anon key inside a compiled
Flutter/FlutterFlow release APK (Dart AOT `libapp.so`), then rebuilds,
aligns, and signs the APK.

Context: legacy backend migration where the original Supabase project
is gone (paused/deleted) and there's no access to the original
FlutterFlow project to rebuild through normal channels. This performs
an in-place, same-length string swap in the AOT snapshot to repoint the
app at a new Supabase project, for an already-authorized migration of
an app you/your client own.

Requirements on PATH:
  - apktool
  - zipalign   (Android build-tools)
  - apksigner  (Android build-tools)
  - keytool    (JDK, only if generating a new keystore)
  - python3

WHY SAME-LENGTH ONLY:
  Dart AOT snapshots store strings with explicit length metadata baked
  into the snapshot, not null-terminated C strings. Replacing a string
  with a different byte length WILL corrupt the snapshot and crash the
  app (or corrupt unrelated data) even though a plain `strings`/grep
  pass on the raw file might look fine. This tool refuses to patch
  unless old/new strings are exactly the same byte length.

USAGE:
  python3 patch_apk.py \\
      --apk Admin-release.apk \\
      --old-url https://oldprojectref00000.supabase.co \\
      --new-url https://newprojectref00000.supabase.co \\
      --old-anon-key eyJhbGciOi... \\
      --new-anon-key eyJhbGciOi... \\
      --keystore /path/to/your-release.keystore \\
      --ks-alias your-key-alias \\
      --ks-storepass "$KEYSTORE_STOREPASS" \\
      --ks-keypass "$KEYSTORE_KEYPASS"

  Or run interactively (it will prompt for anything missing):
  python3 patch_apk.py --apk Admin-release.apk

  To scan an APK first, without patching anything:
  python3 patch_apk.py --apk Admin-release.apk --scan-only

See README.md for full walkthrough, troubleshooting, and safety notes.
"""

import argparse
import getpass
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

# ---------------------------------------------------------------------------
# Small utils
# ---------------------------------------------------------------------------

class Ansi:
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    END = "\033[0m"


def info(msg):
    print(f"{Ansi.CYAN}[i]{Ansi.END} {msg}")


def ok(msg):
    print(f"{Ansi.GREEN}[OK]{Ansi.END} {msg}")


def warn(msg):
    print(f"{Ansi.YELLOW}[!]{Ansi.END} {msg}")


def fail(msg):
    print(f"{Ansi.RED}[ERROR]{Ansi.END} {msg}")


def die(msg, code=1):
    fail(msg)
    sys.exit(code)


def run(cmd, **kwargs):
    """Run a subprocess command, streaming output, raise on failure."""
    info("Running: " + " ".join(str(c) for c in cmd))
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(str(c) for c in cmd)}")
    return result


def check_tool(name, hint):
    if shutil.which(name) is None:
        die(
            f"Required tool '{name}' not found on PATH.\n"
            f"       {hint}"
        )


# ---------------------------------------------------------------------------
# Core steps
# ---------------------------------------------------------------------------

SO_ABIS = ["arm64-v8a", "armeabi-v7a", "x86_64", "x86"]


def decompile(apk_path: Path, out_dir: Path, force: bool):
    if out_dir.exists():
        if force:
            warn(f"Removing existing decompiled dir: {out_dir}")
            shutil.rmtree(out_dir)
        else:
            die(
                f"Decompiled output dir already exists: {out_dir}\n"
                f"       Use --force to overwrite, or delete it manually first."
            )
    run(["apktool", "d", str(apk_path), "-o", str(out_dir)])
    ok(f"Decompiled to {out_dir}")


def find_libapp_files(decompiled_dir: Path):
    found = []
    for abi in SO_ABIS:
        p = decompiled_dir / "lib" / abi / "libapp.so"
        if p.exists():
            found.append(p)
    if not found:
        die(
            f"No libapp.so found under {decompiled_dir}/lib/<abi>/.\n"
            f"       This usually means:\n"
            f"         - The APK isn't a Flutter app, or\n"
            f"         - It's a debug/profile build without AOT snapshot, or\n"
            f"         - apktool decompiled into an unexpected layout.\n"
            f"       Run with --scan-only to inspect the extracted APK manually."
        )
    return found


def scan_libapp(lib_path: Path):
    """Return set of found supabase URLs and any JWT-looking strings."""
    data = lib_path.read_bytes()
    results = {"urls": set(), "jwts": set()}

    # crude "strings" extraction: runs of printable ascii >= 6 chars
    import re
    printable_run = re.compile(rb"[\x20-\x7e]{6,}")
    for m in printable_run.finditer(data):
        s = m.group()
        if b"supabase.co" in s and s.startswith(b"http"):
            # trim to just the base url in case trailing garbage is attached
            try:
                text = s.decode("ascii")
            except UnicodeDecodeError:
                continue
            # keep only up to '.supabase.co' + nothing else risky
            idx = text.find(".supabase.co")
            if idx != -1:
                base = text[: idx + len(".supabase.co")]
                results["urls"].add(base)
        if s.startswith(b"eyJ") and b"." in s:
            try:
                results["jwts"].add(s.decode("ascii"))
            except UnicodeDecodeError:
                pass
    return results


def scan_only_mode(decompiled_dir: Path, show_full: bool = False):
    libs = find_libapp_files(decompiled_dir)
    info(f"Found {len(libs)} libapp.so file(s): {[str(l) for l in libs]}")
    any_url, any_jwt = set(), set()
    for lib in libs:
        r = scan_libapp(lib)
        any_url |= r["urls"]
        any_jwt |= r["jwts"]

    if not any_url:
        warn(
            "No supabase.co URLs found in any libapp.so.\n"
            "     Possible causes: obfuscated/split strings, non-Supabase backend,\n"
            "     config loaded from assets/remote config instead of compiled in.\n"
            "     Try: grep -r supabase " + str(decompiled_dir / "assets") + " (if it exists)"
        )
    else:
        ok(f"Found {len(any_url)} candidate Supabase URL(s):")
        for u in sorted(any_url):
            print(f"    {u}")

    if not any_jwt:
        warn(
            "No JWT-looking strings (starting with 'eyJ') found.\n"
            "     The anon key may be assembled at runtime, obfuscated, or\n"
            "     this build may not embed one directly."
        )
    else:
        ok(f"Found {len(any_jwt)} candidate JWT(s):")
        for j in sorted(any_jwt):
            role = decode_jwt_role(j)
            if show_full:
                print(f"    role={role or '?'}  len={len(j)}")
                print(f"    {j}")
                print()
            else:
                print(f"    role={role or '?'}  {j[:60]}...{j[-20:]}  (len={len(j)})")
        if not show_full:
            info("Pass --show-full-keys to print complete, uncut key values.")

    return any_url, any_jwt


def decode_jwt_role(jwt: str):
    import base64
    import json
    try:
        parts = jwt.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1]
        padding = "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload + padding)
        obj = json.loads(decoded)
        return obj.get("role")
    except Exception:
        return None


def patch_libapp(lib_path: Path, replacements):
    """
    replacements: list of (old_bytes, new_bytes) tuples, all same length,
    already validated by caller.
    """
    data = bytearray(lib_path.read_bytes())
    report = []
    for old, new in replacements:
        count_before = data.count(old)
        if count_before == 0:
            report.append((old, new, 0, 0, "NOT_FOUND"))
            continue
        data = bytearray(bytes(data).replace(old, new))
        count_after = data.count(old)
        report.append((old, new, count_before, count_after, "OK" if count_after == 0 else "PARTIAL"))
    lib_path.write_bytes(bytes(data))
    return report


def rebuild(decompiled_dir: Path, out_apk: Path):
    if out_apk.exists():
        out_apk.unlink()
    run(["apktool", "b", str(decompiled_dir), "-o", str(out_apk)])
    ok(f"Rebuilt unsigned/unaligned APK: {out_apk}")


def zipalign(in_apk: Path, out_apk: Path):
    if out_apk.exists():
        out_apk.unlink()
    run(["zipalign", "-v", "4", str(in_apk), str(out_apk)])
    ok(f"Aligned APK: {out_apk}")


def sign(in_apk: Path, out_apk: Path, keystore: Path, alias: str, storepass: str, keypass: str):
    if out_apk.exists():
        out_apk.unlink()
    cmd = [
        "apksigner", "sign",
        "--ks", str(keystore),
        "--ks-key-alias", alias,
        "--ks-pass", f"pass:{storepass}",
        "--key-pass", f"pass:{keypass}",
        "--out", str(out_apk),
        str(in_apk),
    ]
    run(cmd)
    ok(f"Signed APK: {out_apk}")
    run(["apksigner", "verify", str(out_apk)])
    ok("Signature verified")


def maybe_generate_keystore(path: Path, alias: str, storepass: str, keypass: str):
    if path.exists():
        return
    warn(f"Keystore not found at {path}, generating a new one.")
    warn("Remember: an APK signed with a NEW keystore cannot be installed as an")
    warn("update over the currently-installed app (different signer). Users will")
    warn("need to uninstall the old app first, then install this one fresh.")
    path.parent.mkdir(parents=True, exist_ok=True)
    run([
        "keytool", "-genkey", "-v",
        "-keystore", str(path),
        "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
        "-alias", alias,
        "-storepass", storepass,
        "-keypass", keypass,
        "-dname", "CN=Migration, OU=Dev, O=Org, L=City, S=State, C=US",
    ])
    ok(f"Generated new keystore at {path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Patch a hardcoded Supabase URL/anon key inside a Flutter APK.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(__doc__),
    )
    p.add_argument("--apk", required=True, help="Path to the input .apk file")
    p.add_argument("--workdir", default=None, help="Working directory (default: alongside the apk)")
    p.add_argument("--force", action="store_true", help="Overwrite existing decompiled dir")
    p.add_argument("--scan-only", action="store_true", help="Only scan for URLs/keys, don't patch")
    p.add_argument("--show-full-keys", action="store_true",
                    help="Print complete JWT values in scan output instead of a truncated preview")

    p.add_argument("--old-url", help="Old Supabase base URL, e.g. https://xxxx.supabase.co")
    p.add_argument("--new-url", help="New Supabase base URL, e.g. https://yyyy.supabase.co")
    p.add_argument("--old-anon-key", help="Old Supabase anon (public) JWT")
    p.add_argument("--new-anon-key", help="New Supabase anon (public) JWT")

    p.add_argument("--keystore", help="Path to signing keystore (.jks/.keystore)")
    p.add_argument("--ks-alias", help="Keystore key alias")
    p.add_argument("--ks-storepass", help="Keystore store password")
    p.add_argument("--ks-keypass", help="Keystore key password (often same as storepass)")
    p.add_argument("--generate-keystore", action="store_true",
                    help="Generate a new keystore at --keystore path if it doesn't exist")

    p.add_argument("--skip-sign", action="store_true", help="Stop after rebuild+align, don't sign")
    return p.parse_args()


def main():
    args = parse_args()

    for tool, hint in [
        ("apktool", "Install: https://apktool.org/ or `brew install apktool` / distro package"),
        ("zipalign", "Part of Android SDK build-tools, add build-tools/<ver> to PATH"),
        ("apksigner", "Part of Android SDK build-tools, add build-tools/<ver> to PATH"),
    ]:
        check_tool(tool, hint)

    apk_path = Path(args.apk).expanduser().resolve()
    if not apk_path.exists():
        die(f"APK not found: {apk_path}")

    workdir = Path(args.workdir).expanduser().resolve() if args.workdir else apk_path.parent
    stem = apk_path.stem  # e.g. "Admin-release"
    decompiled_dir = workdir / f"{stem}-decompiled"

    info(f"APK        : {apk_path}")
    info(f"Working dir: {workdir}")
    info(f"Decompiled : {decompiled_dir}")

    # 1. Decompile
    if decompiled_dir.exists() and not args.force:
        warn(f"Using existing decompiled dir (pass --force to redo): {decompiled_dir}")
    else:
        decompile(apk_path, decompiled_dir, args.force)

    # 2. Scan
    found_urls, found_jwts = scan_only_mode(decompiled_dir, show_full=args.show_full_keys)

    if args.scan_only:
        info("Scan-only mode, stopping here.")
        return

    # 3. Resolve old/new values (prompt if missing)
    old_url = args.old_url
    if not old_url:
        if len(found_urls) == 1:
            old_url = next(iter(found_urls))
            info(f"Auto-detected old URL: {old_url}")
        else:
            old_url = input("Enter OLD Supabase URL to replace: ").strip()

    new_url = args.new_url or input("Enter NEW Supabase URL: ").strip()

    old_anon = args.old_anon_key
    if not old_anon:
        if len(found_jwts) == 1:
            old_anon = next(iter(found_jwts))
            info("Auto-detected old anon key from binary.")
        elif len(found_jwts) > 1:
            warn("Multiple JWT-like strings found; can't auto-pick. Pass --old-anon-key explicitly.")
            old_anon = input("Enter OLD anon key (or leave blank to skip key patch): ").strip() or None
        else:
            old_anon = input("Enter OLD anon key (or leave blank to skip key patch): ").strip() or None

    new_anon = None
    if old_anon:
        new_anon = args.new_anon_key or input("Enter NEW anon key: ").strip()

    # 4. Validate lengths BEFORE touching any files
    if not old_url or not new_url:
        die("Old/new URL is required.")

    if len(old_url) != len(new_url):
        die(
            f"URL length mismatch: old={len(old_url)} chars, new={len(new_url)} chars.\n"
            f"       Dart AOT strings can't be safely resized in place. Options:\n"
            f"         - Use a custom short domain that matches the exact old length\n"
            f"         - Fall back to a network-level proxy shim instead of binary patch\n"
            f"       Old: {old_url}\n"
            f"       New: {new_url}"
        )
    ok(f"URL length check passed ({len(old_url)} chars both).")

    replacements = [(old_url.encode(), new_url.encode())]

    if old_anon:
        if not new_anon:
            die("Old anon key given but no new anon key provided.")
        if len(old_anon) != len(new_anon):
            die(
                f"Anon key length mismatch: old={len(old_anon)} chars, new={len(new_anon)} chars.\n"
                f"       Supabase anon JWTs are usually the same length project-to-project\n"
                f"       (same claim shape, same HS256 signature length), but yours differ.\n"
                f"       This is often caused by 'iat'/'exp' epoch values crossing a digit\n"
                f"       boundary (e.g. 999999999 -> 1000000000). Cannot safely patch as-is.\n"
                f"       Consider a header-rewriting proxy in front of the new project instead.\n"
                f"       Old: {old_anon}\n"
                f"       New: {new_anon}"
            )
        ok(f"Anon key length check passed ({len(old_anon)} chars both).")
        replacements.append((old_anon.encode(), new_anon.encode()))
    else:
        warn("No anon key patch requested — only the URL will be patched.")

    # 5. Patch every libapp.so found
    libs = find_libapp_files(decompiled_dir)
    any_failures = False
    for lib in libs:
        info(f"Patching {lib} ...")
        report = patch_libapp(lib, replacements)
        for old, new, before, after, status in report:
            label = "URL " if old.startswith(b"http") else "ANON"
            if status == "NOT_FOUND":
                warn(f"  [{label}] not found in this .so (0 occurrences) — skipping this file for this value")
            elif status == "OK":
                ok(f"  [{label}] replaced {before} occurrence(s), 0 remaining")
            else:
                fail(f"  [{label}] replaced some but {after} occurrence(s) still remain — investigate manually!")
                any_failures = True

    if any_failures:
        die(
            "One or more replacements only partially succeeded. Do NOT proceed to\n"
            "       rebuild/sign until this is resolved — a partially patched binary\n"
            "       is worse than an unpatched one (inconsistent state)."
        )

    total_url_hits = sum(1 for lib in libs if old_url.encode() not in lib.read_bytes())
    if total_url_hits == 0:
        die(
            f"After patching, OLD URL still not found as removed in ANY libapp.so.\n"
            f"       This likely means the URL never existed in these files, or your\n"
            f"       --old-url value didn't exactly match what's in the binary.\n"
            f"       Re-run with --scan-only to see exactly what was detected."
        )

    # 6. Rebuild
    patched_unsigned = workdir / f"{stem}-patched-unsigned.apk"
    rebuild(decompiled_dir, patched_unsigned)

    # 7. Align
    patched_aligned = workdir / f"{stem}-patched-aligned.apk"
    zipalign(patched_unsigned, patched_aligned)

    if args.skip_sign:
        ok(f"Done (unsigned). Final aligned APK: {patched_aligned}")
        warn("Remember to sign before installing: an unsigned APK will not install.")
        return

    # 8. Sign
    keystore = Path(args.keystore).expanduser() if args.keystore else None
    if not keystore:
        die("No --keystore provided. Pass --keystore, or use --skip-sign to stop before signing.")

    alias = args.ks_alias or input("Keystore alias: ").strip()
    storepass = args.ks_storepass or getpass.getpass("Keystore store password: ")
    keypass = args.ks_keypass or getpass.getpass("Keystore key password (blank = same as store password): ") or storepass

    if args.generate_keystore:
        maybe_generate_keystore(keystore, alias, storepass, keypass)

    if not keystore.exists():
        die(
            f"Keystore not found: {keystore}\n"
            f"       Pass --generate-keystore to create a new one at this path\n"
            f"       (note: a new keystore means users must uninstall the old app\n"
            f"       first, since Android requires matching signatures for updates)."
        )

    patched_signed = workdir / f"{stem}-patched-signed.apk"
    try:
        sign(patched_aligned, patched_signed, keystore, alias, storepass, keypass)
    except RuntimeError as e:
        die(
            f"Signing failed: {e}\n"
            f"       Common causes:\n"
            f"         - Wrong alias (check with: keytool -list -v -keystore {keystore})\n"
            f"         - Wrong store/key password\n"
            f"         - Keystore file corrupted or wrong format"
        )

    ok(f"DONE. Final signed APK: {patched_signed}")
    info("Next steps:")
    info("  1. Uninstall the existing app from your test device (required if signed")
    info("     with a different key than the original).")
    info(f"  2. adb install {patched_signed}")
    info("  3. Launch and verify: app doesn't crash, login works, a REST call works,")
    info("     and (if used) a Realtime-dependent screen updates live.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        die("Interrupted by user.")
    except RuntimeError as e:
        die(str(e))
