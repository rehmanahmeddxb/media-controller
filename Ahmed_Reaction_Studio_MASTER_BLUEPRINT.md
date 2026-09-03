# Ahmed Reaction Studio — Master Blueprint
## Finalized Local-First Architecture & Implementation Plan

**Status:** FINAL MASTER BLUEPRINT  
**Targets:** Windows PC + Android/Termux  
**Deployment:** 100% local for v1  
**Primary goals:** extremely low latency, smooth playback, accurate A/V synchronization, lightweight architecture, resilient recovery, scalable multi-PiP editing.

---

## 1. Product Definition

Ahmed Reaction Studio is a local-first browser video studio for creating reaction, commentary, comparison, tutorial, and multi-source videos.

The application must support:

- One main canvas.
- Multiple independent PiP/overlay media layers.
- Local video files.
- Camera sources.
- Independent play/pause/seek controls per media layer.
- Per-layer show/hide.
- Adding/removing/reordering PiP layers.
- Draggable and resizable PiP rectangles.
- Four corner handles + four side handles.
- Presets for common PiP layouts.
- 16:9, 9:16, and 1:1 canvases.
- Android: maximum 2 physical camera sources.
- Windows PC: multiple camera sources, limited only by hardware/browser/device capabilities.
- A local high-performance preview pipeline.
- Independent timeline events.
- Local recording.
- Final high-quality FFmpeg rendering.
- Adaptive proxies for heavy media.
- Multiple export formats/resolutions.
- No cloud dependency.
- Original files must never be modified or deleted by the application.

---

# 2. Final Technology Decisions

## Frontend

Use lightweight browser-native technologies:

- HTML5
- CSS3
- TypeScript
- Canvas 2D
- HTMLVideoElement
- MediaDevices/getUserMedia
- MediaRecorder
- Web Audio API
- Pointer Events
- requestVideoFrameCallback()
- requestAnimationFrame() fallback
- File System Access API where supported
- Web Workers where useful
- OffscreenCanvas where supported and beneficial

### Do NOT use for v1

- React
- Electron
- Node.js backend
- heavy UI frameworks

The browser already provides the media primitives we need.

---

# 3. Backend

Use:

- Python 3.11+
- FastAPI
- Uvicorn
- Pydantic
- python-multipart

Python is the orchestration/control layer.

Python must NOT process every video frame itself.

---

# 4. Media Engine

## Primary media engine

**FFmpeg + FFprobe**

FFmpeg handles:

- decoding
- normalization
- proxy generation
- scaling
- padding
- cropping
- PiP compositing
- audio processing
- timestamp correction
- frame-rate conversion
- encoding
- muxing
- final export
- validation

FFprobe handles:

- codec detection
- resolution
- FPS
- duration
- audio streams
- pixel format
- HDR information
- VFR/CFR detection
- bitrate
- stream validation

## Optional future engine

**GStreamer**

GStreamer is NOT mandatory for v1.

It may be introduced later if benchmarking proves that a specific local real-time capture/processing pipeline benefits from it.

The application architecture must keep GStreamer pluggable rather than making the entire project dependent on it.

---

# 5. Local-First Rule

There is NO PythonAnywhere dependency in v1.

There is:

- no cloud processing
- no mandatory cloud storage
- no remote media upload
- no remote FFmpeg
- no remote database
- no internet requirement for normal editing

Everything remains on the user's device.

PythonAnywhere may be considered later for optional account/project synchronization, but it is explicitly outside v1.

---

# 6. Windows + Android/Termux

## Windows

Supported environment:

- Python
- FastAPI/Uvicorn
- FFmpeg
- FFprobe
- Chrome/Edge/Firefox

Startup:

`start_windows.bat`

## Android

Supported environment:

- Termux
- Python
- FastAPI/Uvicorn
- FFmpeg
- Android browser

Startup:

`start_termux.sh`

The browser handles camera permissions.

