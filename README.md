# CoffeeBreak Cam Pro

*The webcam that stays in the meeting, so you don't have to.*

![CoffeeBreak Cam Pro](docs/screenshot.png)

CoffeeBreak Cam Pro sits between your real webcam and your video calls. It shows up as a
virtual camera, and from then on **you** decide what the meeting sees: you, a frozen you, a
looping you nodding thoughtfully, or a heartbreakingly bad connection that nobody can blame you for.

## Why "Pro"?

Because every app is Pro these days. Your notes app is Pro. Your calculator is Pro Max.
We briefly considered the full name:

> **CoffeeBreak Cam Pro Max Ultra — Venti Iced Half-Caf Oat-Milk Caramel Cloud Edition,
> with an extra shot of plausible deniability**

…but it didn't fit on the button. So: Pro. Please pronounce it with the confidence of a
barista calling out a twelve-word drink order.

## What's on the menu

### The four big buttons

| Button | What the meeting sees |
|---|---|
| **LIVE** | You. Fresh. It also clears every quick preset and walks you out of the hedge. |
| **FREEZE** | You, perfectly still. Deep in thought. Probably. |
| **PLAY LOOP** | Up to 30 seconds of recorded you, played back and forth so nobody spots the seam. |
| **TURN OFF** | "Camera off". Your webcam is released (its light goes out), the app keeps running. The loop and a frozen image still play. |

Switching between live and loop briefly freezes the image, exactly like a real hiccup would.

### Quick presets

| Preset | What happens |
|---|---|
| **Connecting to camera / Camera not found** | The classics. Timeless. |
| **Bad connection** | Blocks, low frame rate and the occasional freeze. Not your fault. Clearly the wifi. |
| **Poor wifi / stuttering** | A "Buffering…" spinner at the most convenient moments. |
| **Partial glitches** | Half the picture hangs, a corner goes blocky. Lost packets. Happens to the best of us. |
| **Old webcam / low-res** | Grainy, low-res, slightly washed out. Vintage. Artisanal. |
| **Upside down** | The driver got confused. You're a professional; you'll carry on regardless. |
| **Disappear into the hedge** | A hedge grows in behind you, then closes in from both sides until you're gone. Click again to step back out. |
| **Default settings** | Everything back to normal. |

Every effect can also be set by hand on the **Effects** tab.

### Reactions

Confetti, fireworks, disco lights, hearts, balloons, rain and a coffee cup being chased by a
very determined sugar cube play a short animation over your image. Or pop up a single emoji
(👍 👎 😂 😮 😢 👏 🙈) that gently beats and tilts. No gestures needed, so nobody sees it coming.

Reactions are on the Controls tab. The emoji are also in the right-click menu of the ☕ button,
so they work with the panel closed.

## Background blur and backgrounds

Tick **Blur background** on the Controls tab and turn off the blur in Teams itself. Teams can't
tell a loading screen from your face, so it would blur those too; CoffeeBreak Cam Pro only
blurs images of you.

Under **Settings > Background blur** you can fine-tune it. The defaults are the nicest look:

| Setting | Default | What it does |
|---|---|---|
| **Strength** | 5 | 1 = just a little soft, 10 = completely out of focus. |
| **Lens blur** | on | Lamps and windows become soft round discs, like a real camera lens. |
| **Dim** | 10% | The background is slightly darker, so you stand out. |
| **Less colour** | 20% | The background is slightly greyer and calmer. |

The blur is halo-free: only the background is blurred, so your hair doesn't smear into a dark rim.

Prefer a picture? Add up to five images under **Effects > Background** and pick one; it replaces
everything behind you (and takes precedence over the blur). Right-click an image to remove it.

### How the blur finds you

A small person-segmentation model runs on your GPU via DirectML (NVIDIA, AMD or Intel), or on
the CPU when there is no usable GPU. It is only loaded while the blur, a background image or
the hedge is in use.

| Model | Size | Notes |
|---|---|---|
| **PP-HumanSeg** (included) | 6 MB | Fast, works fine on the CPU too. |
| **RVM MobileNetV3** (optional) | 15 MB | Finer edges and hair. Needs a GPU for live video; without one the app uses PP-HumanSeg automatically. |

