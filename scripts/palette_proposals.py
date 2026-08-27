"""生成候选方案色板数据（OKLCH 等距 10 色 + 主题亮度阶梯），供调研文档与 HTML mockup 引用。

- 方案 B-1: OKLCH 等距环（等 L/C，色相均布）—— 感知均匀
- 方案 B-2: Tol Muted 10 色（色盲安全，权威引用）
- 主题阶梯：画布/网格/节点体/标题栏/边框/选中
"""
import json

from coloraide import Color

def hex_of(l, c, h):
    try:
        return Color("oklch", [l, c, h % 360]).convert("srgb").to_string(hex=True, upper=True)
    except Exception:
        return "?"

def de(a, b):
    return Color(a).delta_e(b, method="ok")

print("== 方案 B-1: OKLCH 等距环 L=0.74 C=0.155 ==")
pal1 = {}
for k in range(10):
    h = 12 + k * 36
    pal1[f"H{k}"] = hex_of(0.74, 0.155, h)
    print(f"  H{k:2d} ({h:3d}°) -> {pal1[f'H{k}']}")
pairs = sorted((de(pal1[a], pal1[b]), a, b) for i, a in enumerate(pal1) for b in list(pal1)[i+1:])
print("  最近邻对:", [(round(d,3), a, b) for d, a, b in pairs[:3]])

print("\n== 方案 B-2: Tol Muted（色盲安全 10 色） ==")
tol = {
    "靛蓝": "#332288", "青": "#88CCEE", "青绿": "#44AA99", "绿": "#117733",
    "橄榄": "#999933", "沙": "#DDCC77", "玫瑰": "#CC6677", "酒红": "#882255",
    "紫": "#AA4499", "浅灰": "#DDDDDD",
}
for k, (n, v) in enumerate(tol.items()):
    print(f"  {n:4s} {v}")
pairs = sorted((de(tol[a], tol[b]), a, b) for i, a in enumerate(tol) for b in list(tol)[i+1:])
print("  最近邻对:", [(round(d,3), a, b) for d, a, b in pairs[:3]])

print("\n== 主题亮度阶梯（深色节点图） ==")
steps = {
    "canvas":  "#191919",   # 画布（比现状 #232323 更沉）
    "grid":    "#2A2A2A",   # 网格线
    "node":    "#313131",   # 节点体（比画布亮一档）
    "title":   "#3A3A3A",   # 标题栏（再亮一档）
    "border":  "#4E4E4E",   # 节点边框
    "select":  "#59C2FF",   # 选中边框（天蓝，避开所有分类色相）
    "text":    "#E8E8E8",
}
for k, v in steps.items():
    print(f"  {k:8s} {v}")
print("  画布 vs 节点对比:", round(Color(steps['canvas']).contrast(steps['node']), 2))
print("  画布 vs 网格对比:", round(Color(steps['canvas']).contrast(steps['grid']), 2))
print("  节点 vs 边框对比:", round(Color(steps['node']).contrast(steps['border']), 2))
print("  节点 vs 选中对比:", round(Color(steps['node']).contrast(steps['select']), 2))
print("  文字 vs 节点体:", round(Color(steps['text']).contrast(steps['node']), 2))
# 选中色与所有分类色的距离
print("\n  选中 #59C2FF vs 方案 B-1 各色 ΔEOK:")
for k, v in pal1.items():
    print(f"    {k} {v}: {round(de(steps['select'], v), 3)}")
