"""显著性检验：只依赖标准库，保证克隆下来不装 scipy 也能算。

口径固定为「双侧两比例 Z 检验」：
    z = (p1 - p2) / sqrt(p_pool * (1 - p_pool) * (1/n1 + 1/n2))
    p = 2 * (1 - Phi(|z|))
样本量或转化数缺失时返回可读的失败原因，而不是抛异常给接口层。
"""
from __future__ import annotations

import math
from typing import Any


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def two_proportion_z_test(
    *,
    control_sample: int,
    control_converted: int,
    experiment_sample: int,
    experiment_converted: int,
) -> dict[str, Any]:
    """返回检验结果；入参不合法时 ``ok=False`` 并给出原因。"""
    if min(control_sample, experiment_sample) <= 0:
        return {"ok": False, "reason": "样本量必须为正整数"}
    if not (0 <= control_converted <= control_sample and 0 <= experiment_converted <= experiment_sample):
        return {"ok": False, "reason": "转化数必须落在 0 到样本量之间"}

    control_rate = control_converted / control_sample
    experiment_rate = experiment_converted / experiment_sample
    pooled = (control_converted + experiment_converted) / (control_sample + experiment_sample)
    variance = pooled * (1.0 - pooled) * (1.0 / control_sample + 1.0 / experiment_sample)
    if variance <= 0:
        return {"ok": False, "reason": "两组转化率完全相同且无方差，无法计算显著性"}
    z_score = (experiment_rate - control_rate) / math.sqrt(variance)
    p_value = 2.0 * (1.0 - _normal_cdf(abs(z_score)))
    lift = (experiment_rate - control_rate) / control_rate if control_rate else None
    return {
        "ok": True,
        "control_rate": round(control_rate, 6),
        "experiment_rate": round(experiment_rate, 6),
        "lift": round(lift, 6) if lift is not None else None,
        "z_score": round(z_score, 6),
        "p_value": round(p_value, 6),
    }
