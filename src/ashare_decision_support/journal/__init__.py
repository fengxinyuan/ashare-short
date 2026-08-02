"""预测台账：每条建议落账，事后按实际走势核对.

这是「持续学习」的第一步，也是现在唯一诚实的一步。样本量不够时拟合模型只会
过拟合，但**不记账就永远不知道这套规则有没有用**——台账先建起来，统计自然会
说话，模型等数据够了再上。

## 价格口径

建议里的价位来自不复权分钟线，核对却只能用前复权日线。两条序列不能直接比。

解决办法是**存相对值**：把每个价位记成相对参考价的百分比，核对时再套到前复权
序列上。百分比对复权免疫，所以这一步不会出错。

副作用是正确的：前复权序列抹掉了除息跳空，所以 MFE/MAE 天然是全收益口径——
一只票除息当天跌 3%，不该算作策略亏了 3%。
"""

from ashare_decision_support.journal.records import Outcome, Recommendation
from ashare_decision_support.journal.stats import summarize
from ashare_decision_support.journal.store import Journal
from ashare_decision_support.journal.verify import verify_pending

__all__ = ["Journal", "Outcome", "Recommendation", "summarize", "verify_pending"]
