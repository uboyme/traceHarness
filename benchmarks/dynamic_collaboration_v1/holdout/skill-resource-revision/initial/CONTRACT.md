# 用户需求

Skill 卸载或更新后，旧目录引用仍能读取资源。read(catalog, reference) 必须核对插件仍活跃、version 与 catalog_digest 都与引用匹配，再返回该资源；缺失插件或资源、旧版本、旧目录都报 ValueError。
