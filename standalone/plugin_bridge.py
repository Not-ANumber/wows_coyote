# -*- coding: utf-8 -*-
"""加载现有 wows_coyote/main.py，但不像 DGHub 那样启动它。

Phase 1 不改动插件源码：
  - 复用 TelemetryReader / MixerEngine / 配置 schema / 曲线表；
  - 用 StandaloneEngine 覆写 DGHub 输出方法；
  - 避免把插件复制一份造成两套逻辑。
"""

from __future__ import annotations

import importlib.util
import os
import sys

if getattr(sys, "frozen", False):
    # PyInstaller one-dir/one-file：资源在 _MEIPASS（one-dir 通常是 _internal）。
    ROOT = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
else:
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_PATH = os.path.join(ROOT, "wows_coyote", "main.py")

_spec = importlib.util.spec_from_file_location("wows_coyote_main", PLUGIN_PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("无法加载 wows_coyote/main.py：{}".format(PLUGIN_PATH))
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)
