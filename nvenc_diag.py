#!/usr/bin/env python3
"""
NVENC diagnostic — figures out which h264_nvenc settings your GPU accepts.

Run it and paste the whole output back:

    python nvenc_diag.py

It generates a tiny test clip, then tries the Kash Cropper Split tab's exact
NVENC arguments plus several variations, reporting PASS/FAIL for each and the
full ffmpeg error for the ones that fail. This pinpoints the bad argument so the
app can be fixed without trial-and-error.
"""

import os
import subprocess
import sys
import tempfile

# The exact NVENC video args Kash Cropper uses for "H.264 NVENC (GPU, fastest)"
APP_NVENC = ["-c:v", "h264_nvenc", "-preset", "p1", "-rc", "constqp", "-qp", "18",
             "-pix_fmt", "yuv420p", "-profile:v", "main", "-movflags", "+faststart"]

# Each variation = (label, video-arg-list). We change one thing at a time so the
# PASS/FAIL pattern reveals which single argument the GPU is unhappy with.
VARIATIONS = [
    ("1. app baseline (exact app args)",        APP_NVENC),
    ("2. bare minimum (h264_nvenc only)",       ["-c:v", "h264_nvenc"]),
    ("3. minimum + yuv420p",                    ["-c:v", "h264_nvenc", "-pix_fmt", "yuv420p"]),
    ("4. baseline minus -profile:v main",       ["-c:v", "h264_nvenc", "-preset", "p1",
                                                 "-rc", "constqp", "-qp", "18",
                                                 "-pix_fmt", "yuv420p", "-movflags", "+faststart"]),
    ("5. baseline + -bf 0 (no B-frames)",       APP_NVENC + ["-bf", "0"]),
    ("6. preset p4 instead of p1",              ["-c:v", "h264_nvenc", "-preset", "p4",
                                                 "-rc", "constqp", "-qp", "18",
                                                 "-pix_fmt", "yuv420p", "-profile:v", "main",
                                                 "-movflags", "+faststart"]),
    ("7. VBR/-cq instead of constqp/-qp",       ["-c:v", "h264_nvenc", "-preset", "p1",
                                                 "-rc", "vbr", "-cq", "19", "-b:v", "0",
                                                 "-pix_fmt", "yuv420p", "-profile:v", "main",
                                                 "-movflags", "+faststart"]),
    ("8. old-style preset 'fast'",              ["-c:v", "h264_nvenc", "-preset", "fast",
                                                 "-pix_fmt", "yuv420p"]),
]


def _run(args):
    """Return (ok, combined_output)."""
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-y"] + args,
                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return proc.returncode == 0, proc.stderr.decode(errors="replace")


def main() -> None:
    print("=" * 60)
    print("Kash Cropper NVENC diagnostic")
    print("=" * 60)

    # ffmpeg version line
    try:
        v = subprocess.run(["ffmpeg", "-hide_banner", "-version"],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        print(v.stdout.decode(errors="replace").splitlines()[0])
    except FileNotFoundError:
        print("ffmpeg not found on PATH.")
        sys.exit(1)

    # Does the build list the NVENC encoders at all?
    enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
    print("h264_nvenc listed in build:", b"h264_nvenc" in enc)
    print()

    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "src.mp4")
    # 640x480 so the cropped half (320) is a comfortable, even NVENC size.
    ok, out = _run(["-f", "lavfi", "-i", "testsrc=size=640x480:rate=30:duration=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", src])
    if not ok:
        print("Could not create a test clip:\n", out)
        sys.exit(1)

    first_fail_output = None
    results = []
    for label, vargs in VARIATIONS:
        dst = os.path.join(tmp, "out.mp4")
        # Mirror the app: split the left half, then NVENC-encode it.
        ok, out = _run(["-i", src, "-vf", "crop=trunc(iw/4)*2:trunc(ih/2)*2:0:0"]
                       + vargs + ["-an", dst])
        results.append((label, ok))
        if not ok and first_fail_output is None:
            first_fail_output = (label, out)

    print("RESULTS")
    print("-" * 60)
    for label, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}]  {label}")
    print("-" * 60)

    any_pass = any(ok for _, ok in results)
    if not any_pass:
        print("\nNo NVENC variation succeeded — NVENC likely isn't usable on this\n"
              "machine (driver/GPU). The app's CPU formats will still work.")
    print()

    if first_fail_output:
        label, out = first_fail_output
        print(f"FULL ffmpeg OUTPUT for first failure ({label}):")
        print("=" * 60)
        print(out.strip())
        print("=" * 60)
    else:
        print("Every variation PASSED — NVENC works with the app's settings here.")

    # Clean up
    for f in os.listdir(tmp):
        try:
            os.remove(os.path.join(tmp, f))
        except OSError:
            pass
    try:
        os.rmdir(tmp)
    except OSError:
        pass


if __name__ == "__main__":
    main()