Termux does not need direct camera-hardware control for the normal workflow.

---

# 7. Core Application Architecture

```text
                    AHMED REACTION STUDIO
                             |
             +---------------+---------------+
             |                               |
        BROWSER UI                       PYTHON API
             |                               |
      +------+-------+                +------+------+
      |      |       |                |             |
    Video  Camera  Audio           Projects       Jobs
      |      |       |                |             |
      +------+-------+                |          Storage
             |                        |
          Canvas <---------------------+
             |
        MediaRecorder
             |
      Local recording
             |
          FFmpeg
             |
        Final export
```

The browser owns real-time interaction.

Python owns orchestration.

FFmpeg owns authoritative media processing.

---

# 8. Media Source Model

Every media layer is a source object.

Example:

```json
{
  "id": "layer_01",
  "type": "video",
  "source": "local",
  "name": "Main Video",
  "file": "...",
  "visible": true,
  "muted": false,
  "volume": 1.0,
  "playing": true
}
```

Camera:

```json
{
  "id": "camera_01",
  "type": "camera",
  "deviceId": "...",
  "visible": true,
  "muted": false,
  "volume": 1.0
}
```

Every layer has its own media element/state.

Never use one shared media element for multiple independently controlled layers.

---

# 9. Main Canvas

The project has one master canvas.

Supported aspect ratios:

- 16:9
- 9:16
- 1:1

Logical render sizes:

### 16:9

- 1920×1080
- scalable to other export resolutions

### 9:16

- 1080×1920

### 1:1

- 1080×1080

The displayed canvas is responsive.

The logical render coordinate system remains stable.

---

# 10. Multiple PiP / Overlay System

The previous single-PiP model is replaced by a **multi-layer compositor**.

Example:

```text
Layer 0: Main video
Layer 1: Camera 1
Layer 2: Camera 2
Layer 3: Local video A
Layer 4: Local video B
Layer 5: Local video C
...
```

The user can:

- Add PiP
- Remove PiP
- Duplicate PiP
- Rename PiP
- Reorder PiP
- Hide PiP
- Show PiP
- Lock PiP
- Mute PiP
- Change volume
- Change position
- Resize PiP
- Apply presets

---

# 11. Android Camera Limit

Android v1:

**Maximum 2 camera sources.**

This means:

- Camera 1
- Camera 2

The application must detect available camera devices.

If only one camera is available:

- show one camera source.

If two are available:

- allow both.

If the browser/Android device cannot expose two simultaneous camera streams, the UI must clearly report that limitation rather than pretending it can.

No fake multi-camera behavior.

---

# 12. Windows Camera Support

Windows:

- support multiple camera devices.
- enumerate available devices using MediaDevices.
- allow the user to assign any available camera to a layer.
- actual simultaneous camera count is limited by:
  - USB bandwidth
  - camera driver
  - browser
  - CPU/GPU
  - RAM
  - capture resolution/FPS

The UI should detect failures and degrade gracefully.

---

# 13. Add Media Workflow

Main canvas:

```text
+ Add Media
```

Options:

- Local Video
- Camera
- Image (future-ready)
- Audio (future-ready)

Adding a new local video creates a new layer.

Adding a camera creates a new camera layer.

Each layer gets its own independent controls.

---

# 14. Media Assignment

Each PiP can be assigned independently.

Example:

```text
PiP 1 → Camera 1
PiP 2 → Camera 2
PiP 3 → local_video.mp4
PiP 4 → local_clip.webm
PiP 5 → another local video
```

A source may optionally be reusable in more than one layer, but when reused it must have separate media-element state if independent playback is required.

---

# 15. Visibility Controls

Every layer has:

```text
Visible: ON/OFF
```

When hidden:

- it disappears from the main canvas.
- its timeline/media state can continue according to project policy.
- visibility changes are recorded as timeline events.

Important:

**Hide/show is different from pause/play.**

Example:

