#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静态校验 Alas Maa 的 PI V2 资源包。

不依赖 MaaFramework 与 Android 工具链，只做结构层面的检查，用来在真机标定前
拦住「入口不存在 / next 指向不存在的节点 / option 引用打错字 / ROI 超出画面」这类错误。

用法:
    python3 tools/validate_pi.py            # 校验并打印锚点表
    python3 tools/validate_pi.py --json     # 只输出 JSON 报告
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def find_pi_root() -> Path:
    """interface.json 可能在仓库根，也可能在 resources/ 下（alas-maa 是后者）。"""
    for cand in (ROOT, ROOT / "resources"):
        if (cand / "interface.json").is_file():
            return cand
    return ROOT


PI_ROOT = find_pi_root()
BASE_W, BASE_H = 1280, 720

RECOGNITIONS = {
    "DirectHit", "TemplateMatch", "FeatureMatch", "ColorMatch", "OCR",
    "NeuralNetworkClassify", "NeuralNetworkDetect", "And", "Or", "Custom",
}
ACTIONS = {
    "DoNothing", "Click", "LongPress", "Swipe", "MultiSwipe", "Scroll",
    "ClickKey", "LongPressKey", "KeyDown", "KeyUp", "InputText", "StartApp",
    "StopApp", "StopTask", "Command", "Shell", "Screencap", "Custom",
    "TouchDown", "TouchMove", "TouchUp",
}

errors: list[str] = []
warnings: list[str] = []
anchors: list[dict] = []


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        errors.append(f"{path}: JSON 解析失败: {exc}")
        return {}


def node_kind(node: dict) -> tuple[str, dict]:
    """兼容 v1（字符串）与 v2（{type,param}）两种写法。"""
    rec = node.get("recognition", "DirectHit")
    act = node.get("action", "DoNothing")
    if isinstance(rec, dict):
        rec_type, rec_param = rec.get("type", "DirectHit"), rec.get("param", {}) or {}
    else:
        rec_type, rec_param = rec, node
    if isinstance(act, dict):
        act_type, act_param = act.get("type", "DoNothing"), act.get("param", {}) or {}
    else:
        act_type, act_param = act, node
    return rec_type, act_type, rec_param, act_param


