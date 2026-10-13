"""炸板率与连板高度的共用阈值。

行情描述、板块评分、情绪闸门和台账分层使用相同刻度，避免同一读数被不同层矛盾解释。
阈值为规则先验，尚非回测标定结果。
"""

from __future__ import annotations

# 炸板率切点。低于 HEALTHY 封板结实，高于 STRESSED 封板意愿崩塌，高于 FROZEN
# 且没有连板高度就是冰点。
BLOWUP_HEALTHY = 0.30
BLOWUP_STRESSED = 0.50
BLOWUP_FROZEN = 0.60

# 连板高度：有没有资金愿意接力。能出 5 板说明主线在发酵，7 板以上通常已是高潮段。
STREAK_WEAK = 2
STREAK_MOMENTUM = 5
STREAK_CLIMAX = 7
