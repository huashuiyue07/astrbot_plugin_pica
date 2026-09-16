"""pytest 全局配置。

- 把仓库根目录加入 sys.path，使测试可直接 `import core.*`；
- 注入 astrbot 模块桩，避免 CI 依赖真实 AstrBot 运行环境
  （core 模块只用到了 `astrbot.api.logger`；插件入口 main.py 另需
  `astrbot.api.message_components` / `astrbot.api.event` / `astrbot.api.star`）；
- 提供 `import_plugin_main()`，把 main.py 作为「包内模块」载入，
  使其中的相对导入 `from .core import ...` 能正确解析。
"""

import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


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


class AstrBotConfig(dict):
    """astrbot.api.AstrBotConfig 的最小桩（插件内仅作类型标注与 .get 使用）"""


api.AstrBotConfig = AstrBotConfig


# ------------------------------------------------------------ message_components
mc = _make_module("astrbot.api.message_components")


class _Component:
    """消息组件桩：接受任意位置/关键字参数，仅用于类型判定"""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.kwargs or self.args})"


for _name in (
    "Plain",
    "Image",
    "File",
    "Video",
    "Record",
    "At",
    "Reply",
    "Face",
    "Json",
):
    setattr(mc, _name, type(_name, (_Component,), {}))


# ------------------------------------------------------------------------- event
event_mod = _make_module("astrbot.api.event")


class MessageChain:
    """astrbot.api.event.MessageChain 的最小桩"""

    def __init__(self, chain=None):
        self.chain = list(chain or [])


class AstrMessageEvent:
    """事件桩；真实环境下 aiocqhttp 事件带有 `.bot`（CQHttp 实例）"""

    def __init__(self, bot=None):
        self.bot = bot


class _Filter:
    """`filter` 桩：任意 `@filter.<anything>(...)` 均原样返回被装饰函数"""

    def __getattr__(self, item):
        def _decorator(*args, **kwargs):
            if args and callable(args[0]):
                return args[0]

            def _wrap(func):
                return func

            return _wrap

        return _decorator


event_mod.MessageChain = MessageChain
event_mod.AstrMessageEvent = AstrMessageEvent
event_mod.filter = _Filter()


# -------------------------------------------------------------------------- star
star_mod = _make_module("astrbot.api.star")


class Context:
    pass


class Star:
    def __init__(self, context=None):
        self.context = context


class StarTools:
    @staticmethod
    def get_data_dir(name):
        raise RuntimeError("StarTools 仅在真实 AstrBot 环境中可用")


def register(*args, **kwargs):
    def _wrap(cls):
        return cls

    return _wrap


star_mod.Context = Context
star_mod.Star = Star
star_mod.StarTools = StarTools
star_mod.register = register


# ------------------------------------------------------------------ 插件入口载入


def import_plugin_main():
    """把仓库根目录当作包，载入其中的 main.py 并返回该模块。

    main.py 使用相对导入（`from .core import ...`），因此不能按普通脚本导入，
    这里显式构造父包模块并设置 `__path__`。
    """
    pkg_name = ROOT.name
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(ROOT)]
        sys.modules[pkg_name] = pkg

    full_name = f"{pkg_name}.main"
    if full_name in sys.modules:
        return sys.modules[full_name]

    spec = importlib.util.spec_from_file_location(full_name, ROOT / "main.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    return mod
