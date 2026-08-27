from exec_command import execCmd
from pathlib import Path

def find_ui_paths():
    """
    自动探测ui源文件目录和目标目录
    返回: (ui_source_path, destination_path)
    """
    current_dir = Path(__file__).parent
    possible_source_paths = [
        current_dir / "build_src" ,
        current_dir.parent / "build_src" ,
    ]

    possible_dest_paths = [
        current_dir / "src" / "gif_node_studio",
        current_dir.parent / "src" / "gif_node_studio",
    ]

    # 查找包含.ui或.qrc文件的源目录
    source_path = None
    for path in possible_source_paths:
        if path.exists() and (list(path.glob("*.ui")) or list(path.glob("*.qrc"))):
            source_path = path
            break

    if not source_path:
        raise FileNotFoundError("找不到包含.ui或.qrc文件的源目录")

    # 查找或创建目标目录
    dest_path = None
    for path in possible_dest_paths:
        if path.exists():
            dest_path = path
            break

    return source_path, dest_path

def main():
    try:
        ui_source_path, destination_path = find_ui_paths()
        print(f"源目录: {ui_source_path}")
        print(f"目标目录: {destination_path}")
        if destination_path is None:
            raise Exception("未能定位目标目录，退出...")

        path_object_qrc = list(Path(ui_source_path).glob("*.qrc"))
        runner = ''  # 如需使用pdm，可改为'pdm run'

        cmds = []
        # 更新图像资源
        for qrc_file in path_object_qrc:
            py_file = Path(f"{destination_path}/{qrc_file.stem}_rc.py")
            cmds.append(f"{runner} pyside6-rcc \"{qrc_file}\" -o \"{py_file}\"")

        print("将执行以下命令：")
        for cmd in cmds:
            print(f"- {cmd}")

        for cmd in cmds:
            result, error = execCmd(cmd)
            if error != '':
                raise Exception(f"\n{cmd}:\n{error}")

        print("所有文件已成功更新！")

    except Exception as e:
        print(f"错误: {str(e)}")
        raise

if __name__ == "__main__":
    main()
