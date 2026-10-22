"""Build the launch animation and the matching native splash image from the
supplied LUX animated GIF.

Dev tooling only (not bundled, not an app dependency). Requires numpy, pillow
(with WebP):  python3 -m venv /tmp/v && /tmp/v/bin/pip install numpy pillow
              /tmp/v/bin/python scripts/startup-animation/build_animation.py [GIF]

GIF defaults to assets/LUX-Animated_compressed.gif (450x450, 227 frames,
sha256 4b259c7f...2bc). The source is not committed (8.3 MB); keep a copy to
rebuild.

What the GIF needs before it can be a launch screen:
  - its last frame is fully transparent and held 5.01 s: on the black
    background that is a 5 s blank screen every loop. Trailing blank frames
    are dropped, so the loop runs 225 -> 0 directly.
  - many frames draw over transparency (disposal "background"); the app shows
    them on black anyway, so every frame is flattened onto #000000 here and the
    animation is fully opaque (no alpha blending on device).
  - 21 frames repeat the previous one: merged into one 60 ms frame.

Outputs:
  assets/startup/lux.webp   animated WebP, every frame a lossy keyframe
                            (q70; inter-frame encoding smeared the thin
                            waveform bars, worst frame 16 dB PSNR). Played by
                            expo-image (src/ui/StartupScreen.tsx).
  assets/splash-logo.png    frame 0 *as decoded from lux.webp*, so the native
                            splash and the animation's first frame are the
                            same pixels and the hand-off is invisible.
"""
import os
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..', '..'))
SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'assets', 'LUX-Animated_compressed.gif')
OUT_WEBP = os.path.join(ROOT, 'assets', 'startup', 'lux.webp')
OUT_SPLASH = os.path.join(ROOT, 'assets', 'splash-logo.png')

QUALITY = 70
MIN_PSNR = 28.0  # dB, any frame vs the flattened source


def flatten(gif):
    """[(rgb array, duration ms)] on black, without trailing fully transparent frames."""
    frames = []
    for i in range(gif.n_frames):
        gif.seek(i)
        rgba = gif.convert('RGBA')
        blank = rgba.getextrema()[3][1] == 0
        canvas = Image.new('RGBA', rgba.size, (0, 0, 0, 255))
        canvas.alpha_composite(rgba)
        frames.append((np.asarray(canvas.convert('RGB')), gif.info.get('duration', 0), blank))
    while frames and frames[-1][2]:
        frames.pop()
    return [(a, d) for a, d, _ in frames]


def merge_repeats(frames):
    out = []
    for a, d in frames:
        if out and np.array_equal(out[-1][0], a):
            out[-1][1] += d
        else:
            out.append([a, d])
    return out


def psnr(a, b):
    mse = ((a.astype(np.float32) - b.astype(np.float32)) ** 2).mean()
    return float('inf') if mse == 0 else 10 * np.log10(255 ** 2 / mse)


def main():
    gif = Image.open(SRC)
    frames = merge_repeats(flatten(gif))
    if not frames:
        sys.exit('no visible frames')
    durations = [d for _, d in frames]
    if min(durations) <= 0:
        sys.exit(f'zero-length frame in source: {durations}')
    images = [Image.fromarray(a) for a, _ in frames]

    os.makedirs(os.path.dirname(OUT_WEBP), exist_ok=True)
    images[0].save(
        OUT_WEBP, save_all=True, append_images=images[1:], duration=durations, loop=0,
        quality=QUALITY, method=6, lossless=False, kmax=1, background=(0, 0, 0, 255),
    )

    out = Image.open(OUT_WEBP)
    assert out.n_frames == len(frames), (out.n_frames, len(frames))
    worst = min(psnr(np.asarray((out.seek(i), out.convert('RGB'))[1]), frames[i][0]) for i in range(out.n_frames))
    assert worst >= MIN_PSNR, f'worst frame {worst:.1f} dB < {MIN_PSNR}'

    out.seek(0)
    first = out.convert('RGB')
    first.save(OUT_SPLASH, optimize=True)
    assert np.array_equal(np.asarray(Image.open(OUT_SPLASH).convert('RGB')), np.asarray(first))

    print(
        f'{len(frames)} frames, {sum(durations)} ms loop, {os.path.getsize(OUT_WEBP)} bytes, '
        f'worst frame {worst:.1f} dB; splash = decoded frame 0'
    )


if __name__ == '__main__':
    main()
