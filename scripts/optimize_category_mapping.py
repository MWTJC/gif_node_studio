"""B-1 等距环 → 分类映射优化：穷举 10 个等距点在 9 个业务分类上的分配，
使「敏感分类对」（语义易混、需强区分）的感知距离最大化。

敏感对权重：CHANNEL↔OUTPUT(粉红), INPUT↔FORMAT, FORMAT↔PREFORMAT,
INPUT↔PREFORMAT, MOTION↔ANALYSIS, ANALYSIS↔OUTPUT, MOTION↔OUTPUT。
"""
import itertools

from coloraide import Color

RING = {
    0:  "#FC7D91",   # 12° 红粉
    1:  "#F8894B",   # 48° 橙
    2:  "#D8A100",   # 84° 黄
    3:  "#9FB835",   # 120° 黄绿
    4:  "#41C780",   # 156° 绿
    5:  "#00C4BF",   # 192° 青绿
    6:  "#00BBF2",   # 228° 青
    7:  "#7FA9FF",   # 264° 蓝
    8:  "#BA92FD",   # 300° 紫
    9:  "#E682D0",   # 336° 粉
}

# 分类 → (候选点, 语义标签)
CATS = {
    "INPUT":     ([4, 5, 3], "绿/黄绿/青绿"),   # 输入=绿系
    "PREFORMAT": ([6, 5, 4], "青/青绿/绿"),     # 预格式化=青系
    "FORMAT":    ([5, 4, 6], "青绿/绿/青"),     # 格式化=青绿系
    "SEQUENCE":  ([8, 7],    "紫/蓝"),         # 序列=紫系
    "PROCESS":   ([7, 6, 8], "蓝/青/紫"),      # 处理=蓝系
    "MOTION":    ([1, 0, 2], "橙/红/黄"),      # 动效=橙系
    "CHANNEL":   ([9, 0, 8], "粉/红/紫"),      # 通道=粉系
    "OUTPUT":    ([0, 9, 1], "红/粉/橙"),      # 输出=红系
    "ANALYSIS":  ([2, 1, 3], "黄/橙/黄绿"),    # 分析=黄系
}

# 敏感对（权重 3）：语义上易混淆，必须拉开
SENSITIVE = {
    ("CHANNEL", "OUTPUT"), ("INPUT", "FORMAT"), ("FORMAT", "PREFORMAT"),
    ("INPUT", "PREFORMAT"), ("MOTION", "ANALYSIS"), ("ANALYSIS", "OUTPUT"),
    ("MOTION", "OUTPUT"), ("INPUT", "PREFORMAT"),
}
# 一般对（权重 1）
NORMAL = {("PREFORMAT", "FORMAT"), ("SEQUENCE", "PROCESS"), ("SEQUENCE", "CHANNEL"),
          ("PROCESS", "PREFORMAT"), ("PROCESS", "SEQUENCE"), ("PROCESS", "MOTION"),
          ("INPUT", "SEQUENCE"), ("FORMAT", "SEQUENCE"), ("MOTION", "CHANNEL"),
          ("ANALYSIS", "MOTION")}

def de(i, j):
    return Color(RING[i]).delta_e(RING[j], method="ok")

D = {(i, j): de(i, j) for i in RING for j in RING if i != j}

names = list(CATS)
best = None
best_score = -1
count = 0
# 每分类选候选点，笛卡尔积
choices = [CATS[c][0] for c in names]
for assign in itertools.product(*choices):
    if len(set(assign)) != len(assign):
        continue
    m = dict(zip(names, assign))
    # 敏感性惩罚：敏感对距离 < 0.12 重罚
    score = 0.0
    penalty = 0.0
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            key = (a, b) if (a, b) in D else (b, a)
            d = D[(m[a], m[b])]
            w = 3 if key in SENSITIVE else (1 if key in NORMAL else 0.5)
            score += w * d
            if key in SENSITIVE and d < 0.115:
                penalty += (0.115 - d) * 20
    s = score - penalty
    if s > best_score:
        best_score = s
        best = m
    count += 1

print(f"穷举 {count} 种（去重后）")
print("最优映射：")
for c in names:
    p = best[c]
    print(f"  {c:10s} → 点{p} {RING[p]}   ({CATS[c][1]})")
print(f"得分 {best_score:.3f}")
print("\n各敏感对距离：")
for a, b in sorted(SENSITIVE):
    print(f"  {a:10s}↔{b:10s}: ΔEOK={D[(best[a], best[b])]:.3f}")
print("\n全部业务对距离（排序）：")
allp = []
for i in range(len(names)):
    for j in range(i + 1, len(names)):
        allp.append((D[(best[names[i]], best[names[j]])], names[i], names[j]))
for d, a, b in sorted(allp)[:6]:
    print(f"  {d:.3f}  {a} ↔ {b}")
