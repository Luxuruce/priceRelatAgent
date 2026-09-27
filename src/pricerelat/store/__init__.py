"""持久化层：匹配关系库、每期采集快照。

V1 每次运行都从零匹配，人工复核结论无处沉淀。这一层把「确认过的匹配」
存下来，下期直接复用，复核量才会随时间下降。
"""

from .db import Store, current_period

__all__ = ["Store", "current_period"]
