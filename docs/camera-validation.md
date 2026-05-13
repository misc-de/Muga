# Camera Validation Checklist

Use this on real FuriOS / Halium hardware before publishing a camera-facing
release. Desktop CI can cover source logic, but not the Android camera HAL,
sysfs torch permissions, sensor orientation, or Phosh lifecycle behaviour.

## FuriOS / Halium

- Open Camera from the main gallery and confirm preview appears within 3 s.
- Switch rear/front cameras twice; preview must resume after each switch.
- Photo mode: enable flash, capture one photo, confirm the LED fires only for
  the capture and is off afterwards.
- Video mode: enable light, confirm the LED turns on immediately in preview.
- Start video with light enabled; LED must remain on for the whole recording.
- Stop video; with the light toggle still enabled, LED should remain on in
  video preview. Turn the toggle off and confirm LED turns off.
- Start recording, then close the Camera window; LED must turn off and no
  camera process should hold the HAL afterwards.
- Rotate the phone through portrait, landscape-left, upside-down, and
  landscape-right; shutter, toolbar, focus pulse, and record dot should stay
  thumb-reachable and upright.
- Enable geotagging, capture a photo, and verify GPS EXIF is written when
  GeoClue provides a fix.
- Record at every quality preset and verify files play in Yaga and an external
  player.

## Desktop / V4L2 / PipeWire

- Open Camera with an internal webcam and with an external USB webcam.
- Capture photo at default resolution and one user-selected resolution.
- Switch to video mode and record at least 10 s.
- Confirm the saved `.mkv` opens in Yaga and an external player.
- Unplug a USB camera while Yaga is closed, reopen, and confirm the app does
  not show stale devices.

## Diagnostics To Capture On Failure

Run Yaga from a terminal with:

```bash
YAGA_CAMERA_DEBUG=1 python3 -m yaga
```

Save the terminal output, the device model, FuriOS/Distro version, and the
exact action that failed. For torch failures also check whether one of these
paths exists and is writable by the app:

```bash
ls -l /sys/class/leds/*torch* /sys/class/leds/*flash* 2>/dev/null
```

