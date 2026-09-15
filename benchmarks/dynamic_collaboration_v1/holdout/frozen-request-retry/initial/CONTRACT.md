# 用户需求

瞬时失败重试时重新生成 request，导致一次重试变成了不同请求。run(build, send, attempts) 应只 build 一次，所有 send 收到同一请求对象；仅重试 Transient，次数耗尽重抛最后异常，其他错误立即传播。不要因重试重复构造请求或吞掉取消。
