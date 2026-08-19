# Kash Cropper

A Tkinter toolkit for cropping, clipping, splitting and batch-processing behavioral video.

*Created by Jobe Ritchie, PhD — Kash Lab, University of North Carolina at Chapel Hill.*

## Install

1. Install [Python 3.8+](https://www.python.org/downloads/) (check **Add Python to PATH**).
2. Install [Git](https://git-scm.com/downloads) — required for the in-app updater.
3. Open a command prompt in a new folder and run:

   ```
   git clone https://github.com/JobeRitchie/KashCropper.git
   ```

4. Install **ffmpeg** — see [Installing ffmpeg](#installing-ffmpeg) below. Strongly
   recommended; the app runs without it but with a much weaker encoder.
5. Double-click `start.bat` inside the cloned folder. It installs the Python
   dependencies and launches the app.

Cloning (rather than downloading a ZIP) is what lets the **Updates** tab pull new
versions for you.

## Installing ffmpeg

Kash Cropper uses **ffmpeg** for every export and **ffprobe** (shipped in the same
download) to inspect audio streams. Without them the app still runs, but falls back
to a limited OpenCV encoder: no audio, fewer output formats, and no GPU encoding.
Installing ffmpeg is strongly recommended.

Both programs must be on your **PATH** — the list of folders Windows searches for
commands — so that typing `ffmpeg` in any terminal works.

### Option A — winget (easiest, Windows 10/11)

Open PowerShell and run:

```
winget install Gyan.FFmpeg
```

winget downloads ffmpeg and puts it on your PATH for you. **Close and reopen your
terminal afterwards** — a running terminal keeps the old PATH.

### Option B — manual install

1. Go to <https://www.gyan.dev/ffmpeg/builds/> and download **ffmpeg-release-essentials.zip**.
2. Unzip it and move the extracted folder to `C:fmpeg`. You should end up with
   `C:fmpeginfmpeg.exe` and `C:fmpeginfprobe.exe`.
3. Add `C:fmpegin` to your PATH:
   - Press <kbd>Win</kbd> and type *environment variables* → open
     **Edit the system environment variables**.
   - Click **Environment Variables…**
   - Under **User variables**, select **Path** → **Edit…** → **New**.
   - Paste `C:fmpegin` and click **OK** on all three windows.
4. Close and reopen your terminal.

### Verify it worked

In a **new** terminal:

```
ffmpeg -version
ffprobe -version
```

Each should print a version banner. `'ffmpeg' is not recognized` means the PATH
entry is wrong or the terminal predates the change — reopen it and check that
`C:fmpegin` (the `bin` folder, not the folder above it) is what you added.

Then restart Kash Cropper and open the **Welcome** tab. It reports the back-end it
detected:

| Welcome tab says | Meaning |
| --- | --- |
| `ffmpeg detected • NVENC GPU encoding available` | Best case — GPU encoding options are offered in every export dropdown. |
| `ffmpeg detected • CPU encoding (NVENC not available)` | ffmpeg is working; encoding runs on the CPU. |
| `ffmpeg not found — using the limited OpenCV fallback` | ffmpeg still isn't on the PATH. |

Detection happens once at start-up, so **restart the app** after installing ffmpeg
rather than just switching tabs.

### GPU (NVENC) encoding

If you have an NVIDIA GPU, Kash Cropper detects its NVENC hardware encoder and adds
GPU output formats automatically — there is nothing extra to install beyond a
current NVIDIA driver. If the Welcome tab reports NVENC as unavailable but you
believe your card supports it, run:

```
python nvenc_diag.py
```

It tries the app's exact NVENC settings plus several variations and prints
PASS/FAIL with the full ffmpeg error for each, which pinpoints the problem.

## Tabs

| Tab | What it does |
| --- | --- |
| **Welcome** | App overview, detected back-end, and update status. |
| **Crop Regions** | Draw multiple rectangles on one video; export each region as its own file. |
| **Batch Crop** | Draw one crop per video across a batch; outputs keep the original name in a `cropped` folder. |
| **Clip / Trim** | Scrub frame-by-frame, set in/out points, export trimmed clips. Includes a Cut & Stitch editor for removing ranges. |
| **Brightness** | Measure and equalise luminance across many videos. |
| **Stretch** | Force every video to one exact resolution. |
| **Convert** | Batch re-encode legacy formats (e.g. MPG) to MP4. |
| **Split** | Halve each video down the middle; output names are parsed from the source filename and editable. |
| **Updates** | Check GitHub for a newer version and install it in place. |

## Updating

Open the **Updates** tab and click **Check for Updates**. If the app is behind,
**Download & Install Update** pulls the new version and restarts. Local edits that
would block the pull are set aside in a recoverable `git stash`.
