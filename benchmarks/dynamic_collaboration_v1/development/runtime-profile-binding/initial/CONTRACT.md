# 用户需求

预检查展示的能力与实际启动不一致。launch 要通过 registry 唯一解析 profile，要求运行 Provider/model 与配置一致，再把全部且仅有声明的工具交给 factory。未知 profile、未知工具或模型连接不符必须拒绝，不能偷偷回退。
