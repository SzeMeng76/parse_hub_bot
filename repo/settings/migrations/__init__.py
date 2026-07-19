from collections.abc import Callable

from repo.settings.migrations.v1_to_v2 import migrate as migrate_v1_to_v2
from repo.settings.migrations.v2_to_v3 import migrate as migrate_v2_to_v3
from repo.settings.schema import Config

# key = 源版本号，value = 迁移到 key+1 的函数
REGISTRY: dict[int, Callable[[Config], Config]] = {
    1: migrate_v1_to_v2,
    2: migrate_v2_to_v3,
}

__all__ = ["REGISTRY"]
