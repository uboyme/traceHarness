# 用户需求

通过了旧补丁的验证报告被用于新补丁。review.accept 必须核对 request 中的 artifact_id、patch_sha、target 和 verifier_digest 全部相同且 passed 为 True；其他情况拒绝。status 字段或模型宣称不能代替这些绑定。
