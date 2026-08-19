# Changelog

## 1.1.0

- **Updates tab** — checks the GitHub remote for new commits and installs them
  in place, then restarts. A quiet check runs at start-up and badges the tab
  title when an update is waiting; local edits that would block the pull are set
  aside in a recoverable `git stash`.
- **Splash screen** at start-up crediting Jobe Ritchie, PhD, University of North
  Carolina at Chapel Hill. It sizes itself from its content, so it stays intact
  under Windows display scaling.
- Project published to GitHub with a README and `.gitignore`.

## 1.0.0

- Initial release: Crop Regions, Batch Crop, Clip/Trim with Cut & Stitch,
  Brightness, Stretch, Convert and Split tabs, with ffmpeg/NVENC encoding and an
  OpenCV fallback.
