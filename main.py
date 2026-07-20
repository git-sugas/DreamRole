"""DreamRole - 入口文件。"""
import sys
import os

# 确保能找到 src 包
if __package__ is None and __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon

from src.app import init_services, load_theme, get_resource_path
from src.ui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("DreamRole")
    app.setStyleSheet(load_theme())
    # 应用图标：开发模式从 assets/app.ico 读，打包后从 _MEIPASS/assets/app.ico 读
    # （spec 已把 assets/app.ico 加进 datas）。setWindowIcon 同时影响任务栏/标题栏/exe 图标。
    ico_path = get_resource_path(os.path.join("assets", "app.ico"))
    if os.path.exists(ico_path):
        app.setWindowIcon(QIcon(ico_path))

    services = init_services()
    window = MainWindow(services)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()