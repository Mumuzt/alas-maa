#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""maacli —— 在容器里直接看设备画面、按坐标操作设备，用来给 Alas Maa 标定 Pipeline。

它把两件事接起来：
  1. DSHA 桥的 `/app/ui/screenshot`、`/app/ui/tap`、`/app/ui/swipe`（无障碍，display 0）
  2. 容器的 `/sdcard` 挂载（能直接读宿主导出的 PNG）

所有坐标默认是 **1280x720 的 PI 画布坐标**，CLI 自己换算成设备像素。
这样看到的画面、点的位置、写进 Pipeline 的数字是同一套坐标系。

用法:
    python3 tools/maacli.py status
    python3 tools/maacli.py shot --name harbor          # 截图 → captures/
    python3 tools/maacli.py norm captures/xxx.png       # 裁掉黑边、缩到 1280x720
    python3 tools/maacli.py grid captures/xxx.png       # 画 720 坐标系网格，方便读数
    python3 tools/maacli.py tap 640 600                 # 点 720 坐标 (640,600)
    python3 tools/maacli.py swipe 640 600 640 200
    python3 tools/maacli.py key back
    python3 tools/maacli.py launch com.bilibili.azurlane
    python3 tools/maacli.py crop captures/xxx.png 40 640 120 48 CommissionBtn
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent


def find_pi_root() -> Path:
    for cand in (ROOT, ROOT / "resources"):
        if (cand / "interface.json").is_file():
            return cand
    return ROOT


PI_ROOT = find_pi_root()
CAPTURES = ROOT / "captures"
BRIDGE = "http://127.0.0.1:3090"
HEADERS = "/root/.dsh/.bridge_headers"

CANVAS_W, CANVAS_H = 1280, 720


