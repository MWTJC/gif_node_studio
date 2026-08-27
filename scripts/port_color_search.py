"""方案 C：搜索与 10 个分类锚点感知距离最大的两个端口色（MANIFEST/SEQUENCE）。

约束：
- 两端口色之间也要拉开（ΔEOK 尽量大）
- 与全部分类锚点的最小 ΔEOK 尽量大（避免撞分类）
- 暖色（素材流 MANIFEST）vs 冷色（序列流 SEQUENCE）语义：色相偏好 20-70° / 190-270°
"""
from coloraide import Color

ANCHORS = [
    "#FC7D91", "#F8894B", "#D8A100", "#41C780", "#00C4BF",
    "#00BBF2", "#7FA9FF", "#BA92FD", "#E682D0", "#92A9B4",
]

def hex_of(l, c, h):
    return Color("oklch", [l, c, h % 360]).convert("srgb").to_string(hex=True, upper=True)

def de(a, b):
    return Color(a).delta_e(b, method="ok")

best = None
best_score = -1
for l in (0.62, 0.66, 0.70, 0.74):
    for c in (0.12, 0.15, 0.18):
        for h1 in range(20, 75, 5):
            for h2 in range(190, 275, 5):
                a = hex_of(l, c, h1)
                b = hex_of(l, c, h2)
                min_cat = min(de(a, x) for x in ANCHORS + [b])
                min_cat2 = min(de(b, x) for x in ANCHORS)
                score = min(min_cat, min_cat2)
                if score > best_score:
                    best_score = score
                    best = (l, c, h1, h2, a, b, min_cat, min_cat2)

l, c, h1, h2, a, b, m1, m2 = best
print(f"最优: MANIFEST={a} (L={l} C={c} H={h1}°), SEQUENCE={b} (H={h2}°)")
print(f"  MANIFEST 与分类最小ΔE={m1:.3f}, SEQUENCE 与分类最小ΔE={m2:.3f}, 两色间ΔE={de(a,b):.3f}")
print("  MANIFEST vs 各分类:", [(round(de(a, x), 3)) for x in ANCHORS])
print("  SEQUENCE vs 各分类:", [(round(de(b, x), 3)) for x in ANCHORS])
