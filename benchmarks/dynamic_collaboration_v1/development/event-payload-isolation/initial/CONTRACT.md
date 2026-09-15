# 用户需求

修复 MemoryLog 的事件隔离：append 输入、append 返回和 read 返回的嵌套 JSON 都属于各自调用者，修改其中一个不能修改账本或其他调用者的数据。保留当前 API。
