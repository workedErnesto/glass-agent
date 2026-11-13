"""Визуалы для постов без OpenAI Image API.

Основной вариант — сборка из шаблона (Pillow): фон из медиатеки компании или
градиент фирменных цветов, текстовые блоки, логотип, экспорт PNG. Такое
изображение помечается generator=pillow_template, ai_generated=False и не
выдаётся за генерацию моделью.

Дополнительно — локальная генеративная модель (diffusers), если она установлена
и задана LOCAL_IMAGE_MODEL; её результат помечается ai_generated=True.
"""
from __future__ import annotations

import os
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

TEMPLATES = {"square": (1080, 1080), "portrait": (1080, 1350), "story": (1080, 1920), "wide": (1280, 720)}
DEFAULT_COLORS = ["#0B3D5C", "#1F7A8C", "#FFFFFF"]  # фон-1, фон-2, текст
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
FONT_CANDIDATES = {
    False: ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/Library/Fonts/Arial Unicode.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf"],
    True: ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
           "/Library/Fonts/Arial Unicode.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"],
}


class ImageError(RuntimeError):
    pass


def find_font(bold: bool = False) -> str:
    env = os.environ.get("GLASS_FONT_BOLD" if bold else "GLASS_FONT")
    for p in ([env] if env else []) + FONT_CANDIDATES[bold] + FONT_CANDIDATES[not bold]:
        if p and Path(p).is_file():
            return p
    raise ImageError("Не найден шрифт с кириллицей: установите DejaVu (fonts-dejavu) или задайте GLASS_FONT")


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def media_dir(data_dir: Path, slug: str) -> Path:
    return Path(data_dir) / slug / "media"


def safe_media_path(data_dir: Path, slug: str, name: str | None) -> Path | None:
    """Файл только из медиатеки компании (без выхода за её каталог)."""
    if not name:
        return None
    root = media_dir(data_dir, slug).resolve()
    p = (root / name).resolve()
    if root not in p.parents or not p.is_file() or p.suffix.lower() not in IMAGE_EXT:
        raise ImageError(f"Файл {name!r} не найден в медиатеке компании ({root})")
    return p


def list_media(data_dir: Path, slug: str) -> list[str]:
    root = media_dir(data_dir, slug)
    return sorted(p.name for p in root.glob("*") if p.suffix.lower() in IMAGE_EXT) if root.is_dir() else []


def _gradient(size: tuple[int, int], c1: str, c2: str) -> Image.Image:
    w, h = size
    top, bottom = _rgb(c1), _rgb(c2)
    grad = Image.new("RGB", (1, 256))
    for y in range(256):
        t = y / 255
        grad.putpixel((0, y), tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3)))
    return grad.resize((w, h))


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font_path: str, max_w: int, max_h: int,
              start: int, minimum: int = 28) -> tuple[ImageFont.FreeTypeFont, str]:
    size = start
    while size >= minimum:
        font = ImageFont.truetype(font_path, size)
        avg = max(font.getlength("абвгдежзик") / 10, 1)
        wrapped = "\n".join(textwrap.wrap(text, width=max(8, int(max_w / avg))))
        box = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=int(size * 0.25))
        if box[2] - box[0] <= max_w and box[3] - box[1] <= max_h:
            return font, wrapped
        size -= 4
    font = ImageFont.truetype(font_path, minimum)
    return font, "\n".join(textwrap.wrap(text, width=max(8, int(max_w / (minimum * 0.55)))))


@dataclass
class RenderResult:
    path: Path
    meta: dict[str, Any]


