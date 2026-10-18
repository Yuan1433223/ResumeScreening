# -*- coding: utf-8 -*-
"""测试入口配置：隔离的临时 SQLite，避免污染真实数据。

注意：repository/workflow 用 `from .config import DB_PATH` 在导入时复制常量，
因此必须 patch 三个模块（config/repository/workflow）各自的路径变量。
"""
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _isolated_repository(monkeypatch):
    """每个测试用独立临时 DB；重置 repository 单例与 checkpointer 缓存。"""
    tmp_dir = Path(tempfile.mkdtemp(prefix="resume_test_"))
    tmp_db = tmp_dir / "test.db"
    ckpt_db = tmp_dir / "checkpoints.db"

    from src import config
    import src.repository as repo_mod
    import src.task_store as task_store_mod
    import src.workflow as wf

    # 逐模块 patch 导入时复制的路径常量
    for mod in (config, repo_mod, wf, task_store_mod):
        monkeypatch.setattr(mod, "DB_PATH", str(tmp_db))
    for mod in (config, wf):
        monkeypatch.setattr(mod, "CHECKPOINT_DB_PATH", str(ckpt_db))

    # 重置进程内单例 / 编译缓存（指向旧路径的连接全部丢弃）
    repo_mod._default_repo = None
    task_store_mod._default_store = None
    wf._checkpointer_cache = None

    from src.repository import Repository
    return Repository(str(tmp_db))
