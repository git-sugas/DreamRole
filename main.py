"""DreamRole - 入口文件。"""
import sys
import os

# 确保能找到 src 包
if __package__ is None and __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# [!] tiktoken 词表缓存目录：打包版词表随包分发（spec 把缓存收进 _internal/tiktoken_cache），
# 指向包内目录后 count_tokens 无需联网下载 cl100k_base 词表（开发机有系统级缓存可命中，
# 离线用户首次运行也能正常计数）。tiktoken 启动时读此环境变量，须在 import tiktoken 之前设置。
if getattr(sys, "frozen", False):
    _cache_dir = os.path.join(os.path.dirname(sys.executable), "_internal", "tiktoken_cache")
    if os.path.isdir(_cache_dir):
        os.environ["TIKTOKEN_CACHE_DIR"] = _cache_dir

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

    # [!] 远程服务（手机端联动）：启动时据 AppConfig 开关决定是否起后台服务。
    # 远程模块零 PySide6 依赖，import 或启动失败均 try/except 兜底，绝不影响 GUI。
    try:
        app_config = services.get("app_config")
        if app_config is not None and getattr(app_config, "remote_enabled", False):
            from src.remote import start_server
            start_server(services, app_config)
    except Exception as e:
        # 不弹窗，仅 stderr 输出，避免影响主程序（用户可在设置里关掉开关）
        print(f"[DreamRole] 远程服务启动失败: {e}", file=sys.stderr)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()