def check_rect(name: str, field: str, value) -> None:
    if not isinstance(value, list):
        return
    if len(value) == 4:
        x, y, w, h = value
        if w <= 0 or h <= 0:
            errors.append(f"{name}.{field}: 宽高必须为正，得到 {value}")
        if x < 0 or y < 0 or x + w > BASE_W or y + h > BASE_H:
            warnings.append(
                f"{name}.{field}: {value} 超出 {BASE_W}x{BASE_H} 画面，确认是按 720P 标的吗？"
            )
    elif len(value) == 2:
        x, y = value
        if not (0 <= x <= BASE_W and 0 <= y <= BASE_H):
            warnings.append(f"{name}.{field}: 点 {value} 超出 {BASE_W}x{BASE_H} 画面")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    interface_path = PI_ROOT / "interface.json"
    interface = load_json(interface_path)

    # ---- interface 基本字段 ----
    if interface.get("interface_version") != 2:
        errors.append("interface.json: interface_version 必须为 2")
    for field in ("name", "version"):
        if not interface.get(field):
            errors.append(f"interface.json: 缺少 {field}")

    controllers = {c.get("name") for c in interface.get("controller", [])}
    resources = {r.get("name"): r for r in interface.get("resource", [])}
    if not resources:
        errors.append("interface.json: 至少需要一个 resource")

    # ---- 收集任务与 option ----
    tasks: list[dict] = list(interface.get("task", []))
    options: dict[str, dict] = dict(interface.get("option", {}))
    for imp in interface.get("import", []):
        frag = load_json(PI_ROOT / imp)
        tasks.extend(frag.get("task", []))
        for key, value in frag.get("option", {}).items():
            options.setdefault(key, value)

    if not tasks:
        errors.append("没有声明任何 task")

    # ---- 收集 pipeline 节点 ----
    nodes: dict[str, dict] = {}
    origin: dict[str, str] = {}
    for res in resources.values():
        for rel in res.get("path", []):
            bundle = PI_ROOT / rel
            if not bundle.is_dir():
                errors.append(f"resource.path 指向的目录不存在: {rel}")
                continue
            for jf in sorted((bundle / "pipeline").rglob("*.json")):
                for name, node in load_json(jf).items():
                    if name.startswith("$"):
                        continue
                    if name in nodes:
                        errors.append(
                            f"节点重名: {name}（{origin[name]} 与 {jf.relative_to(ROOT)}）"
                        )
                    nodes[name] = node
                    origin[name] = str(jf)

    if not nodes:
        errors.append("没有找到任何 pipeline 节点")

    # ---- 节点内部检查 ----
    for name, node in nodes.items():
        rec_type, act_type, rec_param, act_param = node_kind(node)
        if rec_type not in RECOGNITIONS:
            errors.append(f"{name}: 未知识别类型 {rec_type}")
        if act_type not in ACTIONS:
            errors.append(f"{name}: 未知动作类型 {act_type}")

        for ref_field in ("next", "on_error", "interrupt"):
            value = node.get(ref_field)
            if value is None:
                continue
            refs = [value] if isinstance(value, str) else value
            for ref in refs:
                if isinstance(ref, dict):  # NodeAttr 形式
                    ref = ref.get("name", "")
                if not isinstance(ref, str):
                    continue
                if ref.startswith("[Anchor]") or ref.startswith("[JumpBack]"):
                    continue
                if ref and ref not in nodes:
                    errors.append(f"{name}.{ref_field}: 引用了不存在的节点 {ref}")

        for field in ("roi", "roi_offset", "target", "target_offset", "begin", "end"):
            if field in rec_param:
                check_rect(name, f"recognition.{field}", rec_param[field])
            if field in act_param:
                check_rect(name, f"action.{field}", act_param[field])

        if act_type == "StartApp" and not act_param.get("package"):
            errors.append(f"{name}: StartApp 缺少 package（可能要靠 option 覆盖，确认一下）")
        if act_type == "ClickKey" and "key" not in act_param:
            errors.append(f"{name}: ClickKey 缺少 key")

        if rec_type == "OCR":
            expected = rec_param.get("expected")
            if isinstance(expected, str):
                expected = [expected]
            if expected:
                anchors.append(
                    {
                        "node": name,
                        "expected": expected,
                        "roi": rec_param.get("roi", [0, 0, BASE_W, BASE_H]),
                        "file": origin[name],
                    }
                )
            for pattern in expected or []:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    warnings.append(f"{name}: expected 不是合法正则 {pattern!r}: {exc}")

    # ---- task 检查 ----
    for task in tasks:
        tname = task.get("name", "<未命名>")
        entry = task.get("entry")
        if entry not in nodes:
            errors.append(f"task {tname}: entry {entry} 不存在")
        for opt in task.get("option", []):
            if opt not in options:
                errors.append(f"task {tname}: 引用了未定义的 option {opt}")
        for node_name, override in (task.get("pipeline_override") or {}).items():
            if node_name not in nodes:
                errors.append(f"task {tname}: pipeline_override 指向不存在的节点 {node_name}")
            for ref_field in ("next", "on_error"):
                refs = override.get(ref_field)
                if refs is None:
                    continue
                refs = [refs] if isinstance(refs, str) else refs
                for ref in refs:
                    if isinstance(ref, str) and ref not in nodes:
                        errors.append(
                            f"task {tname}: pipeline_override.{node_name}.{ref_field} "
                            f"引用了不存在的节点 {ref}"
                        )

    # ---- option 检查 ----
    for key, opt in options.items():
        for case in opt.get("cases", []):
            for node_name, override in (case.get("pipeline_override") or {}).items():
                if node_name not in nodes:
                    errors.append(f"option {key}/case {case.get('name')}: 覆盖不存在的节点 {node_name}")

    # ---- 输出 ----
    report = {
        "errors": errors,
        "warnings": warnings,
        "tasks": [t.get("name") for t in tasks],
        "nodes": len(nodes),
        "anchors": anchors,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"任务 {len(tasks)} 个 | 节点 {len(nodes)} 个 | OCR 锚点 {len(anchors)} 个")
        print(f"画布 {BASE_W}x{BASE_H}（controller.display_short_side=720）")
        print("\n== 需要在真机上确认的 OCR 锚点 ==")
        for a in anchors:
            print(f"  · {a['node']:<24} {a['expected']}  roi={a['roi']}")
        if warnings:
            print("\n== 警告 ==")
            for w in warnings:
                print(f"  ! {w}")
        if errors:
            print("\n== 错误 ==")
            for e in errors:
                print(f"  x {e}")
        else:
            print("\n结构校验通过。")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
