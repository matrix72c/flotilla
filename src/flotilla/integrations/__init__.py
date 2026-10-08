"""使用方适配层：xtuner 的 FlotillaProvider 与 Harbor 的 BaseEnvironment。

按接口的结构实现，不 import xtuner / Harbor 的实现（PRD T1）。处于最外层，不被其它内部包 import。
"""

from __future__ import annotations