# ---------------------------------------------------------------- bridge
def bridge(path: str, **params) -> str:
    cmd = ["curl", "-s", "-H", f"@{HEADERS}"]
    if params:
        cmd += ["-G", f"{BRIDGE}{path}"]
        for key, value in params.items():
            cmd += ["--data-urlencode", f"{key}={value}"]
    else:
        cmd += [f"{BRIDGE}{path}"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if out.returncode != 0:
        raise SystemExit(f"桥调用失败: {out.stderr.strip()}")
    payload = json.loads(out.stdout)
    result = payload.get("result", "")
    return result


def bridge_json(path: str, **params) -> dict:
    raw = bridge(path, **params)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise SystemExit(f"桥返回的不是 JSON:\n{raw[:400]}")


# ---------------------------------------------------------------- 截图
def take_shot(tag: str = "shot") -> tuple[Path, dict]:
    meta = bridge_json("/app/ui/screenshot")
    if not meta.get("ok"):
        raise SystemExit(f"截图失败: {meta}")
    src = Path(meta["path"])
    if not src.exists():
        raise SystemExit(f"桥返回的路径读不到: {src}")
    CAPTURES.mkdir(exist_ok=True)
    stamp = time.strftime("%H%M%S")
    dst = CAPTURES / f"{stamp}-{tag}.png"
    dst.write_bytes(src.read_bytes())
    return dst, meta


def content_box(img: Image.Image, tol: int = 12) -> tuple[int, int, int, int]:
    """找画面里的非黑边区域，返回 (x, y, w, h)。找不到黑边就返回整幅。"""
    rgb = img.convert("RGB")
    w, h = rgb.size
    px = rgb.load()

    def dark(column: list[tuple[int, int, int]]) -> bool:
        return all(max(p) <= tol for p in column[::8]) if column else True

    left = 0
    while left < w and dark([px[left, y] for y in range(0, h, 4)]):
        left += 1
    right = w - 1
    while right > left and dark([px[right, y] for y in range(0, h, 4)]):
        right -= 1
    top = 0
    while top < h and dark([px[x, top] for x in range(0, w, 4)]):
        top += 1
    bottom = h - 1
    while bottom > top and dark([px[x, bottom] for x in range(0, w, 4)]):
        bottom -= 1
    if right - left < w * 0.4 or bottom - top < h * 0.4:
        return 0, 0, w, h
    return left, top, right - left + 1, bottom - top + 1


def normalize(img: Image.Image) -> tuple[Image.Image, dict]:
    bx, by, bw, bh = content_box(img)
    canvas = img.convert("RGB").crop((bx, by, bx + bw, by + bh)).resize(
        (CANVAS_W, CANVAS_H), Image.LANCZOS
    )
    meta = {
        "device": {"width": img.width, "height": img.height},
        "content": {"x": bx, "y": by, "w": bw, "h": bh},
        "canvas": {"width": CANVAS_W, "height": CANVAS_H},
        "toDeviceX": bw / CANVAS_W,
        "toDeviceY": bh / CANVAS_H,
        "letterboxed": (bw, bh) != (img.width, img.height),
    }
    return canvas, meta


def ocr_text(img: Image.Image, psm: int = 11) -> str:
    """用 tesseract 识别一张图（chi_sim + eng），返回纯文本。"""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        img.convert("RGB").save(tmp.name)
        path = tmp.name
    out = subprocess.run(
        ["tesseract", path, "stdout", "-l", "chi_sim+eng", "--psm", str(psm)],
        capture_output=True, text=True, timeout=120,
    )
    Path(path).unlink(missing_ok=True)
    return out.stdout.strip()


def roi_crop(canvas: Image.Image, roi: list[int]) -> Image.Image:
    x, y, w, h = roi
    box = (max(0, x), max(0, y), min(CANVAS_W, x + w), min(CANVAS_H, y + h))
    crop = canvas.crop(box)
    if crop.width < 600:
        crop = crop.resize((crop.width * 2, crop.height * 2), Image.LANCZOS)
    return crop



def to_device(x: float, y: float, meta: dict) -> tuple[int, int]:
    c = meta["content"]
    dx = c["x"] + x * c["w"] / CANVAS_W
    dy = c["y"] + y * c["h"] / CANVAS_H
    return int(round(dx)), int(round(dy))


# ---------------------------------------------------------------- 命令
def cmd_status(_args) -> int:
    print(bridge("/app/device").strip())
    shot, meta = take_shot("status")
    canvas, mapping = normalize(Image.open(shot))
    print(f"\n最近截图: {shot.relative_to(ROOT)}  {meta['width']}x{meta['height']}")
    print(f"内容区: {mapping['content']}  黑边={mapping['letterboxed']}")
    print(f"坐标可用: {meta.get('coordinatesAvailable')}  observation={meta.get('observationId')}")
    return 0


def cmd_shot(args) -> int:
    shot, meta = take_shot(args.name)
    img = Image.open(shot)
    canvas, mapping = normalize(img)
    out = CAPTURES / f"{shot.stem}-720.png"
    canvas.save(out)
    print(f"原图: {shot.relative_to(ROOT)}  {img.width}x{img.height}")
    print(f"720: {out.relative_to(ROOT)}  内容区={mapping['content']}  黑边={mapping['letterboxed']}")
    print(f"observation={meta.get('observationId')}  坐标可用={meta.get('coordinatesAvailable')}")
    return 0


def cmd_norm(args) -> int:
    src = Path(args.image)
    canvas, mapping = normalize(Image.open(src))
    out = Path(args.out) if args.out else src.with_name(src.stem + "-720.png")
    canvas.save(out)
    print(json.dumps(mapping, ensure_ascii=False, indent=2))
    print(f"已写入 {out}")
    return 0


def cmd_grid(args) -> int:
    src = Path(args.image)
    src_img = Image.open(src)
    canvas, mapping = normalize(src_img)
    draw = ImageDraw.Draw(canvas)
    step = args.step
    for x in range(0, CANVAS_W, step):
        draw.line([(x, 0), (x, CANVAS_H)], fill=(255, 0, 0, 128), width=1)
        draw.text((x + 2, 2), str(x), fill=(255, 0, 0))
    for y in range(0, CANVAS_H, step):
        draw.line([(0, y), (CANVAS_W, y)], fill=(0, 160, 255, 128), width=1)
        draw.text((2, y + 1), str(y), fill=(0, 160, 255))
    out = CAPTURES / f"{src.stem}-grid.png" if not args.out else Path(args.out)
    canvas.save(out)
    print(f"网格图: {out}  步长={step}  内容区={mapping['content']}")
    return 0


def cmd_tap(args) -> int:
    shot, meta = take_shot("tap")
    _, mapping = normalize(Image.open(shot))
    dx, dy = to_device(args.x, args.y, mapping)
    obs = meta.get("observationId")
    if not obs:
        raise SystemExit(f"这张截图没有 coordinate 凭据（{meta.get('reason')}），无法按坐标点")
    res = bridge("/app/ui/tap", observationId=obs, x=dx, y=dy)
    print(f"tap 720=({args.x},{args.y}) → device=({dx},{dy})  {res[:200]}")
    return 0


def cmd_swipe(args) -> int:
    shot, meta = take_shot("swipe")
    _, mapping = normalize(Image.open(shot))
    x1, y1 = to_device(args.x1, args.y1, mapping)
    x2, y2 = to_device(args.x2, args.y2, mapping)
    obs = meta.get("observationId")
    if not obs:
        raise SystemExit("这张截图没有 coordinate 凭据，无法滑动")
    res = bridge("/app/ui/swipe", observationId=obs, x1=x1, y1=y1, x2=x2, y2=y2, ms=args.ms)
    print(f"swipe 720=({args.x1},{args.y1})→({args.x2},{args.y2}) device=({x1},{y1})→({x2},{y2})  {res[:200]}")
    return 0


def cmd_key(args) -> int:
    print(bridge("/app/ui/key", name=args.name)[:200])
    return 0


def cmd_launch(args) -> int:
    print(bridge("/app/launch", pkg=args.package)[:300])
    return 0


def cmd_ocr(args) -> int:
    src = Path(args.image)
    canvas, _ = normalize(Image.open(src))
    if args.roi:
        canvas = roi_crop(canvas, [int(v) for v in args.roi.split(",")])
    print(ocr_text(canvas, psm=args.psm) or "（没识别到文字）")
    return 0


def load_ocr_nodes() -> list[dict]:
    nodes = []
    for jf in sorted((PI_ROOT / "resource").rglob("pipeline/*.json")):
        for name, node in json.loads(jf.read_text(encoding="utf-8")).items():
            rec = node.get("recognition", "DirectHit")
            if isinstance(rec, dict):
                if rec.get("type") != "OCR":
                    continue
                param = rec.get("param", {})
            else:
                if rec != "OCR":
                    continue
                param = node
            expected = param.get("expected")
            if not expected:
                continue
            if isinstance(expected, str):
                expected = [expected]
            nodes.append({
                "node": name,
                "expected": expected,
                "roi": param.get("roi", [0, 0, CANVAS_W, CANVAS_H]),
            })
    return nodes


def cmd_check(args) -> int:
    canvas, _ = normalize(Image.open(args.image))
    nodes = load_ocr_nodes()
    passed = failed = 0
    print(f"共 {len(nodes)} 个 OCR 节点，阈值只做「文本是否出现」的粗判，最终以 MaaFramework 的 OCR 为准。\n")
    for item in nodes:
        crop = roi_crop(canvas, item["roi"])
        text = ocr_text(crop, psm=args.psm)
        hit = [p for p in item["expected"] if re.search(p, text)]
        ok = bool(hit)
        passed += ok
        failed += (not ok)
        mark = "PASS" if ok else "FAIL"
        seen = " | ".join(text.splitlines())[:60]
        print(f"[{mark}] {item['node']:<24} 期望={item['expected']}  识别={seen!r}")
    print(f"\n通过 {passed} / 失败 {failed}")
    return 0 if failed == 0 else 1


def cmd_crop(args) -> int:
    src = Path(args.image)
    canvas, mapping = normalize(Image.open(src))
    box = (args.x, args.y, args.x + args.w, args.y + args.h)
    crop = canvas.crop(box).convert("RGB")
    out_dir = PI_ROOT / "resource" / "base" / "image"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{args.name}.png"
    crop.save(out)
    print(f"模板: {out}  {crop.width}x{crop.height}")
    print(json.dumps({
        args.name: {
            "recognition": {
                "type": "TemplateMatch",
                "param": {
                    "template": f"image/{args.name}.png",
                    "roi": [args.x, args.y, args.w, args.h],
                    "threshold": 0.8,
                },
            },
            "action": {"type": "Click", "param": {"target": True}},
        }
    }, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="设备画面 / 坐标操作 CLI（1280x720 坐标系）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(func=cmd_status)

    p = sub.add_parser("shot")
    p.add_argument("--name", default="shot")
    p.set_defaults(func=cmd_shot)

    p = sub.add_parser("norm")
    p.add_argument("image")
    p.add_argument("--out")
    p.set_defaults(func=cmd_norm)

    p = sub.add_parser("grid")
    p.add_argument("image")
    p.add_argument("--step", type=int, default=80)
    p.add_argument("--out")
    p.set_defaults(func=cmd_grid)

    p = sub.add_parser("tap")
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)
    p.set_defaults(func=cmd_tap)

    p = sub.add_parser("swipe")
    p.add_argument("x1", type=int)
    p.add_argument("y1", type=int)
    p.add_argument("x2", type=int)
    p.add_argument("y2", type=int)
    p.add_argument("--ms", type=int, default=400)
    p.set_defaults(func=cmd_swipe)

    p = sub.add_parser("key")
    p.add_argument("name", choices=["back", "home", "recents", "notifications", "quicksettings"])
    p.set_defaults(func=cmd_key)

    p = sub.add_parser("launch")
    p.add_argument("package")
    p.set_defaults(func=cmd_launch)

    p = sub.add_parser("ocr")
    p.add_argument("image")
    p.add_argument("--roi", help="x,y,w,h（720 坐标）")
    p.add_argument("--psm", type=int, default=11)
    p.set_defaults(func=cmd_ocr)

    p = sub.add_parser("check")
    p.add_argument("image")
    p.add_argument("--psm", type=int, default=11)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("crop")
    p.add_argument("image")
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)
    p.add_argument("w", type=int)
    p.add_argument("h", type=int)
    p.add_argument("name")
    p.set_defaults(func=cmd_crop)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
