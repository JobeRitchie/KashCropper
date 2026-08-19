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

4. Double-click `start.bat` inside the cloned folder. It installs the Python
   dependencies and launches the app.

Cloning (rather than downloading a ZIP) is what lets the **Updates** tab pull new
versions for you.

### ffmpeg

ffmpeg is optional but strongly recommended — without it the app falls back to a
limited OpenCV encoder. Install it and put it on your PATH; if you have an NVIDIA
GPU, NVENC hardware encoding is detected and offered automatically.

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
