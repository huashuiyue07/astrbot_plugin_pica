"""
排行榜定时推送订阅

按会话（群聊/私聊）记录订阅的榜单类型，持久化到
``{data_dir}/rank_subscriptions.json``::

    {
      "default:group:123456": {
        "targets": ["H24", "D7"],
        "owner_id": "10001",      # 订阅发起人，定时推送时优先用其绑定的账号
        "updated_at": 1759900000.0
      }
    }

推送时间由配置里的 cron 表达式决定（见 core/cron.py），与订阅关系分开存储。
"""

import json
import os
import time
from pathlib import Path

from astrbot.api import logger

from .constants import RANK_TYPES

# 订阅文件（位于插件数据目录下，不受 /picaclean 影响）
SUB_FILE = "rank_subscriptions.json"


class RankSubscriptionStore:
    """排行榜订阅存取（文件很小，读写都直接落盘，保证多实例一致）"""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / SUB_FILE

    # ---------- 内部 ----------

    @staticmethod
    def _norm_targets(targets) -> list[str]:
        """过滤非法值、去重，并统一按 RANK_TYPES 的顺序排列"""
        if not isinstance(targets, list):
            return []
        wanted = {str(t).strip().upper() for t in targets}
        return [tt for tt in RANK_TYPES if tt in wanted]

    def _read(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"读取排行榜订阅失败: {e}")
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): v for k, v in data.items() if isinstance(v, dict)}

    def _write(self, data: dict[str, dict]) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning(f"保存排行榜订阅失败: {e}")

    # ---------- 读取 ----------

    def all(self) -> dict[str, dict]:
        """全部订阅 {umo: 记录}"""
        return self._read()

    def targets(self, umo: str) -> list[str]:
        """该会话订阅的榜单类型"""
        return self._norm_targets(self._read().get(umo, {}).get("targets"))

    def owner_of(self, umo: str) -> str:
        """订阅发起人（定时推送优先用其绑定的账号）"""
        return str(self._read().get(umo, {}).get("owner_id") or "")

    def groups_for(self, tt: str) -> list[str]:
        """订阅了该榜单类型的全部会话（顺序稳定，便于测试与日志）

        只读一次文件再在内存里筛：此前每个元素都会走一次 ``targets()``，
        相当于把整个 JSON 读 n+1 遍。
        """
        tt = str(tt).strip().upper()
        groups: list[str] = []
        for umo, record in sorted(self._read().items()):
            if tt in self._norm_targets(record.get("targets")):
                groups.append(umo)
        return groups

    # ---------- 写入 ----------

    def add(self, umo: str, tt: str, owner_id: str = "") -> bool:
        """添加订阅，返回是否为「新增」（已订阅则返回 False，但会更新 owner）"""
        tt = str(tt).strip().upper()
        if tt not in RANK_TYPES:
            return False
        data = self._read()
        record = data.get(umo) or {}
        targets = self._norm_targets(record.get("targets"))
        added = tt not in targets
        if added:
            targets = self._norm_targets([*targets, tt])
        data[umo] = {
            "targets": targets,
            "owner_id": str(owner_id or record.get("owner_id") or ""),
            "updated_at": time.time(),
        }
        self._write(data)
        return added

    def remove(self, umo: str, tt: str | None = None) -> list[str]:
        """取消订阅，返回被取消的榜单类型列表。

        ``tt`` 为 None 时取消该会话的全部订阅（并删除记录）。
        """
        data = self._read()
        record = data.get(umo)
        if not record:
            return []
        targets = self._norm_targets(record.get("targets"))
        if tt is None:
            removed = targets
            targets = []
        else:
            target = str(tt).strip().upper()
            removed = [t for t in targets if t == target]
            targets = [t for t in targets if t != target]

        if targets:
            record["targets"] = targets
            record["updated_at"] = time.time()
            data[umo] = record
        else:
            data.pop(umo, None)
        self._write(data)
        return removed
