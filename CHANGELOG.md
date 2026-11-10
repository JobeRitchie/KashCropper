# Changelog

## 1.2.0

- **Speed tab** — slow down or speed up one or many videos to a target length
  and/or a target frame rate (Hz). Length only keeps every frame and changes the
  rate to fit. Hz only resamples in real time by default (same length, frames
  dropped/duplicated), or re-stamps every frame at the new rate if "Keep
  original length" is unticked. Both together stretches to the length and
  resamples to the rate.
  A per-file plan shows the resulting frames, Hz, length and speed before you
  run. Audio is retimed with pitch preserved. Output goes to a `retimed` folder.
- Narrower tab padding so all tabs fit the default window.

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
