"""pytest 全局配置。

- 把仓库根目录加入 sys.path，使测试可直接 `import core.*`；
- 注入 astrbot 模块桩，避免 CI 依赖真实 AstrBot 运行环境
  （core 模块只用到了 astrbot.api.logger）。
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(__file__))


def _make_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__name__ = name
    mod.__package__ = name
    sys.modules[name] = mod
    return mod


astrbot = _make_module("astrbot")
api = _make_module("astrbot.api")


class _Logger:
    """与 astrbot.api.logger 接口一致的空实现桩"""

    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


for _mod in (astrbot, api):
    _mod.__path__ = []  # 声明为包，保证后续子模块可注册

api.logger = _Logger()