```text
Video playing + hidden
Video keeps playing but is invisible.

Video paused + visible
Video remains visible on its current frame.

Video paused + hidden
Video remains paused and invisible.
```

---

# 16. Layer Panel

The UI should contain a layer list:

```text
LAYERS

👁 Main Video
👁 Camera 1
👁 Camera 2
👁 Clip A
👁 Clip B

[ + ADD MEDIA ]
```

Each row can provide:

- visibility
- lock
- mute
- volume
- play/pause
- rename
- delete
- drag reorder

Selected layer gets editing handles on the canvas.

---

# 17. PiP Geometry

Each layer has normalized geometry:

```json
{
  "x": 0.70,
  "y": 0.70,
  "w": 0.25,
  "h": 0.25
}
```

Values are normalized 0–1.

This makes layouts resolution-independent.

---

# 18. PiP Dragging

Dragging inside a selected PiP changes:

- x
- y

The layer must remain within canvas bounds unless an optional "allow outside canvas" mode is explicitly implemented.

Pointer Events must support:

- mouse
- touch
- stylus

Use pointer capture during drag.

---

# 19. PiP Resize Handles

Every selected PiP has eight handles:

```text
●──────●──────●
│             │
│             │
●             ●
│             │
│             │
●──────●──────●
```

### Corner handles

Modify:

- width
- height
- x/y as required by the anchored opposite corner.

### Side handles

Modify only one dimension:

- left/right → width
- top/bottom → height

Optional aspect-lock:

- preserve aspect ratio.

All geometry must be clamped and validated.

---

# 20. PiP Presets

Built-in presets:

- top-left
- top-center
- top-right
- center-left
- center
- center-right
- bottom-left
- bottom-center
- bottom-right
- 50/50
- 70/30
- full-screen
- quarter-screen
- custom

Presets must work regardless of canvas aspect ratio.

---

# 21. Layer Ordering

The compositor uses z-order.

Example:

```text
Main
  ↓
Camera 1
  ↓
Camera 2
  ↓
Clip A
  ↓
Clip B
```

Higher layer = drawn later.

User can drag layers up/down.

The renderer must use the same z-order.

---

# 22. Independent Media Controls

Every layer has independent:

- play
- pause
- seek
- volume
- mute
- visibility

No global pause command should unintentionally pause all layers.

A global control may be provided separately if explicitly requested:

- Play All
- Pause All
- Reset All

But individual controls remain authoritative.

---

# 23. Timeline Event Model

Every meaningful media action becomes an event.

Example:

```json
{
  "layerId": "camera_01",
  "action": "pause",
  "wallMs": 12843,
  "mediaTime": 7.233
}
```

Possible events:

- play
- pause
- seek
- visibility_on
- visibility_off
- mute
- unmute
- volume
- source_change
- geometry_change
- layer_add
- layer_remove
- layer_reorder

The master recording clock uses `performance.now()`.

Never synchronize independent media by frame number.

---

# 24. Synchronization Architecture

```text
                MASTER CLOCK
              performance.now()
                     |
      +--------------+--------------+
      |              |              |
    Main           Camera 1       Camera 2
   timeline        timeline       timeline
      |              |              |
      +--------------+--------------+
                     |
                 Event log
                     |
              FFmpeg renderer
```

This supports:

- different FPS
- VFR
- independent pauses
- independent seeks
- camera timing
- visibility changes

---

# 25. Browser Preview Pipeline

Preview should be optimized for responsiveness.

```text
Media Source
     ↓
HTMLVideoElement
     ↓
requestVideoFrameCallback()
     ↓
Canvas compositor
     ↓
Display
```

Fallback:

```text
requestAnimationFrame()
```

when requestVideoFrameCallback is unavailable.

Do not use Python/OpenCV for live preview.

---

# 26. Audio Pipeline

Each audio-enabled layer can feed:

```text
Media Element
     ↓
MediaElementSource
     ↓
Gain Node
     ↓
Mute/Volume
     ↓
Audio Mixer
     ↓
MediaStreamDestination
```