def render_template(out_path: Path, *, title: str, subtitle: str = "", footer: str = "",
                    template: str = "square", colors: list[str] | None = None,
                    background: Path | None = None, logo: Path | None = None) -> RenderResult:
    if template not in TEMPLATES:
        raise ImageError(f"Неизвестный шаблон {template}; есть: {', '.join(TEMPLATES)}")
    size = TEMPLATES[template]
    w, h = size
    colors = (colors or []) + DEFAULT_COLORS[len(colors or []):]
    c1, c2, text_color = colors[0], colors[1], colors[2]
    if background:
        img = ImageOps.fit(Image.open(background).convert("RGB"), size, Image.Resampling.LANCZOS)
        shade = Image.new("RGB", size, _rgb(c1))
        img = Image.blend(img.filter(ImageFilter.GaussianBlur(1)), shade, 0.45)  # читаемость текста
    else:
        img = _gradient(size, c1, c2)
    draw = ImageDraw.Draw(img)
    pad = int(w * 0.08)
    bold, regular = find_font(True), find_font(False)

    draw.rectangle([pad, pad, pad + int(w * 0.12), pad + 10], fill=_rgb(text_color))  # фирменная плашка
    title_font, title_wrapped = _fit_text(draw, title.strip(), bold, w - 2 * pad, int(h * 0.45), start=int(w * 0.085))
    y = int(h * 0.2)
    draw.multiline_text((pad, y), title_wrapped, font=title_font, fill=_rgb(text_color),
                        spacing=int(title_font.size * 0.25))
    y = draw.multiline_textbbox((pad, y), title_wrapped, font=title_font, spacing=int(title_font.size * 0.25))[3]
    if subtitle.strip():
        sub_font, sub_wrapped = _fit_text(draw, subtitle.strip(), regular, w - 2 * pad, int(h * 0.25),
                                          start=int(w * 0.042), minimum=24)
        draw.multiline_text((pad, y + int(h * 0.04)), sub_wrapped, font=sub_font, fill=_rgb(text_color),
                            spacing=int(sub_font.size * 0.3))
    if footer.strip():
        f_font = ImageFont.truetype(regular, int(w * 0.03))
        draw.text((pad, h - pad - f_font.size), footer.strip()[:80], font=f_font, fill=_rgb(text_color))
    if logo:
        lg = Image.open(logo).convert("RGBA")
        lg.thumbnail((int(w * 0.22), int(h * 0.1)))
        img.paste(lg, (w - pad - lg.width, h - pad - lg.height), lg)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG", optimize=True)
    return RenderResult(out_path, {
        "generator": "pillow_template", "ai_generated": False, "template": template, "size": list(size),
        "background": background.name if background else "gradient", "logo": bool(logo), "colors": colors[:3],
    })


def render_for_company(data_dir: Path, slug: str, settings: dict[str, Any], out_name: str, *, title: str,
                       subtitle: str = "", template: str = "square", background: str | None = None) -> RenderResult:
    """Рендер с фирменным стилем компании: settings.brand_colors, settings.logo (файл медиатеки), settings.site."""
    bg = safe_media_path(data_dir, slug, background)
    logo = safe_media_path(data_dir, slug, settings.get("logo")) if settings.get("logo") else None
    out = Path(data_dir) / slug / "images" / f"{out_name}.png"
    return render_template(out, title=title, subtitle=subtitle, footer=settings.get("site", ""), template=template,
                           colors=settings.get("brand_colors"), background=bg, logo=logo)


# --------------------------------------------------------------------------- локальная генеративная модель

def local_generation_status(model: str | None) -> dict[str, Any]:
    if not model:
        return {"available": False, "reason": "LOCAL_IMAGE_MODEL не задан"}
    try:
        import diffusers  # noqa: F401
        import torch
    except ImportError:
        return {"available": False, "reason": "не установлен пакет diffusers"}
    device = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else None
    if device is None:
        return {"available": False, "reason": "нет GPU (CUDA/MPS) — генерация на CPU слишком медленная"}
    return {"available": True, "model": model, "device": device}


def generate_local(model: str, prompt: str, out_path: Path, size: tuple[int, int] = (1024, 1024)) -> RenderResult:
    st = local_generation_status(model)
    if not st["available"]:
        raise ImageError(f"Локальная генерация недоступна: {st['reason']}")
    import torch
    from diffusers import AutoPipelineForText2Image

    pipe = AutoPipelineForText2Image.from_pretrained(model, torch_dtype=torch.float16).to(st["device"])
    image = pipe(prompt=prompt, width=size[0], height=size[1]).images[0]
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path, "PNG")
    return RenderResult(out_path, {"generator": "local_diffusion", "ai_generated": True, "model": model,
                                   "prompt": prompt[:500]})
