"""把角色图处理成桌面悬浮窗可用的透明 PNG。

原图是白底 JPG，直接放进无边框窗口会是个白方块。这里做两件事：

1. **从四角泛洪**把「与边缘相连的白色」抠掉 —— 注意不能用「全图白色置透明」，
   因为角色身上的白围裙、白荷叶边也是白色，那样会把衣服打穿成洞。
   泛洪只吃掉连到画布边缘的部分，肚子上的白色因为被深色描边包住而保留。
2. **对 alpha 做轻微高斯模糊**再抠图，等于给边缘做抗锯齿。原图是描边厚涂风格，
   硬切会出现锯齿，模糊 0.8px 后放到桌面上边缘就干净了。

用法：
    python pet_asset.py                     # 处理 assets/pet_src.jpg → assets/pet.png
    python pet_asset.py 新图.jpg           # 换一张图，输出同名 png
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).parent
ASSETS = ROOT / "assets"
DEFAULT_SRC = ASSETS / "pet_src.jpg"
DEFAULT_DST = ASSETS / "pet.png"

TARGET_WIDTH = 560      # 2x 素材，窗口按 280 显示，高分屏也不糊
KEY = (255, 0, 255)     # 泛洪用的标记色，正常图片里不会出现
WHITE_TOLERANCE = 46    # JPG 压缩会让白底有噪点，容差给宽一点
EDGE_FEATHER = 0.8


def cut_out(src: Path, dst: Path, width: int = TARGET_WIDTH) -> Path:
    image = Image.open(src).convert("RGB")
    w, h = image.size

    # 在白底图上用醒目的颜色把「连到边缘的白」涂掉
    work = image.copy()
    for corner in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        try:
            ImageDraw.floodfill(work, corner, KEY, thresh=WHITE_TOLERANCE)
        except Exception:  # 该角本来就不是白色，跳过
            continue

    # 标记色 → alpha 0（numpy 向量化，逐像素 Python 循环在这张图上要卡好几秒）
    arr = np.asarray(work)
    is_bg = (arr[:, :, 0] == KEY[0]) & (arr[:, :, 1] == KEY[1]) & (arr[:, :, 2] == KEY[2])
    alpha = np.where(is_bg, 0, 255).astype(np.uint8)

    # 注：头发卷曲之间会夹住几块与外界不连通的纯白（视觉上是发丝的缝隙）。
    # 试过两种自动去掉它们的办法，都失败得比较难看：
    #   * 按「纯白 + 连通域面积」过滤 —— 头饰蕾丝、裙边荷叶边的白属于同一类，被一起打穿；
    #   * 先膨胀再泛洪（桥接细缝）—— 泛洪会顺着 2~3px 的描边漏进蕾丝内部。
    # 实测这些白和画布背景是同一个白色（都是 254,254,254 量级），不存在能自动区分的
    # 色彩特征，所以保留。缩到 280px 显示时它们读起来像发丝高光；
    # 要彻底干净得换 rembg 之类的抠图模型，为一张图不值得引入 onnxruntime。

    mask = Image.fromarray(alpha, "L").filter(ImageFilter.GaussianBlur(EDGE_FEATHER))

    out = image.convert("RGBA")
    out.putalpha(mask)

    bbox = out.getbbox()
    if bbox:
        pad = 6
        bbox = (
            max(0, bbox[0] - pad),
            max(0, bbox[1] - pad),
            min(w, bbox[2] + pad),
            min(h, bbox[3] + pad),
        )
        out = out.crop(bbox)

    scale = width / out.width
    out = out.resize((width, max(1, round(out.height * scale))), Image.LANCZOS)
    dst.parent.mkdir(parents=True, exist_ok=True)
    out.save(dst, "PNG", optimize=True)
    return dst


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SRC
    if not src.exists():
        raise SystemExit(f"找不到原图：{src}")
    dst = DEFAULT_DST if src == DEFAULT_SRC else src.with_suffix(".png")
    out = cut_out(src, dst)
    with Image.open(out) as im:
        alpha = np.asarray(im.getchannel("A"))
        ratio = float((alpha > 128).mean())
        print(f"已生成 {out.relative_to(ROOT)}  {im.width}x{im.height}  不透明像素占比 {ratio:.1%}")


if __name__ == "__main__":
    main()