RVM is not included in the release because of its license (GPL-3.0). To use it, download
`rvm_mobilenetv3_fp32.onnx` from the
[RobustVideoMatting releases](https://github.com/PeterL1n/RobustVideoMatting/releases) and put
it in the `models` folder (next to `app.py`, or in `_internal\models` of the .exe). It then
appears under **Settings > Segmentation model**.

## Before you start

Install [OBS Studio](https://obsproject.com/) once. CoffeeBreak Cam Pro borrows its
**OBS Virtual Camera** driver; OBS itself doesn't need to be running. Windows 10 or 11.

## Option 1: just download it (easiest)

1. Go to the [latest release](https://github.com/gen-x-coder/CoffeeBreakCamPro/releases/latest).
2. Download `CoffeeBreakCamPro-<version>-windows.zip`, unpack it anywhere (not in Program Files:
   the app saves its settings next to itself) and start **CoffeeBreakCamPro.exe**.

No Python needed. That's it. Go get a coffee.

## Option 2: run it from source

You need [Python 3.10+](https://www.python.org/downloads/) and [Git](https://git-scm.com/).

```
git clone https://github.com/gen-x-coder/CoffeeBreakCamPro
cd CoffeeBreakCamPro
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Use `opencv-python`, as in `requirements.txt`, not `opencv-python-headless`: on Windows the
headless build has no Media Foundation. Don't install both; they overwrite each other.

To update later: `git pull` in the same folder.

## Option 3: build the .exe yourself

From the source folder (option 2):

```
pip install pyinstaller
pyinstaller --noconsole --clean --name CoffeeBreakCamPro --add-data "models;models" app.py
```

The app ends up in `dist\CoffeeBreakCamPro\`. Two large files in there are not used by the app and
can be removed (PowerShell):

```
Get-ChildItem dist\CoffeeBreakCamPro -Recurse -Include opencv_videoio_ffmpeg*.dll, opengl32sw.dll | Remove-Item
```

Zip the whole folder to share it. Official releases are built the same way by GitHub Actions
(`.github/workflows/release.yml`) and attached to each release automatically.
(`--onefile` also works and gives a single .exe, but it unpacks itself on every start, which makes
starting noticeably slower.)

## Using it

1. Start CoffeeBreak Cam Pro. A small ☕ button appears on your screen.
2. In Teams, Zoom or Meet, pick **OBS Virtual Camera** as your camera.
3. Click the ☕ to open the panel, drag it to move it. Right-click it for emoji, camera on/off
   and quit.

The ☕ border tells you what the meeting sees: **green** your plain camera, **orange** live with
effects, **red** frozen, **blue** the loop, **grey** camera off.

## Barista notes

- **Settings** are remembered in `config.json` next to `app.py` or the .exe: camera, resolution,
  language, blur, backgrounds, window position and size.
- **Camera API.** Under Settings, *Automatic* uses Media Foundation (the modern Windows camera
  API, fastest start) and switches to DirectShow by itself if that gives no picture. Pick
  *DirectShow first* for older cameras.
- **Resolution.** 960×540 is a good default: video calls rarely show more, and it costs less
  than 720p. If you change it, the camera switches right away; the virtual camera follows on
  the next start.
- **Memory.** A 30-second loop takes roughly 20–30 MB. Less than one browser tab. Probably less
  than this README.
- **Diagnostics.** Settings > *Performance and diagnostics* shows the camera start-up timeline, frame
  rates, where the blur runs and how long it takes. Handy when something is slow; "Copy report"
  puts it on the clipboard.
- **Something wrong?** The app closes both cameras and writes the details to `crash.log`.

## License

CoffeeBreak Cam Pro is released under the [MIT License](LICENSE): use it, change it, share it.
A mention is appreciated; the license only asks you to keep the copyright notice.

The Windows release also contains third-party libraries, each under its own license:
PySide6 / Qt (LGPL-3.0), OpenCV (Apache-2.0), NumPy (BSD), pyvirtualcam (GPL-2.0),
pygrabber, comtypes, cv2-enumerate-cameras and ONNX Runtime (MIT), psutil and nvidia-ml-py (BSD).
The background blur uses the PP-HumanSeg model from the
[OpenCV Model Zoo](https://github.com/opencv/opencv_zoo) (Apache-2.0,
`models/LICENSE-PPHumanSeg.txt`). Their full license texts are included in the release zip as
`THIRD_PARTY_LICENSES.txt`. The OBS Virtual Camera driver is part of OBS Studio (GPL-2.0) and is
installed separately. The optional RVM model is GPL-3.0 and is not included.

## Disclaimer

Use responsibly. Your manager may also have read this README.
