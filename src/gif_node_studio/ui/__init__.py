"""ui：UI 适配层。

2026-08 收敛为单类（见关键决策 #98）：撤销 #82 的 mixin 拆分——PySide6 的
C3 线性化把 Qt 类排在 mixin 之前导致 C++ 虚函数遮蔽（决策 #85），且 mixin
内的跨文件引用无法被 IDE 静态分析定位。现 MainWindow 为单类：

- ``ui.py`` —— `MainWindow` 单类（实例状态 + 信号接线 + 全部行为；菜单/节点/
  视图/执行/预览/存档六职责以区段注释分组，无状态辅助为模块级纯函数）。
- ``session.py`` —— 节点方案存档保存 / 旧存档兼容清洗（纯函数）。
- ``widgets.py`` —— 节点说明面板 / 节点库按钮 / 底部状态栏。
- ``theme.py`` —— 全局主题应用（apply_theme）。
- ``actions.py`` —— 动作定义唯一登记处（菜单/工具栏共用）。
- ``settings_manager.py`` —— 设置管理器（QSettings）与设置对话框。
- ``hotkeys/`` —— 画布右键菜单/顶栏共用的 graph 级功能函数。
"""
