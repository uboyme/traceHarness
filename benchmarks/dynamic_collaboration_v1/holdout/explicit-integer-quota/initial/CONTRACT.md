# 用户需求

quota.parse 只接受 1 到 max_value 的真正整数。JSON true/false、浮点数、字符串都必须拒绝，不能被转换成额度。max_value 是显式正整数，非法时同样报 ValueError。
