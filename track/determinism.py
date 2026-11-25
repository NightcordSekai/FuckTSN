#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把仓库里那些**未播种**的随机源接上种子，让 `--seed` 名副其实。

为什么需要它
------------
`sdk/runpath.py`（真实客户端的忠实移植）里有两处随机**不接外部 rng**：

    gps_counter = random.randint(0, 15)      # 模块级全局 RNG
    rng = random.Random()                    # 无名 Random，取 OS 熵
    _step_rng = random.Random()
    ...
    elif random.random() < 0.004: ...        # 全局 RNG

所以 `app/backfill.py:build_run(day, ref, setting, rng)` 里即便调用方传了
`random.Random(seed)`，**点串每次重跑仍然不同**。实测同一 seed 连跑两次：
`km 2.3524 vs 2.3299`，点串 md5 也不同 —— `--seed` 在此之前只是摆设。

`track/verify_plan.py:install_deterministic_random` 只解决了一半：它让
`random.Random()` 去取一个**每次调用都 +1 的计数器**，所以「同进程内跑两次」
依旧不同（那是给「一进程只跑一份 plan」设计的）。本模块补上缺的那一半：
块内所有未显式播种的随机源都从同一个 seed 派生，退出时恢复原状。

用法
----
    from track.determinism import seeded

    with seeded(20261125):
        run = build_run(day, ref, setting, random.Random(20261125))
"""
from __future__ import annotations

import contextlib
import hashlib
import random

_ORIGINAL_RANDOM = random.Random


class _SeededRandom(_ORIGINAL_RANDOM):
    """未显式播种时，从**当前已被 seed 的**全局 RNG 取种子。"""

    _tsn_seeded_patch = True

    def __init__(self, x=None):
        super().__init__(random.getrandbits(64) if x is None else x)


@contextlib.contextmanager
def seeded(seed):
    """块内 `random.random()` 与 `random.Random()` 全部由 seed 派生。"""
    snapshot = random.getstate()
    nested = getattr(random.Random, '_tsn_seeded_patch', False)
    random.seed(seed)
    random.Random = _SeededRandom
    try:
        yield
    finally:
        random.setstate(snapshot)
        if not nested:
            random.Random = _ORIGINAL_RANDOM


def seed_of(*parts) -> int:
    """把任意可复现的输入折成一个 32 位种子。"""
    blob = '|'.join(repr(p) for p in parts).encode('utf-8')
    return int(hashlib.md5(blob).hexdigest()[:8], 16)


def seed_from_rng(rng: random.Random) -> int:
    """从调用方给的 rng **当前状态**派生种子。

    这样既不需要改任何调用方签名，又保留了「rng 前进 → 每条记录不同」
    的原意：同 seed 的 rng 必然给出同一条状态，同一条状态必然给出同一份点串。
    """
    return seed_of(rng.getstate())
