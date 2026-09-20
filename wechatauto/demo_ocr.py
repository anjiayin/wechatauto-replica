# -*- coding: utf-8 -*-
"""wechatauto 复刻版 —— OCR 图片文字识别示例程序

演示 ScreenOCR 工具：调用 Windows 系统自带 OCR（WinRT Windows.Media.Ocr）
识别本地图片中的文字，并显示识别结果及位置信息。

使用前提：
    1. Windows 10/11 系统（需安装中文语言包以识别中文）
    2. 已安装依赖：pip install -e .

用法：
    python demo_ocr.py                    # 弹出文件选择对话框
    python demo_ocr.py path/to/image.png  # 直接指定图片路径
"""

from __future__ import annotations

import os
import sys

try:
    os.system("chcp 65001 >nul 2>&1")
except Exception:
    pass
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

from PIL import Image
from wechatauto.guia import ScreenOCR


SUPPORTED_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}


def pick_image() -> str | None:
    """弹出系统文件选择对话框，返回选中图片的路径。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        ext_list = " ".join(f"*{e}" for e in SUPPORTED_EXT)
        path = filedialog.askopenfilename(
            title="选择要识别的图片",
            filetypes=[("图片文件", ext_list), ("所有文件", "*.*")],
        )
        root.destroy()
        return path or None
    except Exception:
        return None


def run_ocr(image_path: str) -> None:
    """对指定图片执行 OCR 识别并打印结果。"""
    if not os.path.isfile(image_path):
        print(f"[错误] 文件不存在：{image_path}")
        return

    ext = os.path.splitext(image_path)[1].lower()
    if ext not in SUPPORTED_EXT:
        print(f"[警告] 不支持的图片格式 '{ext}'，尝试加载...")

    print(f"\n图片路径：{image_path}")
    print(f"文件大小：{os.path.getsize(image_path) / 1024:.1f} KB")

    try:
        img = Image.open(image_path)
    except Exception as e:
        print(f"[错误] 无法打开图片：{e}")
        return

    w, h = img.size
    print(f"图片尺寸：{w} x {h} px")
    print("-" * 50)
    print("正在识别（最长 8 秒）...")

    results = ScreenOCR.recognize(img)

    if not results:
        print("\n未识别到任何文字。可能原因：")
        print("  - 图片中无可识别文字")
        print("  - Windows 未安装对应语言包（中文需在系统设置中添加中文语言包）")
        print("  - OCR 超时（8 秒）")
        return

    print(f"\n共识别到 {len(results)} 段文字：\n")
    print(f"  {'序号':<4}  {'位置 (x,y,w,h)':<24}  识别文字")
    print(f"  {'----':<4}  {'----------------------':<24}  --------")
    for i, (text, x, y, bw, bh) in enumerate(results, 1):
        pos = f"({x}, {y}, {bw}, {bh})"
        print(f"  {i:<4}  {pos:<24}  {text}")

    print("-" * 50)
    full_text = "\n".join(text for text, *_ in results)
    print(f"\n【完整文字】\n{full_text}")


def main() -> None:
    print("=" * 50)
    print("  wechatauto OCR 图片文字识别 Demo")
    print("=" * 50)

    if len(sys.argv) > 1:
        image_path = sys.argv[1]
    else:
        print("\n未指定图片路径，正在打开文件选择对话框...")
        image_path = pick_image()
        if not image_path:
            print("未选择文件，退出。")
            return

    run_ocr(image_path)


if __name__ == "__main__":
    main()
