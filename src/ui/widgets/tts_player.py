"""TTS 音频播放器：多段顺序播放 + 段间 2-3 秒间隔。

用 QMediaPlayer + QAudioOutput 播放 mp3/wav。段间间隔用 QTimer 单次触发，
一段播放结束（PlaybackState -> StoppedState）后启动间隔定时器，到时播放下一段。

全局单例（MainWindow 持有），避免多个 QMediaPlayer 实例冲突。
切换会话/开始新生成时调 stop() 清空队列，避免旧会话音频串到新会话。
"""
from __future__ import annotations
from PySide6.QtCore import QObject, Signal, QTimer, QUrl
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput


# 段间间隔（毫秒）：2.5 秒（2-3 秒中间值）
GAP_MS = 2500


class TtsPlayer(QObject):
    """TTS 音频播放器。"""

    playback_finished = Signal()     # 全部播放完
    playback_started = Signal(str)   # 开始播放某段（path）
    playback_error = Signal(str)    # 播放出错

    def __init__(self, parent=None):
        super().__init__(parent)
        self._audio_output = QAudioOutput()
        self._player = QMediaPlayer()
        self._player.setAudioOutput(self._audio_output)
        self._queue: list[str] = []          # 待播放音频路径队列
        self._gap_timer = QTimer(self)
        self._gap_timer.setSingleShot(True)
        self._gap_timer.setInterval(GAP_MS)
        self._gap_timer.timeout.connect(self._play_next)
        self._is_playing = False
        # 播放状态变化：一段播放结束 -> 启动间隔定时器
        self._player.playbackStateChanged.connect(self._on_state_changed)
        self._player.errorOccurred.connect(self._on_error)

    def play_sequence(self, audio_paths: list):
        """顺序播放多段音频，段间间隔 2.5 秒。

        若正在播放则先停止清空，再播放新的。
        """
        if not audio_paths:
            return
        # 停止当前播放（若有），清空队列
        self.stop()
        self._queue = list(audio_paths)
        self._is_playing = True
        self._play_next()

    def stop(self):
        """停止播放并清空队列。"""
        self._gap_timer.stop()
        self._queue = []
        self._is_playing = False
        try:
            self._player.stop()
        except Exception:
            pass

    def is_playing(self) -> bool:
        return self._is_playing

    def _play_next(self):
        """播放队列中的下一段。"""
        if not self._queue:
            self._is_playing = False
            self.playback_finished.emit()
            return
        path = self._queue.pop(0)
        self.playback_started.emit(path)
        self._player.setSource(QUrl.fromLocalFile(path))
        self._player.play()

    def _on_state_changed(self, state):
        """播放状态变化：一段播放结束 -> 启动间隔定时器。"""
        if state == QMediaPlayer.StoppedState and self._is_playing:
            # 一段播放结束，启动间隔定时器（段间 2.5 秒）
            self._gap_timer.start()

    def _on_error(self, error, error_string=""):
        """播放出错。"""
        self.playback_error.emit(f"播放错误: {error_string or error}")
        # 出错也继续播放下一段（跳过坏文件）
        if self._is_playing:
            self._gap_timer.start()
