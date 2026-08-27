"""nodes：节点类（nodeclass）。

按职责分文件（2026-08 代码整理，见关键决策 #82）：

- ``definitions.py`` —— 纯声明层：PortType / NodeCategory / PortDefinition /
  ParamDefinition 及全部参数子类 / NodeDefinition（与 Qt 无关）。
- ``node_base.py`` —— 领域无关节点基座：StudioNode / StudioNodeItem /
  EmbeddedPanelWidget。
- ``widgets.py`` —— 输入控件（滑条+数值框、choice 下拉、文件路径、颜色）。
- ``crop_overlay.py`` —— 裁剪可视化 overlay。
- ``preview_widgets.py`` —— GIF 逐帧播放器 + 棋盘格透明预览。
- ``parameter_panel.py`` —— 节点参数面板（ParameterPanel）。
- ``input_nodes.py`` —— 输入类节点。
- ``manifest_nodes.py`` —— 预格式化/格式化节点（清单族）。
- ``sequence_nodes.py`` —— 序列处理节点。
- ``process_nodes.py`` —— 一般处理节点（颜色/几何/量化）。
- ``channel_nodes.py`` —— 通道处理节点。
- ``export_nodes.py`` —— 输出/导出节点。
- ``analysis_nodes.py`` —— 分析节点。
- ``registry.py`` —— 唯一注册表 NODE_CLASSES + 派生目录函数。
- ``backdrop.py`` —— 项目版背景框（EditableBackdropNode）。
"""
