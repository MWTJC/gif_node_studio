"""媒体后端：MediaBackend。

2026-08（决策 #120）：七个职责区段拆分为七个纯函数模块
（backend_format / backend_color / backend_sequence / backend_export /
backend_quantize / backend_analysis / backend_cache）；本类保留实例状态核心
（__init__ / for_node / _progress / _job_dir）与全部区段方法的薄转发——
外部调用方（节点 execute / runner / ui）仍按 ``backend.<fn>(...)`` 调用，
API 零改动。缓解巨型单体问题（原单文件 2964 行）。

- ``backend_*.py`` —— 七区段纯函数模块（决策 #120；与 #82 同名 mixin 无继承关系）。
- ``palettes.py`` —— 调色板/阈值图辅助；
- ``image_utils.py`` —— wand/PIL 图像底层辅助。
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4
from collections.abc import Callable

from ..core.domain import MediaManifest, SequenceArtifact, CropSpec
from . import (
    backend_analysis,
    backend_cache,
    backend_color,
    backend_export,
    backend_format,
    backend_quantize,
    backend_sequence,
)
from .backend_cache import _CacheSizeLedger
from .imagemagick import ImageMagickRuntime, configure_imagemagick
from .image_utils import PNG_CACHE_COMPRESS_LEVEL  # 转发 def 默认值（决策 #120）

ProgressReporter = Callable[[float | None, str], None]


class MediaBackend:
    """节点执行后端：解码、变换、编码、探测、缓存管理（状态核心 + 区段转发）。"""

    def __init__(
        self,
        workspace: str | Path,
        root_workspace: str | Path | None = None,
        imagemagick: ImageMagickRuntime | None = None,
        progress_callback: ProgressReporter | None = None,
        cache_ledger: _CacheSizeLedger | None = None,
    ):
        self.workspace = Path(workspace)
        self.root_workspace = Path(root_workspace) if root_workspace is not None else self.workspace
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.imagemagick = imagemagick or configure_imagemagick()
        self.progress_callback = progress_callback
        # 缓存大小增量账本：根后端（workspace == root_workspace）新建持有，
        # 节点后端经 for_node 共享同一实例（跨实例一致，读 O(1)）。
        self._cache_ledger = cache_ledger if cache_ledger is not None else _CacheSizeLedger()
    def for_node(self, node_id: str, progress_callback: ProgressReporter | None = None) -> "MediaBackend":
        safe_id = "".join(character if character.isalnum() or character in "-_" else "_" for character in node_id)
        return MediaBackend(
            self.root_workspace / "nodes" / safe_id,
            self.root_workspace,
            self.imagemagick,
            progress_callback if progress_callback is not None else self.progress_callback,
            cache_ledger=self._cache_ledger,
        )
    def _progress(self, fraction: float | None, label: str) -> None:
        if self.progress_callback:
            self.progress_callback(fraction, label)
    def _job_dir(self, prefix: str) -> Path:
        path = self.workspace / f"{prefix}_{uuid4().hex[:10]}"
        path.mkdir(parents=True)
        return path

    # ===== 区段 1（转发至 backend_format，决策 #120） =====
    def extract_first_frame(self, manifest: MediaManifest):
        return backend_format.extract_first_frame(self.workspace, manifest)

    def extract_start_frame(self, manifest: MediaManifest):
        return backend_format.extract_start_frame(self.workspace, manifest)

    def decode_ico(self, path: str | Path):
        return backend_format.decode_ico(self.workspace, path)

    def _extract_video_frame(self, container_or_path, seconds: float):
        return backend_format._extract_video_frame(self.workspace, container_or_path, seconds)

    def format_manifest(self, manifest: MediaManifest):
        return backend_format.format_manifest(self.workspace, self.imagemagick, self._progress, manifest)

    def _format_static_sequence(self, manifest: MediaManifest, output: Path):
        return backend_format._format_static_sequence(self._progress, manifest, output)

    def _format_animated_image(self, manifest: MediaManifest, output: Path):
        return backend_format._format_animated_image(self.imagemagick, self._progress, manifest, output)

    def _format_video(self, manifest: MediaManifest, output: Path):
        return backend_format._format_video(self._progress, manifest, output)

    def _parallel_pil_export(self, total: int, output: Path, label: str, process: Callable[[int], Image.Image | None], *, compress_level: int=PNG_CACHE_COMPRESS_LEVEL):
        return backend_format._parallel_pil_export(self._progress, total, output, label, process, compress_level=compress_level)

    def _parallel_pil_save_bounded(self, image: Image.Image, target: Path, semaphore: threading.BoundedSemaphore):
        return backend_format._parallel_pil_save_bounded(image, target, semaphore)

    def _drain_save_futures(self, futures: list[Future]):
        return backend_format._drain_save_futures(futures)

    # ===== 区段 2（转发至 backend_color，决策 #120） =====
    def adjust_color(self, artifact: SequenceArtifact, brightness: float=0, saturation: float=0):
        return backend_color.adjust_color(self.workspace, self._progress, artifact, brightness, saturation)

    def hue_sat_range_sequence(self, artifact: SequenceArtifact, *, center_hue: float | None = None,
                               hue_delta: float = 0.0, sat_delta: float = 0.0,
                               light_delta: float = 0.0, range_half: float = 15.0,
                               feather_deg: float = 30.0):
        """PS 色相/饱和度（选区/全图，序列 → 序列）转发（决策 #134）。"""
        return backend_color.hue_sat_range_sequence(
            self.workspace, self._progress, artifact,
            center_hue=center_hue, hue_delta=hue_delta, sat_delta=sat_delta,
            light_delta=light_delta, range_half=range_half, feather_deg=feather_deg,
        )

    def binarize_sequence(self, artifact: SequenceArtifact, threshold: int):
        return backend_color.binarize_sequence(self.workspace, self._progress, artifact, threshold)

    def grayscale_sequence(self, artifact: SequenceArtifact):
        return backend_color.grayscale_sequence(self.workspace, self._progress, artifact)

    def contrast_sequence(self, artifact: SequenceArtifact, amount: float):
        return backend_color.contrast_sequence(self.workspace, self._progress, artifact, amount)

    def invert_sequence(self, artifact: SequenceArtifact):
        return backend_color.invert_sequence(self.workspace, self._progress, artifact)

    def flip_sequence(self, artifact: SequenceArtifact, direction: str):
        return backend_color.flip_sequence(self.workspace, self._progress, artifact, direction)

    def color_key_sequence(self, artifact: SequenceArtifact, key_color: tuple[int, int, int], edge_strength: float):
        return backend_color.color_key_sequence(self.workspace, self._progress, artifact, key_color, edge_strength)

    def color_key_tolerance_sequence(self, artifact: SequenceArtifact, key_color: tuple[int, int, int], tolerance: float = 50.0, feather: float = 50.0):
        return backend_color.color_key_tolerance_sequence(self.workspace, self._progress, artifact, key_color, tolerance, feather)

    # ===== 区段 3（转发至 backend_sequence，决策 #120） =====
    def rewind_sequence(self, artifact: SequenceArtifact):
        return backend_sequence.rewind_sequence(self.workspace, self._progress, artifact)

    def freeze_sequence(self, artifact: SequenceArtifact, *, end: str='first', count: int=1):
        return backend_sequence.freeze_sequence(self.workspace, self._progress, artifact, end=end, count=count)

    def concat_sequences(self, a: SequenceArtifact, b: SequenceArtifact, *, resample: str='lanczos', strategy: str='fit'):
        return backend_sequence.concat_sequences(self.workspace, self._progress, a, b, resample=resample, strategy=strategy)

    def align_resolution(self, a: SequenceArtifact, b: SequenceArtifact, *, resample: str='lanczos', strategy: str='fit'):
        return backend_sequence.align_resolution(self.workspace, self._progress, a, b, resample=resample, strategy=strategy)

    def scale_percent_sequence(self, artifact: SequenceArtifact, *, percent: int = 100, resample: str = 'lanczos'):
        return backend_sequence.scale_percent_sequence(self.workspace, self._progress, artifact, percent=percent, resample=resample)

    def overlay_sequences(self, a: SequenceArtifact, b: SequenceArtifact, *, resample: str='lanczos', strategy: str='fit'):
        return backend_sequence.overlay_sequences(self.workspace, self._progress, a, b, resample=resample, strategy=strategy)

    def split_channels(self, artifact: SequenceArtifact):
        return backend_sequence.split_channels(self.workspace, self._progress, artifact)

    def merge_channels(self, red: SequenceArtifact | None, green: SequenceArtifact | None, blue: SequenceArtifact | None, alpha: SequenceArtifact | None):
        return backend_sequence.merge_channels(self.workspace, self._progress, red, green, blue, alpha)

    def split_alpha(self, artifact: SequenceArtifact):
        return backend_sequence.split_alpha(self.workspace, self._progress, artifact)

    def merge_alpha(self, rgb: SequenceArtifact | None, alpha: SequenceArtifact | None):
        return backend_sequence.merge_alpha(self.workspace, self._progress, rgb, alpha)

    def sample_frames(self, artifact: SequenceArtifact, in_fps: int, out_fps: int):
        return backend_sequence.sample_frames(self.workspace, self._progress, artifact, in_fps, out_fps)

    def static_hold_sequence(self, artifact: SequenceArtifact, *, threshold: int=3, reference: str='prev', neighbors: int=4):
        return backend_sequence.static_hold_sequence(self.workspace, self._progress, artifact, threshold=threshold, reference=reference, neighbors=neighbors)

    def trim_sequence(self, artifact: SequenceArtifact, start: int, end: int):
        return backend_sequence.trim_sequence(self.workspace, self._progress, artifact, start, end)

    def split_sequence(self, artifact: SequenceArtifact, cut: int):
        return backend_sequence.split_sequence(self.workspace, self._progress, artifact, cut)

    def align_length(self, a: SequenceArtifact, b: SequenceArtifact, method: str='loop'):
        return backend_sequence.align_length(self.workspace, self._progress, a, b, method)

    def pan_sequence(self, artifact: SequenceArtifact, *, direction: str='right', duration: int=30, curve: str='linear', interpolation: str='bilinear'):
        return backend_sequence.pan_sequence(self.workspace, self._progress, artifact, direction=direction, duration=duration, curve=curve, interpolation=interpolation)

    def crop_sequence(self, artifact: SequenceArtifact, crop: CropSpec):
        return backend_sequence.crop_sequence(self.workspace, self._progress, artifact, crop)

    def squeeze_aspect_sequence(self, artifact: SequenceArtifact, factor: float):
        return backend_sequence.squeeze_aspect_sequence(self.workspace, self._progress, artifact, factor)

    def blank_sequence(self, width: int, height: int, frames: int, color: str):
        return backend_sequence.blank_sequence(self.workspace, self._progress, width, height, frames, color)

    def gradient_sequence(self, width: int, height: int, frames: int, start_color: str, end_color: str, angle: float=0.0):
        return backend_sequence.gradient_sequence(self.workspace, self._progress, width, height, frames, start_color, end_color, angle)

    # ===== 区段 4（转发至 backend_export，决策 #120） =====
    def icon_compose(self, inputs: list[SequenceArtifact], auto_grade: bool=True):
        return backend_export.icon_compose(self.workspace, self._progress, inputs, auto_grade)

    def write_ico(self, artifact: SequenceArtifact, path: str | Path):
        return backend_export.write_ico(artifact, path)

    def export_pngs(self, artifact: SequenceArtifact, directory: str | Path, prefix: str='frame_'):
        return backend_export.export_pngs(self._progress, artifact, directory, prefix)

    def export_gif(self, artifact: SequenceArtifact, path: str | Path, fps: float | None=None, colors: int=256, dither: str='FloydSteinberg', loop: int=0, width_percent: int=100):
        return backend_export.export_gif(self.imagemagick, self._progress, artifact, path, fps, colors, dither, loop, width_percent)

    def export_gif_ffmpeg(self, artifact: SequenceArtifact, path: str | Path, *, fps: float | None=None, width_percent: int=100, max_colors: int=256, stats_mode: str='full', dither: str='floyd_steinberg', bayer_scale: int=5, diff_mode: bool=True):
        return backend_export.export_gif_ffmpeg(self._progress, artifact, path, fps=fps, width_percent=width_percent, max_colors=max_colors, stats_mode=stats_mode, dither=dither, bayer_scale=bayer_scale, diff_mode=diff_mode)

    def export_gif_gifski(self, artifact: SequenceArtifact, path: str | Path, *, fps: float=12.0, quality: int=90, motion_quality: int=90, lossy_quality: int=90, width: int=0, height: int=0, fast_mode: str='normal', repeat: int=0, bounce: bool=False, fixed_color: str | None=None, matte: str | None=None):
        return backend_export.export_gif_gifski(self._progress, artifact, path, fps=fps, quality=quality, motion_quality=motion_quality, lossy_quality=lossy_quality, width=width, height=height, fast_mode=fast_mode, repeat=repeat, bounce=bounce, fixed_color=fixed_color, matte=matte)

    def export_webp(self, artifact: SequenceArtifact, path: str | Path, *, fps: float | None=None, quality: int=80, lossless: bool=False, width_percent: int=100):
        return backend_export.export_webp(self._progress, artifact, path, fps=fps, quality=quality, lossless=lossless, width_percent=width_percent)

    def export_apng(self, artifact: SequenceArtifact, path: str | Path, *, fps: float | None=None, width_percent: int=100):
        return backend_export.export_apng(self._progress, artifact, path, fps=fps, width_percent=width_percent)

    def _export_animated_pillow(self, artifact: SequenceArtifact, path: str | Path, *, fmt: str, fps: float | None, width_percent: int, save_kwargs: dict):
        return backend_export._export_animated_pillow(self._progress, artifact, path, fmt=fmt, fps=fps, width_percent=width_percent, save_kwargs=save_kwargs)

    def optimize_gif(self, manifest: MediaManifest, path: str | Path, *, optimize: str='o3', lossy: int=0, recolor: bool=False, colors: int=128, color_method: str='diversity', dither: str='floyd-steinberg', colormap: str='none', colormap_file: str | None=None, careful: bool=False):
        return backend_export.optimize_gif(self._progress, manifest, path, optimize=optimize, lossy=lossy, recolor=recolor, colors=colors, color_method=color_method, dither=dither, colormap=colormap, colormap_file=colormap_file, careful=careful)

    def _assemble_gif(self, artifact: SequenceArtifact, path: str | Path, *, delay: int, target_width: int, target_height: int, colors: int, dither_index: int, loop: int, fps: float | None=None):
        return backend_export._assemble_gif(self.imagemagick, self._progress, artifact, path, delay=delay, target_width=target_width, target_height=target_height, colors=colors, dither_index=dither_index, loop=loop, fps=fps)

    # ===== 区段 5（转发至 backend_quantize，决策 #120） =====
    def color_reduce_sequence(self, artifact: SequenceArtifact, *, algorithm: str='adaptive', colors: int=256, dither: str='diffusion', map_name: str='o8x8', levels: str='13'):
        return backend_quantize.color_reduce_sequence(self.workspace, self.imagemagick, self._progress, artifact, algorithm=algorithm, colors=colors, dither=dither, map_name=map_name, levels=levels)

    def color_quantize_sequence(self, artifact: SequenceArtifact, *, colorspace: str='srgb', colors: int=256, treedepth: int=0, dither: str='floyd_steinberg', use_ordered: bool=False, ordered_map: str='o8x8', levels: str='', posterize_levels: int=0):
        return backend_quantize.color_quantize_sequence(self.workspace, self.imagemagick, self._progress, artifact, colorspace=colorspace, colors=colors, treedepth=treedepth, dither=dither, use_ordered=use_ordered, ordered_map=ordered_map, levels=levels, posterize_levels=posterize_levels)

    # ===== 区段 6（转发至 backend_analysis，决策 #120） =====
    def analysis_first_frame(self, manifest: MediaManifest | None=None, sequence: SequenceArtifact | None=None):
        return backend_analysis.analysis_first_frame(self.workspace, manifest, sequence)

    def analysis_palette(self, manifest: MediaManifest | None=None, sequence: SequenceArtifact | None=None):
        return backend_analysis.analysis_palette(self._progress, manifest, sequence)

    def palette_swatch(self, colors: list[tuple[int, int, int]], has_transparency: bool):
        return backend_analysis.palette_swatch(self.workspace, colors, has_transparency)

    def analysis_gif_frames(self, manifest: MediaManifest | None=None, mode: str='stored'):
        return backend_analysis.analysis_gif_frames(self.workspace, self.imagemagick, self._progress, manifest, mode)

    def analysis_palette_frames(self, manifest: MediaManifest | None=None):
        return backend_analysis.analysis_palette_frames(self.workspace, manifest)

    def analysis_ico_montage(self, manifest: MediaManifest | None=None):
        return backend_analysis.analysis_ico_montage(self.workspace, manifest)

    # ===== 区段 7（转发至 backend_cache，决策 #120） =====
    def clear_workspace(self):
        return backend_cache.clear_workspace(self.workspace, self._cache_ledger)

    def clear_cache(self):
        return backend_cache.clear_cache(self.workspace, self.root_workspace, self._cache_ledger)

    def snapshot_workspace(self):
        return backend_cache.snapshot_workspace(self.workspace)

    def clear_previous_run(self, snapshot: list[Path], keep: set[Path] | None=None):
        return backend_cache.clear_previous_run(snapshot, keep)

    def cache_size(self):
        return backend_cache.cache_size(self.workspace, self.root_workspace, self._cache_ledger)

    def refresh_node_cache_size(self):
        return backend_cache.refresh_node_cache_size(self.workspace, self._cache_ledger)

    def refresh_cache_ledger(self):
        return backend_cache.refresh_cache_ledger(self.root_workspace, self._cache_ledger)

    def total_cache_size(self):
        return backend_cache.total_cache_size(self.root_workspace, self._cache_ledger)

    def _collect_evictable_jobs(self):
        return backend_cache._collect_evictable_jobs(self.root_workspace, self._cache_ledger)

    def enforce_cache_limit(self, limit_bytes: int, *, keep_fraction: float=0.8):
        return backend_cache.enforce_cache_limit(self.root_workspace, self._cache_ledger, limit_bytes, keep_fraction=keep_fraction)
