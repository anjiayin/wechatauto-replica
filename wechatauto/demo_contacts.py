# -*- coding: utf-8 -*-
"""wechatauto 复刻版 —— 联系人列表示例程序

演示 get_contacts() 接口：按类别读取本地 contact.db 中的联系人，
不依赖 UI 自动化。

类别说明：
    friend   好友（通讯录好友 + 仅聊天联系人，排除公众号/系统号/群聊/自己）
    chatroom 群聊
    official 公众号/服务号
    openim   企业微信/客服联系人
    all      全部（含群内非好友等，可能数万条）

使用前提：
    1. 微信 4.x 已登录运行（首次运行时需要从进程内存提取密钥）
    2. 已安装依赖：pip install -e .

用法：
    python demo_contacts.py               # 交互模式
    python demo_contacts.py --export out.csv   # 导出好友 CSV
"""

from __future__ import annotations

import os
import sys
import time

try:
    os.system("chcp 65001 >nul 2>&1")
except Exception:
    pass
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

from wechatauto import WeChatDB

KINDS = ("friend", "chatroom", "official", "openim", "all")


def disp(c: dict) -> str:
    """显示名：备注 > 昵称 > 微信号"""
    return c.get("remark") or c.get("nick_name") or c.get("username") or ""


def show_summary(db: WeChatDB):
    print("-" * 60)
    print("联系人统计")
    print("-" * 60)
    for kind in KINDS:
        t0 = time.time()
        contacts = db.get_contacts(kind)
        dt = time.time() - t0
        print(f"  {kind:<9} {len(contacts):>6} 条（{dt:.2f}s）")


def show_list(contacts: list, kind: str, limit: int = 10):
    print()
    print("-" * 60)
    print(f"{kind} 列表（前 {min(limit, len(contacts))} / {len(contacts)} 条）")
    print("-" * 60)
    for c in contacts[:limit]:
        alias = f"（微信号: {c['alias']}）" if c.get("alias") else ""
        print(f"  {disp(c):<24} {c['username']:<32} {alias}")


def export_csv(contacts: list, out_path: str):
    """导出联系人为 CSV（Excel 可直接打开，UTF-8 BOM 编码）"""
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        f.write("username,alias,nick_name,remark,description,local_type\n")
        for c in contacts:
            fields = [
                c.get("username") or "",
                c.get("alias") or "",
                c.get("nick_name") or "",
                c.get("remark") or "",
                (c.get("description") or "").replace("\n", " "),
                str(c.get("local_type", "")),
            ]
            f.write(",".join('"%s"' % x.replace('"', '""') for x in fields) + "\n")
    print(f"已导出 {len(contacts)} 条到 {out_path}")


def main():
    export_path = None
    if "--export" in sys.argv:
        i = sys.argv.index("--export")
        if i + 1 < len(sys.argv):
            export_path = sys.argv[i + 1]

    print("正在初始化微信数据库读取器 ...")
    t0 = time.time()
    db = WeChatDB()
    print(f"初始化完成（{time.time() - t0:.1f}s，账号目录：{db.account}）")

    info = db.get_self_info()
    print(f"当前账号：{info.get('nick_name')}（{info.get('username')}）")

    show_summary(db)

    friends = db.get_contacts("friend")
    # 预览按显示名排序（默认 username 排序会让数字并头的企微号排在最前）
    show_list(sorted(friends, key=disp), "好友")

    if export_path:
        export_csv(friends, export_path)
        return

    # 交互：按关键字过滤好友（备注/昵称/微信号）
    while True:
        kw = input("\n输入关键字过滤好友（export=导出CSV，回车退出）：").strip()
        if not kw:
            break
        if kw.lower() in ("export", "csv"):
            out = os.path.abspath("contacts.csv")
            export_csv(friends, out)
            continue
        hits = [c for c in friends if kw in disp(c) or kw in (c.get("username") or "")
                or kw in (c.get("alias") or "")]
        show_list(hits, f"好友 ∩ {kw!r}", limit=30)
        if not hits:
            print("  （没有匹配的联系人）")


if __name__ == "__main__":
    main()
