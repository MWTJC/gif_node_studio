"""MediaBackend 区段 7：缓存/工作区管理（纯函数模块，决策 #120 拆出）。

与 #82 同名 mixin 无继承关系；本模块同时承载 ``_CacheSizeLedger`` 增量账本类、
缓存工具函数（``_remove_path`` / ``_sum_path_bytes``）与 ``_JOB_DIR_RE`` 常量。"""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import threading
import time


def _collect_evictable_jobs(root_workspace, ledger):
    """收集可淘汰的 job 目录与当前总大小（供 enforce_cache_limit 与测试用）。

    保护规则（不进入淘汰候选）：
    - 每个节点工作区（``root/nodes/<id>/``）下**最新**（mtime 最大）的
      job 目录——预览/帧滑条依赖它；
    - 非 job 条目（名字不含 ``<prefix>_<10位hex>`` 的目录/文件，即
      导出固定缓存 preview.gif / preview_frames 等）。
    返回 ``(候选 job 目录列表, 当前总字节数)``；候选按 mtime 从旧到新排序。
    """
    total = total_cache_size(root_workspace, ledger, )
    candidates: list[Path] = []
    nodes_dir = root_workspace / "nodes"
    if not nodes_dir.is_dir():
        return candidates, total
    for node_dir in nodes_dir.iterdir():
        if not node_dir.is_dir():
            continue
        jobs = [
            path
            for path in node_dir.iterdir()
            if path.is_dir() and _JOB_DIR_RE.match(path.name)
        ]
        if not jobs:
            continue
        latest = max(jobs, key=lambda path: path.stat().st_mtime)
        candidates.extend(job for job in jobs if job is not latest)
    candidates.sort(key=lambda path: path.stat().st_mtime)
    return candidates, total

def cache_size(workspace, root_workspace, ledger):
    """本节点工作区（``root_workspace/nodes/<id>``）字节数。

    读 O(1)（取增量账本值）；未被账本覆盖时回退一次单节点 rglob 统计并记录
    （账本在节点运行结束时由工作线程写入，GUI 线程只在步骤成功后调用）。
    """
    if workspace == root_workspace:
        return total_cache_size(root_workspace, ledger, )
    safe_id = workspace.name
    cached = ledger.get_node(safe_id)
    if cached is not None:
        return cached
    size = _sum_path_bytes(workspace)
    ledger.note_node(safe_id, size)
    return size

def clear_cache(workspace, root_workspace, ledger):
    if workspace == root_workspace:
        clear_workspace(workspace, ledger, )
        return
    _remove_path(workspace)
    ledger.remove_node(workspace.name)

def clear_previous_run(snapshot: list[Path], keep: set[Path] | None=None):
    """删除节点重新运行前的旧缓存（尽力而为，失败不中断运行）。

    ``snapshot`` 须在本次运行开始前捕获；``keep`` 中的路径跳过
    （例如被本次运行原地覆盖的固定文件 gif_export 的 preview.gif）。
    """
    keep = keep or set()
    for path in snapshot:
        if path in keep:
            continue
        try:
            _remove_path(path)
        except Exception:
            pass

def clear_workspace(workspace, ledger):
    workspace.mkdir(parents=True, exist_ok=True)
    for child in workspace.iterdir():
        _remove_path(child)
    # 已知清空：账本置空并标记干净（避免下次对空树全量重建）。
    ledger.clear_known_empty()

def enforce_cache_limit(root_workspace, ledger, limit_bytes: int, *, keep_fraction: float=0.8):
    """缓存总大小超限时淘汰最旧中间缓存，返回 ``(已清理字节, 已清理条目数)``。

    - 只有总量 > ``limit_bytes × keep_fraction`` 才动手（回退系数避免
      临界抖动）；保留规则见 ``_collect_evictable_jobs``（每节点最新
      job + 固定缓存不淘汰）；
    - 逐个按 mtime 从旧到新删除 job 目录，删除尽力而为
      （``_remove_path`` 容忍 Windows 句柄竞争），失败跳过不中断；
    - 未超限时零开销（只做一次 rglob 统计，不扫描节点目录）。
    """
    limit_bytes = max(0, int(limit_bytes))
    target = limit_bytes * max(0.0, min(1.0, keep_fraction))
    candidates, total = _collect_evictable_jobs(root_workspace, ledger, )
    if total <= target or not candidates:
        return (0, 0)
    freed = 0
    removed = 0
    for job in candidates:
        if total - freed <= target:
            break
        size = sum(path.stat().st_size for path in job.rglob("*") if path.is_file())
        try:
            _remove_path(job)
        except Exception:
            continue  # 文件占用等删除失败：跳过该条目，不中断淘汰
        freed += size
        removed += 1
    if removed:
        # 淘汰了旧 job：账本失效（下一次 total_cache_size 重建；
        # _execute_step 会随之调用 refresh_cache_ledger 在工作线程就绪）。
        ledger.invalidate()
    return (freed, removed)

def refresh_cache_ledger(root_workspace, ledger):
    """（工作线程）全量重建账本：遍历 ``root_workspace/nodes/*`` 记录各节点字节数。

    在某次运行淘汰了**其他**节点的旧 job（``enforce_cache_limit`` 返回
    ``removed > 0``）之后调用，保证总账与实际磁盘一致。
    """
    ledger.rebuild_from(root_workspace)

