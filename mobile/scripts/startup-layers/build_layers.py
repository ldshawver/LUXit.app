"""Split assets/splash-logo.png into the layers the animated startup uses.

Dev tooling only (not bundled, not an app dependency). Requires numpy, scipy,
pillow:  python3 -m venv /tmp/v && /tmp/v/bin/pip install numpy scipy pillow
         /tmp/v/bin/python scripts/startup-layers/build_layers.py

Outputs (assets/startup/, all 450x450, same canvas as splash-logo.png):
  ring.png         the waveform annulus (rotates/pulses). Pixels the artwork
                   hides (under the lettering plaque, under the two scan
                   lines) are filled by mirroring the adjacent visible
                   waveform; they only show while the ring is turned/scaled.
  ring_bright.png  ring.png brightened, faded in on each "beat".
  static.png       everything else, unchanged: lettering plaque, centre
                   backdrop, scan lines (stationary).
  glint.png        white highlight weighted to the bright metal of the
                   letters, revealed by a moving window (shimmer).

At rest static-over-ring reproduces splash-logo.png (checked below), so the
hand-off from the native splash is invisible.
"""
import json
import os
import sys

import numpy as np
from PIL import Image
from scipy import ndimage as nd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..', '..'))
SRC = os.path.join(ROOT, 'assets', 'splash-logo.png')
OUT = os.path.join(ROOT, 'assets', 'startup')

R_IN_MARGIN = 3.0    # px: ring layer starts this far inside the bars' inner edge
R_FILL_MAX = 216.0   # px: hidden pixels beyond the waveform's outer edge stay empty
PLAQUE_GROW = 4      # px: plaque mask growth that captures its coloured border
SCAN_ROWS = [(215, 219), (242, 245)]  # inclusive rows of the two scan lines incl. anti-aliasing


def load():
    im = np.array(Image.open(SRC).convert('RGBA')).astype(np.float64)
    return im


def plaque_mask(im):
    """Lettering plaque = the thick black outline band around LUX with its
    holes (the letters) filled, grown PLAQUE_GROW px to take its thin coloured border and anti-aliasing."""
    rgb, a = im[..., :3], im[..., 3]
    dark = (rgb.max(-1) < 18) & (a == 255)
    lab, _ = nd.label(dark)
    seed = lab[200, 76]  # inside the black band left of the "L"
    assert seed, 'seed not in the dark outline band'
    m = nd.binary_fill_holes(lab == seed)
    return nd.binary_dilation(m, iterations=PLAQUE_GROW)


def inner_edge(im, plaque, scan, r, th):
    """Per-pixel radius where the waveform bars begin (1-degree bins, gaps the
    plaque hides interpolated around the circle, widened by a +-4 degree min)."""
    rgb, a = im[..., :3], im[..., 3]
    mx, mn = rgb.max(-1), rgb.min(-1)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1), 0)
    bar = (sat > 0.5) & (mx > 90) & (a > 128) & (r > 110) & ~plaque & ~scan
    deg = (np.degrees(th) + 360) % 360
    bins = np.full(360, np.nan)
    for d in range(360):
        rs = r[bar & (deg >= d) & (deg < d + 1)]
        if rs.size >= 8:
            bins[d] = np.percentile(rs, 3)
    ok = ~np.isnan(bins)
    idx = np.arange(360)
    bins = np.interp(idx, np.concatenate([idx[ok] - 360, idx[ok], idx[ok] + 360]), np.tile(bins[ok], 3))
    bins = np.array([bins[(idx + k) % 360] for k in range(-4, 5)]).min(0)
    bins = np.clip(bins - R_IN_MARGIN, 120, 175)
    return bins[np.floor(deg).astype(int) % 360]


def polar(h, w):
    cy, cx = (h - 1) / 2, (w - 1) / 2
    yy, xx = np.mgrid[0:h, 0:w]
    return cx, cy, np.hypot(xx - cx, yy - cy), np.arctan2(yy - cy, xx - cx)


def bilinear(img, x, y):
    h, w = img.shape[:2]
    x = np.clip(x, 0, w - 1.001); y = np.clip(y, 0, h - 1.001)
    x0, y0 = np.floor(x).astype(int), np.floor(y).astype(int)
    fx, fy = (x - x0)[..., None], (y - y0)[..., None]
    p = img  # premultiplied in, premultiplied out
    return (p[y0, x0] * (1 - fx) * (1 - fy) + p[y0, x0 + 1] * fx * (1 - fy)
            + p[y0 + 1, x0] * (1 - fx) * fy + p[y0 + 1, x0 + 1] * fx * fy)


def premul(im):
    out = im.copy(); out[..., :3] *= im[..., 3:4] / 255.0
    return out