The final recording stream combines:

```text
Canvas video track
+
Mixed audio track
```

The audio system must preserve synchronization with the master recording clock.

---

# 27. Recording

Recording starts with a configurable countdown:

- 3
- 2
- 1
- RECORD

Recording captures:

- compositor video
- mixed audio
- event timeline
- project state

Where practical, camera/source recordings should also be preserved separately so final rendering does not depend solely on the browser-composited recording.

---

# 28. Local File Handling

Browser users select files through:

- `<input type=file>`
- File System Access API where supported.

The application must not assume it can directly browse arbitrary disk paths from a normal web page.

Android file selection uses the Android browser/system picker.

---

# 29. Large File Strategy

Do not send large media to a remote server.

For local v1:

- use browser-selected local files.
- use object URLs for preview.
- retain source File references during the session.
- use local worker/backend APIs only when a local filesystem path is actually available.

For future desktop-local worker workflows, use direct filesystem access through the local Python service.

---

# 30. Adaptive Proxy System

The application must inspect media before deciding how to preview it.

Analyze:

- resolution
- codec
- pixel format
- FPS
- bitrate
- VFR
- HDR
- duration
- audio

Proxy strategy:

```text
Original
   |
   +-- device handles it --> Original preview
   |
   +-- too heavy ----------> 1080p proxy
   |
   +-- still heavy --------> 720p proxy
   |
   +-- still heavy --------> 480p proxy
```

Do not create unnecessary proxies.

Original media is never replaced.

---

# 31. Playback Health Monitor

Track:

- decoded frames
- dropped frames
- current FPS
- target FPS
- buffer state
- render time
- compositor time
- audio drift
- camera frame stability

Example:

```text
TARGET FPS: 60
ACTUAL FPS: 59.7
DROPPED: 0
AUDIO DRIFT: 1.1 ms
STATUS: EXCELLENT
```

If performance degrades:

1. reduce preview resolution.
2. reduce preview FPS if necessary.
3. switch to a lighter proxy.
4. reduce compositor workload.
5. enter safe preview mode.

Do not degrade final export quality.

---

# 32. Stutter Prevention Rules

Never:

- decode every frame in Python.
- unnecessarily re-encode originals.
- upload local media to the cloud.
- perform expensive synchronous API calls during playback.
- block the browser main thread with heavy processing.
- continuously recreate media elements.
- repeatedly create MediaElementSource nodes for the same element.

Prefer:

- hardware/browser decoding.
- requestVideoFrameCallback.
- lightweight Canvas operations.
- cached media elements.
- Web Workers where useful.
- adaptive proxies.
- local FFmpeg for heavy operations.

---

# 33. Final Rendering

Final rendering is authoritative.

Inputs:

- original media
- camera recording
- timeline
- visibility events
- PiP geometry
- layer order
- audio settings
- output settings

Pipeline:

```text
Original Sources
       +
Camera Recordings
       +
Timeline
       +
Layer Geometry
       +
Audio Configuration
       ↓
Timeline Reconstruction
       ↓
Scale/Padding
       ↓
Multi-layer Overlay
       ↓
Audio Mix
       ↓
Timestamp Normalization
       ↓
Encode
       ↓
FFprobe Validation
```

---

# 34. Independent Pause Rendering

A paused source must not accidentally pause the entire composition.

For each layer:

- reconstruct play intervals.
- reconstruct pause intervals.
- freeze the visual frame during intentional pause when required.
- control audio independently according to the layer audio policy.
- rebuild continuous output timestamps.
- composite all layers against the master timeline.

Segment-based intermediate rendering may be used where it produces more reliable synchronization than one enormous filter graph.

---

# 35. Export Formats

Primary:

- MP4 / H.264 + AAC
- WebM / VP9 + Opus
- MKV
- MOV

Future codec support may include:

- H.265/HEVC
- AV1

Only expose codecs actually available on the local FFmpeg build.

---

# 36. Export Resolutions

Landscape:

- 480p
- 720p
- 1080p
- 1440p
- 2160p / 4K
- Original/custom

Vertical:

- 720×1280
- 1080×1920
- 1440×2560
- 2160×3840

Square:

- 1080×1080
- 2160×2160

FPS:

- 24
- 25
- 30
- 50
- 60

Do not expose an impossible combination if the source/encoder/device cannot support it.

---

# 37. FFmpeg Encoder Strategy

Preflight local FFmpeg.

Prefer:

1. hardware encoder when correctly available and stable.
2. optimized software encoder fallback.
3. safer compatibility settings if necessary.

Never assume NVIDIA/AMD/Intel/Apple hardware exists.

Detect first.

---

# 38. Job System

Local jobs:

```text
QUEUED
  ↓
PREPARING
  ↓
RUNNING
  ↓
VALIDATING
  ↓
COMPLETED
```

Failure:

```text
RUNNING
  ↓
RECOVERING
  ↓
RETRY/FALLBACK
  ↓
FAILED or COMPLETED
```

Only bounded retries.

No infinite retry loops.

---

# 39. FFmpeg Progress

Run FFmpeg using argument arrays, not shell string concatenation.

Capture progress using FFmpeg's machine-readable progress output.

Expose:

- percentage
- elapsed
- ETA where available
- current frame
- FPS
- speed
- output size
- current stage

---

# 40. Cancellation

Export cancellation must:

1. request graceful FFmpeg termination.
2. wait briefly.
3. force terminate if necessary.
4. clean temporary files.
5. preserve originals.
6. mark job CANCELLED.

---

# 41. Crash Recovery

Store project state frequently.

Store:

- project JSON
- timeline JSON
- layer definitions
- source metadata
- export job metadata

If the app crashes:

- reopen last project.
- recover safe state.
- never delete originals during recovery.

---

# 42. Storage Safety

Before export:

- verify free disk space.
- estimate temporary space.
- verify output directory.
- verify source readability.

If insufficient space:

- refuse to start a large export.
- explain the required/available space.

Never silently corrupt or overwrite the source.

---

# 43. Security

Even though v1 is local:

- sanitize filenames.
- never build shell commands from raw user strings.
- use subprocess argument arrays.
- use random job IDs.
- restrict backend filesystem operations to configured project/storage roots.
- never expose arbitrary filesystem browsing.
- validate paths.
- limit temporary file lifetime.
- validate FFprobe results before rendering.

---

# 44. Logging

Use structured logs.

Directories:

```text
storage/
  logs/
  projects/
  proxies/
  recordings/
  exports/
  temp/
```

Log:

- startup
- FFmpeg detection
- FFprobe results
- source errors
- proxy creation
- camera errors
- recording events
- export stages
- recovery attempts
- final validation

Keep a concise user-facing diagnostic panel and detailed technical logs separately.

---

# 45. Self-Healing Strategy

Examples:

### FFmpeg not found

Search configured/common executable locations and report exact remediation.

### Camera constraint failure

Fallback:

```text
1080p → 720p → 480p
```

### Preview too heavy

Fallback:

```text
Original → 1080p → 720p → 480p
```

### Hardware encoder fails

Fallback to software encoder.

### Proxy fails

Retry at lower proxy resolution.

### Export fails

Use bounded fallback strategy and preserve all originals.

---

# 46. Performance Principles

Priority order:

1. Responsiveness.
2. A/V synchronization.
3. Playback stability.
4. Correctness.
5. Final output quality.
6. Extra UI features.

The app should never sacrifice synchronization merely to show a higher preview resolution.

---

# 47. UI Layout

Recommended desktop layout:

