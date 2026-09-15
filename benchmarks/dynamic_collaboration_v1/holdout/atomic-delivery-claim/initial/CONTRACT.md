# 用户需求

两个协程可能同时领取一条消息。Queue.claim 在存在 await 的检查路径上仍须保证同一消息只有一个调用返回 True；另一个返回 False。消息未存在同样 False，领取后状态为 claimed。保留异步 checkpoint 注入以便验证并发，不要删除它或用固定 sleep。
