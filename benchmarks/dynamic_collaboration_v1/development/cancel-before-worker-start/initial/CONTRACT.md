# 用户需求

后台 Job.start 后立即 cancel 有时缺少 settled 记录。保证每次 admitted 最终恰好一次 settled，cancel 返回前工作已终止，重复取消幂等，运行中取消也适用。不要用固定睡眠猜时序。