```text
+------------------------------------------------------+
|                    TOP TOOLBAR                       |
+----------------+----------------------+--------------+
|                |                      |              |
|   MEDIA/LAYER  |      MAIN CANVAS     |  CONTROLS    |
|     PANEL      |                      |              |
|                |                      |              |
|                |                      |              |
+----------------+----------------------+--------------+
|                 TIMELINE / EVENTS                    |
+------------------------------------------------------+
|                STATUS / PERFORMANCE                  |
+------------------------------------------------------+
```

Mobile:

```text
TOP BAR
CANVAS
LAYER LIST
CONTROLS
TIMELINE
STATUS
```

The UI must remain usable on Android portrait and landscape.

---

# 48. Project Data Model

A project contains:

```json
{
  "version": 1,
  "canvas": {
    "aspect": "16:9",
    "width": 1920,
    "height": 1080,
    "fps": 30
  },
  "layers": [],
  "timeline": [],
  "audio": {},
  "export": {}
}
```

Version project JSON from the beginning so future schema migrations are possible.

---

# 49. Proposed Project Tree

```text
AhmedReactionStudio/
│
├── app/
│   ├── server.py
│   ├── api/
│   │   ├── health.py
│   │   ├── media.py
│   │   ├── projects.py
│   │   ├── recording.py
│   │   └── export.py
│   │
│   ├── media/
│   │   ├── ffmpeg.py
│   │   ├── ffprobe.py
│   │   ├── proxy.py
│   │   ├── renderer.py
│   │   ├── timeline.py
│   │   ├── compositor.py
│   │   └── validators.py
│   │
│   ├── workers/
│   │   ├── job_manager.py
│   │   └── export_worker.py
│   │
│   └── core/
│       ├── config.py
│       ├── storage.py
│       ├── logging.py
│       └── recovery.py
│
├── web/
│   ├── index.html
│   ├── css/
│   │   └── studio.css
│   └── js/
│       ├── app.ts
│       ├── api.ts
│       ├── compositor.ts
│       ├── layers.ts
│       ├── pip-editor.ts
│       ├── camera.ts
│       ├── media.ts
│       ├── audio.ts
│       ├── recorder.ts
│       ├── timeline.ts
│       ├── performance.ts
│       └── ui.ts
│
├── scripts/
│   ├── start_windows.bat
│   ├── start_termux.sh
│   └── diagnostics.py
│
├── storage/
│   ├── projects/
│   ├── proxies/
│   ├── recordings/
│   ├── exports/
│   ├── temp/
│   └── logs/
│
├── requirements.txt
├── config.example.json
├── README.md
└── MASTER_BLUEPRINT.md
```

---

# 50. Development Phases

## Phase 1 — Foundation

- FastAPI local server.
- Static web app.
- configuration.
- logging.
- FFmpeg/FFprobe detection.
- diagnostics.
- storage manager.

## Phase 2 — Media

- local file loading.
- FFprobe.
- source metadata.
- proxy generation.
- playback health monitor.

## Phase 3 — Compositor

- main canvas.
- multiple layers.
- layer ordering.
- visibility.
- media assignment.
- PiP dragging.
- eight resize handles.
- presets.

## Phase 4 — Cameras

- camera enumeration.
- Android two-camera policy.
- Windows multi-camera support.
- camera fallback constraints.
- camera recording.

## Phase 5 — Audio

- Web Audio mixer.
- per-layer volume.
- mute.
- synchronization.

## Phase 6 — Timeline

- master clock.
- independent events.
- play/pause/seek.
- visibility events.
- geometry events.
- project persistence.

## Phase 7 — Recording

- countdown.
- compositor recording.
- audio recording.
- event capture.
- recovery.

## Phase 8 — Final Renderer

- independent pause reconstruction.
- freeze frames.
- audio reconstruction.
- multi-layer FFmpeg compositor.
- timestamp normalization.
- FFprobe validation.

## Phase 9 — Export

- formats.
- resolution presets.
- FPS.
- encoder detection.
- progress.
- cancellation.

## Phase 10 — Hardening

