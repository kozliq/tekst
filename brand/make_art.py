"""Аватарка и баннер бота «Текст» — из герба «Чисто», как у Кружка.

Берём щит Чисто (brand/kit/crest-transparent.png из github.com/kozliq/chisto, рядом с этой папкой): рамка та же,
поле перекрашиваем заново, кобру убираем. Мангуст прыгает на красную звуковую волну, а она переходит
в строчки текста цвета кости — голос в текст. Без надписей.

Нужны Pillow и numpy (в обычный Python бота не ставим):
  python3 -m venv /tmp/art && /tmp/art/bin/pip install pillow numpy && /tmp/art/bin/python make_art.py
"""
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
KIT = HERE.parent.parent / "chisto" / "brand" / "kit"
BG = (0x1c, 0x1a, 0x1b)
FIELD_L, FIELD_R = (96, 96, 96), (62, 62, 62)     # поле щита: левая половина светлее, как у Чисто
INK = (8, 8, 8)
RED = (0x87, 0x37, 0x30)
INSET = 64          # от края щита до поля (рамка с двумя чёрными линиями), px исходника
PAD = 40            # поля вокруг исходника
SS = 4              # рисуем волну в 4 раза крупнее и уменьшаем — края гладкие


def erode(mask, px):
    """Сжать маску на px пикселей (чтобы получить поле внутри рамки)."""
    for _ in range(px // 2):
        mask = mask.filter(ImageFilter.MinFilter(5))
    return mask


def bfs(ok, seeds):
    """Все точки, куда можно дойти от seeds по ok (соседи по 4 сторонам)."""
    h, w = ok.shape
    seen = np.zeros_like(ok)
    q = deque(s for s in seeds if ok[s])
    for s in q:
        seen[s] = True
    while q:
        y, x = q.popleft()
        for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
            if 0 <= ny < h and 0 <= nx < w and ok[ny, nx] and not seen[ny, nx]:
                seen[ny, nx] = True
                q.append((ny, nx))
    return seen


def mongoose(src, field, split):
    """Вырезать мангуста: всё, что на поле не похоже на поле и связано с его телом, + дырки внутри."""
    rgb = src[..., :3].astype(int)
    want = np.where(np.arange(src.shape[1])[None, :, None] < split, FIELD_L, FIELD_R)
    off = np.abs(rgb - want).max(-1) > 14
    red = (rgb[..., 0] - rgb[..., 1] > 30)
    body = off & ~red & field
    # мангуст — над коброй; кобру отсекаем по красному и по месту (голова кобры справа ниже лап)
    h, w = body.shape
    yy, xx = np.mgrid[:h, :w]
    body &= ~((yy > 480 + PAD) & (xx > 430 + PAD))
    body &= yy < 860 + PAD
    blob = bfs(body, [(y + PAD, x + PAD) for y, x in ((300, 420), (350, 300), (420, 500))])
    outside = bfs(~blob, [(0, 0), (h - 1, w - 1), (0, w - 1), (h - 1, 0)])
    return blob | ~outside


VARIANT = "C"


def crest():
    raw = Image.open(KIT / "crest-transparent.png").convert("RGBA")
    # щит касается краёв картинки — даём поля, иначе рамка по бокам не «сжимается» в поле
    src_img = Image.new("RGBA", (raw.width + 2 * PAD, raw.height + 2 * PAD))
    src_img.paste(raw, (PAD, PAD))
    src = np.array(src_img)
    w, h = src_img.size
    # внутри щита у исходника местами полупрозрачно — щит = всё, куда не дойти снаружи по прозрачному
    see = np.array(src_img.getchannel("A")) < 40
    outside = bfs(see, [(0, 0), (h - 1, w - 1), (0, w - 1), (h - 1, 0)])
    alpha = Image.fromarray(np.where(outside, 0, 255).astype(np.uint8))
    src[..., 3] = np.where(outside, src[..., 3], 255)
    field_img = erode(alpha, INSET)
    field = np.array(field_img) > 0
    top = np.array(alpha)
    split = int(top[np.argmax(top.any(1))].nonzero()[0].mean())     # остриё сверху = середина щита
    beast = mongoose(src, field, split)

    out = src.copy()
    # хвост кобры заходит на рамку справа внизу — там ставим зеркальный кусок левой рамки
    rgb = src[..., :3].astype(int)
    reddish = (rgb[..., 0] - rgb[..., 1] > 30) & ~field & (np.array(alpha) > 0)
    if reddish.any():
        ys, xs = np.nonzero(reddish)
        y0, y1, x0, x1 = ys.min() - 20, ys.max() + 20, xs.min() - 20, xs.max() + 20
        mirror = out[y0:y1, 2 * split - x1 + 1:2 * split - x0 + 1][:, ::-1].astype(float)
        right = out[y0:y1, x0:x1]
        band = ~field[y0:y1, x0:x1] & (np.array(alpha)[y0:y1, x0:x1] > 0)
        clean = band & ~reddish[y0:y1, x0:x1]
        k = right[clean, :3].mean() / max(1, mirror[clean, :3].mean())     # правая сторона рамки темнее
        right[band, :3] = np.clip(mirror[band, :3] * k, 0, 255).astype(np.uint8)
    xs = np.arange(w)[None, :]
    out[..., :3] = np.where(field[..., None], np.where(xs[..., None] < split, FIELD_L, FIELD_R), out[..., :3])
    line = field & ~(np.array(erode(field_img, 4)) > 0)       # тонкая чёрная линия по краю поля, как у Чисто
    out[line, :3] = INK
    base = Image.fromarray(out)

    ys, xs_ = np.nonzero(beast)
    bx0, by0, bx1, by1 = xs_.min(), ys.min(), xs_.max() + 1, ys.max() + 1
    m = src.copy()
    m[..., 3] = np.where(beast, 255, 0)
    m_img = Image.fromarray(m).crop((bx0, by0, bx1, by1))
    fy, fx = np.nonzero(field)
    box = (fx.min(), fy.min(), fx.max(), fy.max())
    layer = Image.new("RGBA", (w, h))
    draw_sign(layer, box, m_img, VARIANT)
    layer.putalpha(ImageChops.multiply(layer.getchannel("A"), field_img))   # ничего не вылезает за рамку
    base.alpha_composite(layer)
    return base.crop(base.getbbox())


BONE = (0xcf, 0xc2, 0xb3)
# огибающая голоса: громче-тише, как у настоящей фразы (без случайности — картинка всегда одна)
ENVELOPE = [0.18, 0.35, 0.62, 0.9, 0.7, 0.45, 0.8, 1.0, 0.75, 0.5, 0.3, 0.55, 0.85, 0.6, 0.35, 0.2]


def bars(d, x0, x1, cy, height, n, color, k):
    """Звуковая волна: n столбиков с круглыми концами, высота по ENVELOPE."""
    step = (x1 - x0) / n
    wd = step * 0.56
    for i in range(n):
        hgt = max(wd, height * ENVELOPE[i % len(ENVELOPE)])
        cx = x0 + step * (i + 0.5)
        rect = (cx - wd / 2, cy - hgt / 2, cx + wd / 2, cy + hgt / 2)
        d.rounded_rectangle([v - 3 * k if j < 2 else v + 3 * k for j, v in enumerate(rect)], radius=wd / 2 + 3 * k,
                            fill=INK)
        d.rounded_rectangle(rect, radius=wd / 2, fill=color)


def lines(d, x0, x1, cy, height, n, k):
    """Строчки текста: n полос цвета кости, последняя короче."""
    gap = height / n
    th = gap * 0.42
    for i in range(n):
        y = cy - height / 2 + gap * (i + 0.5)
        right = x1 if i < n - 1 else x0 + (x1 - x0) * 0.55
        r = (x0, y - th / 2, right, y + th / 2)
        d.rounded_rectangle((r[0] - 3 * k, r[1] - 3 * k, r[2] + 3 * k, r[3] + 3 * k), radius=th / 2 + 3 * k, fill=INK)
        d.rounded_rectangle(r, radius=th / 2, fill=BONE)


def draw_sign(layer, box, beast, variant):
    """Знак на поле щита. A — волна переходит в строчки; B — мангуст над волной; C — мангуст + волна → строчки."""
    fx0, fy0, fx1, fy1 = box
    fw, fh = fx1 - fx0, fy1 - fy0
    big = Image.new("RGBA", (layer.width * SS, layer.height * SS))
    d = ImageDraw.Draw(big)
    k = SS
    X = lambda v: (fx0 + fw * v) * SS     # noqa: E731
    Y = lambda v: (fy0 + fh * v) * SS     # noqa: E731
    if variant == "A":
        bars(d, X(0.14), X(0.52), Y(0.42), fh * 0.36 * SS, 8, RED, k)
        lines(d, X(0.58), X(0.86), Y(0.42), fh * 0.30 * SS, 4, k)
    elif variant == "B":
        bars(d, X(0.12), X(0.88), Y(0.66), fh * 0.26 * SS, 16, RED, k)
    else:
        bars(d, X(0.16), X(0.54), Y(0.70), fh * 0.22 * SS, 9, RED, k)
        lines(d, X(0.60), X(0.84), Y(0.70), fh * 0.17 * SS, 3, k)
    layer.alpha_composite(big.resize(layer.size, Image.LANCZOS))
    if variant in ("B", "C"):
        s = fw * (0.62 if variant == "B" else 0.54) / beast.width
        b = beast.resize((round(beast.width * s), round(beast.height * s)), Image.LANCZOS)
        layer.alpha_composite(b, (int(fx0 + (fw - b.width) / 2 + fw * 0.03), int(fy0 + fh * 0.05)))


def on_bg(img, size, height_share):
    canvas = Image.new("RGBA", size, BG + (255,))
    k = size[1] * height_share / img.height
    s = img.resize((round(img.width * k), round(img.height * k)), Image.LANCZOS)
    canvas.alpha_composite(s, ((size[0] - s.width) // 2, (size[1] - s.height) // 2))
    return canvas.convert("RGB")


def main():
    import sys
    global VARIANT
    if len(sys.argv) > 1:       # make_art.py try — три варианта рядом, чтобы выбрать
        for v in "ABC":
            VARIANT = v
            on_bg(crest(), (640, 640), 0.80).save(HERE / f"try-{v}.png")
        return
    c = crest()
    c.save(HERE / "crest-tekst-transparent.png")
    on_bg(c, (640, 640), 0.80).save(HERE / "avatar-640.jpg", quality=92)
    on_bg(c, (640, 640), 0.80).save(HERE / "avatar-640.png")
    on_bg(c, (640, 360), 0.84).save(HERE / "tg-banner-640x360.png")
    on_bg(c, (1280, 720), 0.84).save(HERE / "tg-banner-1280x720.png")
    print("готово:", ", ".join(p.name for p in sorted(HERE.glob("*.png")) + sorted(HERE.glob("*.jpg"))))


if __name__ == "__main__":
    main()