def refresh_node_cache_size(workspace, ledger):
    """（工作线程）重新统计本节点工作区字节数并写入账本。

    在节点运行结束、旧缓存清理后调用；使后续 ``cache_size`` 读到 O(1) 的
    最新值（避免 GUI 线程为显示缓存大小而全量 stat）。
    """
    ledger.note_node(workspace.name, _sum_path_bytes(workspace))

def snapshot_workspace(workspace):
    """列出本节点工作区顶层的既有产物（上一次运行留下的缓存）。

    在节点重新运行前调用；执行成功后把这些条目交给
    :meth:`clear_previous_run` 删除。
    """
    if not workspace.exists():
        return []
    return list(workspace.iterdir())

def total_cache_size(root_workspace, ledger):
    """整个缓存根（``root_workspace``）下所有文件的字节总和。

    读 O(1)（账本求和）；账本失效（发生过删除）时触发一次全量重建。
    重建通常已由工作线程完成（``refresh_cache_ledger``），此处仅在
    未被覆盖的兜底路径上重建（如测试直接构造后未运行节点）。
    """
    if ledger.total_dirty():
        ledger.rebuild_from(root_workspace)
    return ledger.total_bytes()

class _CacheSizeLedger:
    """缓存大小增量账本（node 工作区目录名 → 字节数）。

    背景：cache_size() / total_cache_size() 原实现都是 ``rglob("*")`` + 逐文件
    ``stat()`` 求和，在 GUI 线程执行。缓存里文件一多（如格式化节点物化
    6000 帧/200MB），一次遍历实测约 0.5 s，设置对话框又在打开时 + 每秒各跑
    一次 → 界面明显卡顿（见 _perf_probe/time_cache_scan.py 实测）。

    本账本让**读**变成 O(1)：每个节点工作区的字节数由工作线程在节点运行
    结束后增量写入（``note_node`` / ``rebuild_from``）；``cache_size`` /
    ``total_cache_size`` 直接取账本值求和，不再逐文件 stat。任何**删除**
    （clear_workspace / clear_cache / clear_previous_run / enforce_cache_limit
    淘汰 job）都会 ``invalidate``，下一次 ``total_cache_size`` 因失效回到一次
    全量重建——该重建同样安排在**工作线程**（见 ui._execute_step 末尾），
    GUI 线程只读到已就绪的账本值。

    线程安全：写（工作线程）与读（GUI 线程）可能并发，内部用锁保护。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._nodes: dict[str, int] = {}
        self._total_dirty = True  # 初始未知 → 首次 total 触发一次全量重建

    # --- 写（工作线程） ---

    def note_node(self, safe_id: str, size: int) -> None:
        """记录某节点工作区字节数（节点运行结束后调用）。"""
        with self._lock:
            self._nodes[safe_id] = max(0, int(size))

    def remove_node(self, safe_id: str) -> None:
        """节点工作区被删除后移除其账本条目并使总账失效。"""
        with self._lock:
            self._nodes.pop(safe_id, None)
            self._total_dirty = True

    def clear_known_empty(self) -> None:
        """缓存已知被清空：账本置空并标记干净（避免下次对空树全量重建）。"""
        with self._lock:
            self._nodes.clear()
            self._total_dirty = False

    def invalidate(self) -> None:
        """任何缓存删除/变动：使总账失效，下次 total_cache_size 全量重建。"""
        with self._lock:
            self._total_dirty = True

    def rebuild_from(self, root_workspace: Path) -> None:
        """（工作线程）全量重建：遍历 ``root_workspace/nodes/*`` 记录各节点字节数。"""
        sizes: dict[str, int] = {}
        nodes_dir = root_workspace / "nodes"
        if nodes_dir.is_dir():
            for node_dir in nodes_dir.iterdir():
                if node_dir.is_dir():
                    sizes[node_dir.name] = _sum_path_bytes(node_dir)
        with self._lock:
            self._nodes = sizes
            self._total_dirty = False

    # --- 读（GUI 线程） ---

    def get_node(self, safe_id: str) -> int | None:
        with self._lock:
            return self._nodes.get(safe_id)

    def total_bytes(self) -> int:
        with self._lock:
            return sum(self._nodes.values())

    def total_dirty(self) -> bool:
        with self._lock:
            return self._total_dirty

# job 目录命名：_job_dir 生成 ``<prefix>_<uuid4().hex[:10]>``，末尾为
# 10 位十六进制。固定缓存（preview.gif / preview_frames 等）不含该后缀，
# 天然区分「可淘汰的中间产物」与「必须保留的固定缓存」。
_JOB_DIR_RE = re.compile(r"^.+_[0-9a-f]{10}$")

def _remove_path(path: Path, attempts: int = 5) -> None:
    """Remove a cache path, tolerating short Windows handle-release races."""
    for attempt in range(attempts):
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))

def _sum_path_bytes(path: Path) -> int:
    """单条路径（目录递归）下所有文件的字节和——即原 cache_size /
    total_cache_size 的 rglob 表达式。抽取为模块级纯函数，供账本与后端复用。"""
    if not path.exists():
        return 0
    return sum(entry.stat().st_size for entry in path.rglob("*") if entry.is_file())
