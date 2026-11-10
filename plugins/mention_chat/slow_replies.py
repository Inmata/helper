"""艾特的慢回复队列；同一会话内，同一发言者的后续回复排在其前一轮之后。"""
from collections import deque
import queue
import threading


class SlowReplies:
    def __init__(self, logger, capacity=128):
        self.logger = logger
        self.capacity = capacity
        self.stopped = threading.Event()
        self._lock = threading.Lock()
        self._ready = queue.Queue()
        self._following = {}
        self._count = 0
        self._thread = threading.Thread(target=self._run, name="MentionChat.slow", daemon=True)
        self._thread.start()

    def busy(self):
        with self._lock:
            return bool(self._following)

    def queue_if_active(self, key, resume):
        """同一人已有慢回复时保留后续任务；其他人仍走插件原来的线程。"""
        with self._lock:
            if self.stopped.is_set():
                return True
            if key not in self._following:
                return False
            if self._reserve():
                self._following[key].append(resume)
            return True

    def handoff(self, key, turn):
        """接手走到需要等待的位置、仍需继续的回复。"""
        with self._lock:
            if not self.stopped.is_set() and self._reserve():
                self._following[key] = deque()
                self._ready.put((key, lambda: turn))
                return True
        return False

    def _reserve(self):
        if self._count >= self.capacity:
            self.logger.warning("艾特慢回复队列已满，跳过本轮")
            return False
        self._count += 1
        return True

    def _run(self):
        while (job := self._ready.get()) is not None:
            key, resume = job
            try:
                turn = resume()
                if self.stopped.is_set():
                    turn.close()
                else:
                    for _ in turn:
                        pass
            except Exception as error:
                self.logger.warning("艾特慢回复处理失败：%s", type(error).__name__)
            finally:
                with self._lock:
                    self._count -= 1
                    following = self._following[key]
                    if following and not self.stopped.is_set():
                        self._ready.put((key, following.popleft()))
                    else:
                        self._count -= len(following)
                        del self._following[key]

    def close(self):
        with self._lock:
            if not self.stopped.is_set():
                self.stopped.set()
                self._ready.put(None)
        self._thread.join()