- stress tests.
- long-duration tests.
- multiple PiPs.
- multiple cameras.
- 144p/480p/720p/1080p/4K/8K.
- VFR.
- unusual codecs.
- audio drift tests.
- crash recovery.
- low-RAM tests.
- Termux tests.
- Windows tests.

---

# 51. Acceptance Criteria

The release is not considered production-ready until:

- Main and every PiP can play/pause independently.
- Multiple PiPs render in correct z-order.
- Visibility does not equal pause.
- Eight resize handles work correctly.
- Dragging works with mouse/touch/stylus.
- Android supports up to two camera sources when the device/browser permits.
- Windows supports multiple camera sources within hardware limits.
- Camera failure degrades gracefully.
- Heavy sources automatically receive suitable proxies.
- Preview remains responsive under supported device limits.
- Timeline events reconstruct correctly.
- Independent pauses do not pause other layers.
- Audio remains synchronized.
- Final export is validated with FFprobe.
- Originals are never modified.
- Export cancellation cleans temporary files.
- Recovery never loops forever.
- Insufficient disk space is detected before dangerous operations.
- Application works fully offline after dependencies are installed.
- Windows and Termux startup scripts work.
- No PythonAnywhere dependency exists.

---

# 52. Non-Negotiable Rules

1. **Local first.**
2. **FFmpeg is the authoritative final media engine.**
3. **Python orchestrates; it does not process every frame.**
4. **Browser APIs handle live camera/preview work.**
5. **No React/Electron/large framework unless a future benchmark proves it necessary.**
6. **GStreamer is optional, not mandatory.**
7. **Original media is immutable.**
8. **Every independent layer has independent state.**
9. **Pause/play and hide/show are separate concepts.**
10. **Timeline synchronization uses a monotonic master clock, not frame numbers.**
11. **Adaptive proxying protects playback stability.**
12. **Final export uses the highest-quality valid source available.**
13. **All recovery attempts are bounded.**
14. **No silent data loss.**
15. **No cloud dependency in v1.**
16. **Android camera count is capped at two.**
17. **Windows camera count is capability-based.**
18. **Performance monitoring is built into the application, not added later.**

---

# 53. Final Technology Lock

| Area | Final choice |
|---|---|
| Language | Python + TypeScript |
| Backend | FastAPI |
| Server | Uvicorn |
| Frontend | HTML/CSS/TypeScript |
| Live video | HTMLVideoElement |
| Live compositor | Canvas 2D |
| Camera | MediaDevices/getUserMedia |
| Audio | Web Audio API |
| Recording | MediaRecorder |
| Media inspection | FFprobe |
| Final media engine | FFmpeg |
| Optional real-time engine | GStreamer |
| Background work | Local Python worker |
| Database | SQLite |
| Storage | Local filesystem |
| Windows | First-class |
| Android/Termux | First-class |
| PythonAnywhere | Not used in v1 |
| Cloud | Not required |
| React | Not used in v1 |
| Electron | Not used |
| Django | Not used |

---

# 54. Final Architecture Decision

The final system is:

```text
             LOCAL DEVICE
                  |
       +----------+----------+
       |                     |
   BROWSER UI             FASTAPI
       |                     |
       |              Project/Jobs/
       |              Media control
       |                     |
       +----------+----------+
                  |
             Local Storage
                  |
               FFmpeg
                  |
             Final Export
```

The design intentionally separates:

**real-time interaction → browser**

**application orchestration → Python**

**heavy authoritative media processing → FFmpeg**

This provides the strongest practical architecture for a lightweight local reaction studio on Windows and Android/Termux without making the system dependent on PythonAnywhere or a cloud service.

Absolute zero-stutter/zero-latency cannot be guaranteed on every device and codec combination; the system instead uses hardware/browser decoding, adaptive proxies, performance monitoring, bounded fallbacks, and timestamp-based synchronization to minimize latency and gracefully handle hardware limits.
