"""量化分析当前分类色板：OKLCH 明度/彩度分布 + OKLab 两两色距 + 关键对比度。

供设计调研文档引用（docs/research/color-theme-design.md 的数据来源）。
"""
import sys

from coloraide import Color
from coloraide.distance import DeltaE

CURRENT = {
    "INPUT 输入": "#66BB6A",
    "PREFORMAT 预格式化": "#26C6DA",
    "FORMAT 格式化": "#4DB6AC",
    "SEQUENCE 序列处理": "#AB47BC",
    "PROCESS 一般处理": "#42A5F5",
    "MOTION 动效处理": "#FFA726",
    "CHANNEL 通道处理": "#EC407A",
    "OUTPUT 输出": "#EF5350",
    "ANALYSIS 分析": "#FFCA28",
    "BACKDROP 背景": "#90A4AE",
}

# 参考色板
OKABE_ITO = {
    "蓝": "#0072B2",
    "橙": "#E69F00",
    "青": "#56B4E9",
    "绿": "#009E73",
    "黄": "#F0E442",
    "朱红": "#D55E00",
    "紫红": "#CC79A7",
    "黑": "#000000",
}

TOL_MUTED = {
    "靛蓝": "#332288",
    "青": "#88CCEE",
    "青绿": "#44AA99",
    "绿": "#117733",
    "橄榄": "#999933",
    "沙": "#DDCC77",
    "玫瑰": "#CC6677",
    "酒红": "#882255",
    "紫": "#AA4499",
    "浅灰": "#DDDDDD",
}

CANVAS = "#232323"   # NodeGraphQt 默认画布
BODY = "#232323"     # NodeGraphQt 默认节点体
SELECT = "#FECF2A"   # NodeGraphQt 默认选中边框


def oklch(hexc):
    c = Color(hexc)
    o = c.convert("oklch")
    return o["lightness"], o["chroma"], o["hue"]


def de(a, b):
    return Color(a).delta_e(b, method="ok")


def contrast(a, b):
    return Color(a).contrast(b)


print("== 当前 10 分类 OKLCH (L, C, H) ==")
for name, hexc in CURRENT.items():
    L, C, H = oklch(hexc)
    print(f"  {name:22s} {hexc}  L={L:.3f} C={C:.3f} H={H:6.1f}")

print("\n== 当前色板两两 ΔE(OK) 最小 5 对（感知最接近） ==")
pairs = []
names = list(CURRENT)
for i in range(len(names)):
    for j in range(i + 1, len(names)):
        pairs.append((de(CURRENT[names[i]], CURRENT[names[j]]), names[i], names[j]))
for d, a, b in sorted(pairs)[:5]:
    print(f"  {d:6.2f}  {a}  ↔  {b}")

print("\n== 参考色板两两 ΔE(OK) 最小 3 对 ==")
for label, pal in (("Okabe-Ito", OKABE_ITO), ("Tol Muted", TOL_MUTED)):
    names = list(pal)
    ps = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            ps.append((de(pal[names[i]], pal[names[j]]), names[i], names[j]))
    for d, a, b in sorted(ps)[:3]:
        print(f"  {label}: {d:6.2f}  {a} ↔ {b}")

print("\n== 对比度（WCAG） ==")
print(f"  节点体 {BODY} vs 画布 {CANVAS}: {contrast(BODY, CANVAS):.2f}:1")
print(f"  节点体 {BODY} vs 选中边框 {SELECT}: {contrast(BODY, SELECT):.2f}:1")
print(f"  画布 {CANVAS} vs 网格 #2D2D2D: {contrast(CANVAS, '#2D2D2D'):.2f}:1")
print(f"  ANALYSIS amber {CURRENT['ANALYSIS 分析']} vs 选中 {SELECT}: {contrast(CURRENT['ANALYSIS 分析'], SELECT):.2f}:1  (撞色)")
print(f"  白字 on 节点体 {BODY}: {contrast('#FFFFFF', BODY):.2f}:1")

print("\n== 明度方差（感知均匀度，越小越均匀） ==")
import statistics
for label, pal in (("当前", CURRENT), ("Okabe-Ito", OKABE_ITO), ("Tol Muted", TOL_MUTED)):
    ls = [oklch(v)[0] for v in pal.values()]
    print(f"  {label:10s} L 范围 {min(ls):.3f}~{max(ls):.3f}  标准差 {statistics.pstdev(ls):.4f}")
