# 用户需求

分页读取中文或 emoji 时会丢字。page 的 offset/count 和 next_offset 都应按 Unicode 码点计算，不是 UTF-8 字节。到末尾 next_offset 为 None，越界 offset 或非正 count 报 ValueError。