def unpremul(p):
    out = p.copy(); a = p[..., 3:4]
    out[..., :3] = np.where(a > 0, p[..., :3] * 255.0 / np.maximum(a, 1e-6), 0)
    return out


def main():
    im = load()
    h, w = im.shape[:2]
    cx, cy, r, th = polar(h, w)
    plaque = plaque_mask(im)

    # Scan-line pixels outside the plaque (they cross the waveform bars).
    rows = np.zeros((h, w), bool)
    for y0, y1 in SCAN_ROWS:
        rows[y0:y1 + 1] = True
    scan = rows & ~plaque

    r_in = inner_edge(im, plaque, scan, r, th)
    ring_region = (r >= r_in) & ~plaque
    hidden = (r >= r_in) & (r <= R_FILL_MAX) & plaque & (im[..., 3] == 255)  # only what the plaque fully covers

    pm = premul(im)
    ring = np.where(ring_region[..., None], pm, 0.0)

    # 1) Under the scan lines: interpolate each column between the clean rows
    #    just above and below the band (bars are near-horizontal there).
    for y0, y1 in SCAN_ROWS:
        top, bot = pm[y0 - 1], pm[y1 + 1]
        for y in range(y0, y1 + 1):
            t = (y - (y0 - 1)) / ((y1 + 1) - (y0 - 1))
            fill = top * (1 - t) + bot * t
            sel = ring_region[y]
            ring[y, sel] = fill[sel]

    # Where a scan line is not fully opaque (its tips, past the waveform) the
    # ring layer must stay empty or the rest frame would differ.
    ring[scan & (im[..., 3] < 255)] = 0.0

    # 2) Under the plaque: mirror the waveform about the nearest angle at the
    #    same radius where it is visible (radial bars stay radial, colour stays
    #    continuous across the seam).
    vis = ring_region & ~scan
    ys, xs = np.nonzero(hidden)
    step = np.radians(0.25)
    filled = 0
    for y, x in zip(ys, xs):
        rr, t0 = r[y, x], th[y, x]
        for k in range(1, 480):
            for sgn in (1, -1):
                tb = t0 + sgn * k * step
                bx, by = cx + rr * np.cos(tb), cy + rr * np.sin(tb)
                ix, iy = int(round(bx)), int(round(by))
                if 0 <= ix < w and 0 <= iy < h and vis[iy, ix]:
                    ts = 2 * tb - t0
                    ring[y, x] = bilinear(ring, np.array(cx + rr * np.cos(ts)), np.array(cy + rr * np.sin(ts)))
                    filled += 1
                    break
            else:
                continue
            break

    # Static layer: everything that is not the ring region, plus the scan-line
    # pixels, all unchanged from the original (so the rest frame is exact).
    static = np.where((ring_region & ~scan)[..., None], 0.0, pm)

    def save(p, name):
        out = np.clip(np.round(unpremul(p)), 0, 255).astype(np.uint8)
        out[out[..., 3] == 0] = 0  # transparent pixels black: no light fringe when scaled
        Image.fromarray(out, 'RGBA').save(os.path.join(OUT, name), optimize=True)
        return out

    os.makedirs(OUT, exist_ok=True)
    ring_u8 = save(ring, 'ring.png')
    static_u8 = save(static, 'static.png')

    bright = ring.copy()
    bright[..., :3] = np.minimum(bright[..., :3] * 1.45 + 18 * bright[..., 3:4] / 255.0, bright[..., 3:4])
    save(bright, 'ring_bright.png')

    rgb = im[..., :3]
    mx, mn = rgb.max(-1), rgb.min(-1)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1), 0)
    metal = np.clip((mx - 110) / 120, 0, 1) ** 1.2 * np.clip(1 - sat * 1.4, 0, 1)
    glint_a = np.where(plaque, metal * 255, 0)
    glint = np.zeros((h, w, 4)); glint[..., :3] = glint_a[..., None]; glint[..., 3] = glint_a
    save(glint, 'glint.png')

    # Rest-state check: static over ring vs the original.
    def over(top, bot):
        ta = top[..., 3:4] / 255.0
        return top + bot * (1 - ta)
    rest = unpremul(over(premul(static_u8.astype(float)), premul(ring_u8.astype(float))))
    vis_px = im[..., 3] > 0
    diff = np.abs(rest - im).max(-1)
    report = {
        'hidden_px_filled': int(filled), 'hidden_px': int(hidden.sum()),
        'scan_px_over_ring': int((scan & ring_region).sum()),
        'rest_max_channel_diff': float(diff[vis_px].max()),
        'rest_px_diff_gt_1': int((diff[vis_px] > 1).sum()),
        'visible_px': int(vis_px.sum()),
    }
    assert report['rest_px_diff_gt_1'] == 0, 'layers no longer reproduce splash-logo.png at rest'
    print(json.dumps(report, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
