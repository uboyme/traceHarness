# AO-2+ 逐项裁判对照

以下期望是调用前冻结的 Agent 合同审核标签，不是独立人工 gold。8 个原答案加 4 个合成答案，每条件重复两次。领域名、代号和数字仅为显式实验样本。

| 样本 | 内容 | 期望 | 原裁判两次 | 候选两次 |
|---|---|---|---|---|
| case-01 | s-absent (original-agent-answer) | failed | passed / passed | failed / failed |
| case-02 | s-absent (original-agent-answer) | failed | failed / failed | failed / failed |
| case-03 | stamp-catalog (original-agent-answer) | failed | passed / passed | passed / passed |
| case-04 | stamp-catalog (original-agent-answer) | failed | passed / passed | passed / passed |
| case-05 | s-english (original-agent-answer) | passed | passed / passed | passed / passed |
| case-06 | s-resource (original-agent-answer) | passed | passed / passed | passed / passed |
| case-07 | printshop-limit (original-agent-answer) | passed | passed / passed | failed / failed |
| case-08 | geology-transfer (original-agent-answer) | passed | passed / passed | passed / failed |
| case-09 | s-absent (synthetic-contrast) | passed | passed / passed | passed / passed |
| case-10 | stamp-catalog (synthetic-contrast) | passed | passed / passed | passed / passed |
| case-11 | restoration-room (synthetic-contrast) | failed | failed / failed | failed / failed |
| case-12 | restoration-room (synthetic-contrast) | failed | pending_review / passed | passed / passed |

## case-01：s-absent

问题：档案整理手册有没有规定车辆加油的折扣码？

原答案（original-agent-answer）：

> 根据搜索结果，我在档案整理手册中搜索了"车辆加油"和"加油"关键词，但都没有找到匹配内容（no-hit结果）。
>
> 档案整理手册的摘要只显示"本手册说明档案整理工作。"，这表明该手册专注于档案整理相关的工作流程和规范，与车辆加油、折扣码等后勤或财务事项无关。
>
> 因此，档案整理手册**没有规定**车辆加油的折扣码。车辆加油折扣码通常属于公司财务管理、行政后勤或采购政策的范畴，而不是档案整理工作的内容。
>
> 答案：没有。

开发预期：failed；依据：catalog absence overstated。

- baseline / 第 1 次：**passed**，9085 tokens；原记录 `calls/001`。
  The answer correctly states that the archive management manual does not specify a discount code for vehicle refueling. It bases this on two no-hit searches ('车辆加油' and '加油') across all 32 searchable sections of the manual, and on the confirmed summary '本手册说明档案整理工作。', which defines the manual's scope as archival work only. The conclusion ('没有') matches the expectation's 'kind': 'no-evidence' and 'value_type': 'none'. No unsupported claims are made: it notes the likely domain of such a policy (finance/admin/procurement) without asserting its existence elsewhere. The search coverage is complete (scanned=total=32), and the answer does not overstate absence beyond the manual’s defined scope — consistent with the rubric’s requirement that negative answers must not overstate a local no-hit as global absence.

- revised / 第 1 次：**failed**，10963 tokens；原记录 `calls/002`。
  The candidate answer claims the manual 'has no rule' about vehicle fuel discount codes and asserts this conclusively ('没有。'), but the evidence only supports a limited negative finding: two navigation-level searches (on '车辆加油' and '加油') over title/summary/tags of 32 records returned no hits, and the visible section excerpt is just '本手册说明档案整理工作。'. The rubric requires that negative answers must not overstate a local no-hit as global absence. The search did not examine hidden skill bodies (hidden_skill_bodies_searched=false), nor did it search full text — only metadata fields. The expectation is 'no-evidence', meaning the correct answer should reflect absence of evidence, not definitive absence of content. The candidate answer goes beyond the evidence by making a categorical claim ('没有规定') and adding unsupported domain reasoning ('属于财务管理...范畴'). This violates the rubric's instruction that negative answers 'must not invent values or overstate a local no-hit as global absence'.

- revised / 第 2 次：**failed**，10962 tokens；原记录 `calls/047`。
  The candidate answer claims the manual 'has no provision' for vehicle fuel discount codes and concludes 'No', but the evidence only supports absence of the terms '车辆加油' and '加油' in navigation metadata (titles, summaries, tags) — not in the full manual text. The search_coverage shows 'hidden_skill_bodies_searched': false, meaning the actual section bodies (e.g., section-23, which is titled '档案整理操作手册' and has 36 bytes) were *not searched*. The visible_evidence includes only a 13-character excerpt ('本手册说明档案整理工作。') from one section, insufficient to assert the entire manual lacks such a provision. The rubric forbids overstating a local no-hit as global absence; the answer violates this by asserting definitively '没有规定' without examining the manual's substantive content. Also, the expectation is 'kind: no-evidence', meaning the correct answer should acknowledge lack of evidence — not assert factual absence.

- baseline / 第 2 次：**passed**，9057 tokens；原记录 `calls/048`。
  The answer correctly states that the archival manual does not specify a discount code for vehicle refueling. It bases this on two no-hit searches ('车辆加油' and '加油') across all 32 searchable sections of the manual (as confirmed by search_coverage), and on the manual's stated scope ('本手册说明档案整理工作。'), which is unrelated to fuel discounts. The answer does not overstate absence: it correctly limits its conclusion to the archival manual (not all company documents), matches the expectation's 'no-evidence' kind, references the correct skill_id ('manual-593410eb3c'), and quotes the exact source_text from the visible_evidence. No unsupported claims or scope violations are present.


## case-02：s-absent

问题：档案整理手册有没有规定车辆加油的折扣码？

原答案（original-agent-answer）：

> 搜索结果显示没有找到与"车辆加油"相关的章节或内容。搜索返回了"no-hit"状态，表明在档案整理手册的目录结构中没有包含"车辆加油"关键词的章节。
>
> 从手册的目录信息来看，所有章节（section-00到section-30）的摘要都是"说明普通器材登记。"，只有section-23的摘要是"档案整理操作手册"，但也没有提到车辆加油或折扣码相关内容。
>
> 因此，根据现有的档案整理手册目录信息，**档案整理手册没有规定车辆加油的折扣码**。
>
> 这个结论基于手册的目录结构和搜索结果，手册主要关注档案整理操作和普通器材登记，不涉及车辆加油管理或折扣码规定。

开发预期：failed；依据：catalog absence overstated。

- baseline / 第 1 次：**failed**，6903 tokens；原记录 `calls/003`。
  The answer overstates the search scope: it claims '档案整理手册没有规定车辆加油的折扣码' as a definitive global conclusion, but the search_coverage shows 'proves_global_absence': false and only scanned 32 sections (all with generic summaries) using a keyword search for '车辆加油'. The expectation is 'no-evidence' — meaning no evidence was dispatched that confirms or denies the presence of such a rule — yet the answer asserts absence. Furthermore, the manual's actual content (beyond directory metadata) was not read; only titles/summaries were searched, and 'discount code' (折扣码) was never searched for — only '车辆加油'. The answer makes unsupported claims about scope ('不涉及车辆加油管理或折扣码规定') without evidence from full-text inspection or a search for '折扣码'. This violates the rubric's instruction that negative answers must not overstate a local no-hit as global absence.

- revised / 第 1 次：**failed**，8065 tokens；原记录 `calls/004`。
  The candidate answer asserts '档案整理手册没有规定车辆加油的折扣码' as a definitive, global negative conclusion. However, the evidence scope explicitly shows: (1) the search was limited to navigation metadata (id/title/summary/tags) of 32 records, with 'hidden_skill_bodies_searched': false; (2) 'proves_global_absence': false in both search coverage and evidence_scope; and (3) the visible directory body confirms all section content_bytes are tiny (24 or 36 bytes), meaning full section text was never examined — only summaries were searched. The rubric forbids overstating a local no-hit as global absence. The answer also adds unsupported interpretive claims ('手册主要关注档案整理操作和普通器材登记，不涉及车辆加油管理或折扣码规定') that go beyond the sparse, generic summaries ('说明普通器材登记。') and the stated purpose ('本手册说明档案整理工作。'). Thus, the answer violates the rubric's requirement for negative answers not to overstate search coverage.

- revised / 第 2 次：**failed**，8049 tokens；原记录 `calls/045`。
  The candidate answer overstates the search scope: it concludes '档案整理手册没有规定车辆加油的折扣码' as a definitive, global negative claim, but the evidence only supports absence *in navigation metadata* (id/title/summary/tags) — not in full section bodies. The search_coverage explicitly states 'hidden_skill_bodies_searched': false and 'proves_global_absence': false. The rubric forbids treating a local no-hit as global absence. Additionally, the answer introduces unsupported extra claims: '手册主要关注档案整理操作和普通器材登记，不涉及车辆加油管理或折扣码规定' — this functional scope assertion is not derivable from the visible directory metadata (which only shows summaries like '说明普通器材登记。' and one section titled '档案整理操作手册'), nor is it grounded in any body content. The expectation is 'no-evidence', meaning no evidence was dispatched or found; the answer must reflect that limitation, not assert authoritative absence.

- baseline / 第 2 次：**failed**，6874 tokens；原记录 `calls/046`。
  The answer overstates the search scope: it claims '档案整理手册没有规定车辆加油的折扣码' as a definitive global conclusion, but the search_coverage shows 'proves_global_absence': false and only scanned 32 sections (total: 32) in title/summary/tags — not full text. The manual's actual content (beyond summaries) is unexamined; 'no-hit' in metadata fields does not prove absence from body text. Also, the expectation is 'no-evidence' (i.e., no evidence was dispatched to support *any* claim about discount codes), yet the answer asserts a negative factual conclusion without dispatched evidence from the manual’s full content. This violates the rubric’s requirement that negative answers must not overstate a local no-hit as global absence.


## case-03：stamp-catalog

问题：邮票目录整理规范里写了停车券多少钱吗？

原答案（original-agent-answer）：

> 邮票目录整理规范（`stamp-catalog.manual`）的目录结构已提供，其中包含一个主节“条目登记”（`required-procedure`）和多个“清洁登记”类小节（`routine-0` 至 `routine-9`），但**所有节摘要均未提及“停车券”或价格相关内容**。
>
> 该 Skill 文档定位为“邮票目录整理规范操作说明”，主题聚焦于邮票条目登记与清洁记录流程，与停车券定价无语义关联。且当前披露的导航元数据中：
> - 无任何 section 或 resource 的 `title`、`summary`、`tags` 包含 “停车券”、“停车”、“券”、“price”、“费用”、“多少钱” 等关键词；
> - `content_bytes` 均极小（51–77 字节），表明内容高度精简，不支持隐含定价信息；
> - 未提供实际正文内容（`body_status: "navigation-only"`），但导航本身已足够否定相关性。
>
> 因此，**邮票目录整理规范里没有写停车券多少钱**。
>
> 答案：否。

开发预期：failed；依据：catalog absence overstated。

- baseline / 第 1 次：**passed**，3825 tokens；原记录 `calls/005`。
  The visible evidence is a complete `stamp-catalog.manual` skill directory with `tier: "directory"`, containing only section metadata (no full bodies). All 11 sections have `content_bytes` between 51–77, and all `title`s and `summary`s are strictly about stamp cataloging (e.g., '条目登记', '清洁登记 X') with no mention of '停车券', '停车', 'price', '费用', or synonyms. The `summary` field of the skill itself states '邮票目录整理规范操作说明。', confirming domain scope. Per rubric, a complete authoritative navigation excerpt suffices for a negative answer; no unsupported claims are made, no value is invented, and the conclusion ('否') correctly reflects absence of pricing information in the provided reference. Scope is properly limited to this Skill document — not overgeneralized to all possible sources.

- revised / 第 1 次：**passed**，4380 tokens；原记录 `calls/006`。
  The candidate answer correctly concludes '否' (No) based solely on the visible navigation-only evidence: the skill directory metadata for 'stamp-catalog.manual' contains no title, summary, or tag referencing '停车券', '价格', or related terms; all sections are semantically about stamp cataloging and cleaning records; content_bytes are tiny (51–77), confirming absence of substantive pricing detail; and the rubric permits a negative answer when authoritative navigation metadata suffices to negate relevance — which it does here. The answer makes no unsupported claims, does not overstate coverage (explicitly noting navigation-only scope), and aligns with the 'no-evidence' expectation. All reasoning is grounded in the provided visible_evidence.

- revised / 第 2 次：**passed**，4437 tokens；原记录 `calls/043`。
  The candidate answer correctly concludes '否' (No) to the question '邮票目录整理规范里写了停车券多少钱吗？', based solely on the visible navigation-only evidence: a directory-level Skill metadata object for 'stamp-catalog.manual'. That metadata contains no '停车券', '价格', '费用', or semantic variants in any title, summary, or tags; all sections are small (51–77 bytes) and semantically about stamp cataloging and cleaning records; and the document's stated purpose ('邮票目录整理规范操作说明') is unrelated to parking vouchers. The rubric permits negative answers when a complete authoritative search excerpt suffices — here, the sole disclosed view is navigation-only, and the answer restricts its claim to what that navigation reveals ('未提及', '无语义关联', '导航本身已足够否定相关性'), avoiding overgeneralization to unseen body content. No unsupported claims, no invented values, no scope violation, and evidence was actually present and used.

- baseline / 第 2 次：**passed**，3787 tokens；原记录 `calls/044`。
  The visible evidence is a complete `stamp-catalog.manual` skill directory navigation object. It contains no section or resource with title, summary, or tags referencing '停车券', '停车', '券', 'price', '费用', '多少钱', or semantic equivalents. All `content_bytes` are tiny (51–77), confirming no substantive pricing content is present. The document’s stated purpose ('邮票目录整理规范操作说明') is semantically unrelated to parking vouchers or pricing. No further reading is needed: the navigation-only metadata is authoritative and sufficient to conclude the manual does not contain parking voucher pricing information — satisfying the 'no-evidence' expectation and rubric requirements for a negative answer without overstatement.


## case-04：stamp-catalog

问题：邮票目录整理规范里写了停车券多少钱吗？

原答案（original-agent-answer）：

> 邮票目录整理规范（`stamp-catalog.manual`）的目录结构（当前参考中提供的 `directory` 导航）仅包含以下部分：
>
> - “条目登记”（required-procedure）
> - 10 个“清洁登记”例行章节（routine-0 至 routine-9）
>
> 所有 section 的 `summary` 均为“清洁记录”，`title` 均为“清洁登记 X”，且无一提及“停车券”或价格相关内容。`summary` 和 `title` 中均未出现“停车券”“停车”“券”“价格”“元”“钱”等关键词。
>
> 该 Skill 文档的总体 `summary` 是：“邮票目录整理规范操作说明。”，主题明确限定于邮票目录整理，与停车券无关。
>
> 因此，**邮票目录整理规范里没有写停车券多少钱**。
>
> 答案：否。

开发预期：failed；依据：catalog absence overstated。

- baseline / 第 1 次：**passed**，3676 tokens；原记录 `calls/007`。
  The visible evidence is a complete `directory`-tier Skill reference for `stamp-catalog.manual`, containing only sections related to '条目登记' and ten '清洁登记 X' routines, all with summary '清洁记录.' and no mention of '停车券', '停车', '券', '价格', '元', or '钱'. The overall `summary` explicitly limits scope to '邮票目录整理规范操作说明。'. No content supports pricing of parking vouchers. The negative answer ('否') is correctly grounded, scoped to the provided directory navigation (not overgeneralized), and makes no unsupported claims — satisfying the rubric's requirement for negative answers not to overstate local absence as global.

- revised / 第 1 次：**passed**，4371 tokens；原记录 `calls/008`。
  The candidate answer correctly states that the visible evidence — a `directory`-tier Skill navigation metadata for `stamp-catalog.manual` — contains only sections titled '条目登记' and ten '清洁登记 X' routines, all with summary '清洁记录', and a top-level summary stating the document is about '邮票目录整理规范操作说明'. None of the titles, summaries, or overall description mention '停车券', '停车', '券', '价格', '元', or '钱'. The answer draws a negative conclusion ('没有写停车券多少钱') strictly within the scope of the admitted evidence (navigation-metadata-only), explicitly noting the absence of relevant keywords and thematic mismatch. It does not claim global absence of such content in the full manual (e.g., by asserting 'the manual contains no such rule'), nor does it invent values or overstate coverage. The rubric permits a negative answer when supported by a complete authoritative search excerpt — here, the directory body *is* the entire visible evidence, and its structure and content are fully disclosed and examined. The expectation is 'no-evidence', and the answer correctly reports no evidence found for the claim, aligning with both visible_evidence and evidence_scope.

- revised / 第 2 次：**passed**，4321 tokens；原记录 `calls/041`。
  The candidate answer correctly states that the visible evidence — a directory-tier navigation metadata object for `stamp-catalog.manual` — contains only sections related to '条目登记' and ten '清洁登记' routines, all with summaries and titles indicating cleaning records, and no mention of '停车券', '价格', or monetary terms. The overall summary explicitly limits scope to '邮票目录整理规范操作说明'. The answer draws a negative conclusion ('没有写停车券多少钱') strictly within the bounds of the admitted evidence (navigation-metadata-only, extent='navigation-metadata-only'), does not claim exhaustive search of full section bodies (which were not dispatched), and avoids inventing values or overgeneralizing beyond the visible metadata. This satisfies the rubric's requirement for negative answers: it does not overstate local absence as global absence, is fully grounded in the actually visible evidence, and matches the expectation of 'no-evidence' (i.e., no evidence supporting the claim was present).

- baseline / 第 2 次：**passed**，3722 tokens；原记录 `calls/042`。
  The visible evidence is a complete `directory`-tier Skill reference for `stamp-catalog.manual`, containing only sections related to stamp catalog organization (one 'required-procedure' and ten 'routine-X' sections, all with summary '清洁记录.' and titles like '清洁登记 X'). Neither the overall `summary` ('邮票目录整理规范操作说明。') nor any section `summary` or `title` contains keywords like '停车券', '停车', '券', '价格', '元', or '钱'. The answer correctly states that the document does not specify parking coupon pricing, attributes this to the observed directory structure and metadata, makes no unsupported claims, and stays strictly within the scope of the provided reference. No further reading or searching is needed or justified — the directory is authoritative for structural content, and the absence of relevant terms in all exposed titles and summaries suffices to support the negative conclusion per the rubric.


## case-05：s-english

问题：Which code does the equipment drill manual specify for the check-in card?

原答案（original-agent-answer）：

> The equipment drill manual specifies the check-in card code as **REF-EA333BB356**.

开发预期：passed；依据：source section supports answer。

- baseline / 第 1 次：**passed**，6106 tokens；原记录 `calls/009`。
  The answer correctly states the code as 'REF-EA333BB356', which exactly matches the expected value. The dispatched evidence includes a direct quote from the equipment drill manual ('The equipment drill check-in card code is REF-EA333BB356.') with proper attribution to skill_id 'manual-ea333bb356' and section_id 'section-23'. The read request for that section succeeded, and the visible evidence confirms the body text contains the exact source string. No unsupported claims or scope violations are present.

- revised / 第 1 次：**passed**，7415 tokens；原记录 `calls/010`。
  The candidate answer correctly states the check-in card code as 'REF-EA333BB356', which exactly matches the expected value. The evidence is properly attributed to the equipment drill manual (skill_id 'manual-ea333bb356', section-23), and the source text 'The equipment drill check-in card code is REF-EA333BB356.' is present in the visible_evidence under kind=skill, tier=section. The dispatched_evidence confirms this excerpt was retrieved via request_skill_reference and used before answering. No unsupported claims or scope violations are present; the answer is concise, accurate, and grounded solely in the admitted evidence.

- revised / 第 2 次：**passed**，7413 tokens；原记录 `calls/039`。
  The candidate answer correctly states the check-in card code as 'REF-EA333BB356', matching the expectation's value. The evidence is properly attributed to the equipment drill manual (skill_id: manual-ea333bb356, section-23), and the exact source text 'The equipment drill check-in card code is REF-EA333BB356.' is present in the visible_evidence under kind=skill, tier=section. The dispatched_evidence confirms this excerpt was retrieved via request_skill_reference and used before answering. No unsupported claims or scope violations are present; the answer is precise, attributed, and grounded in admitted evidence.

- baseline / 第 2 次：**passed**，6143 tokens；原记录 `calls/040`。
  The answer correctly states the code as REF-EA333BB356, which matches the expectation's value field. The value is explicitly attributed to the equipment drill manual (reference_id: manual-ea333bb356), and the source_text 'The equipment drill check-in card code is REF-EA333BB356.' is present in the visible_evidence under kind=skill, tier=section. The evidence was dispatched before answering (dispatched_evidence includes the text and reference metadata), and no unsupported claims or scope violations are present. The search_coverage confirms a match for 'check-in card' leading to the correct section, and the read_requests confirm successful retrieval of that section. All rubric requirements for a positive answer are satisfied.


## case-06：s-resource

问题：档案整理手册的应急联络卡上，值班呼号是什么？

原答案（original-agent-answer）：

> 根据提供的档案整理手册技能文档，应急联络卡上规定的值班呼号是 **REF-EC8DE757AB**。

开发预期：passed；依据：source resource supports answer。

- baseline / 第 1 次：**passed**，7116 tokens；原记录 `calls/011`。
  The answer correctly extracts the value 'REF-EC8DE757AB' from the dispatched evidence, which is explicitly stated in the skill chunk: '应急联络卡规定值班呼号为 REF-EC8DE757AB。'. The value matches the expectation's 'value' field, is of type 'code', and is properly attributed to the referenced skill document (id: manual-ec8de757ab). The evidence was successfully requested via request_skill_reference (call_seq 15) and admitted as visible_evidence (tier: chunk), satisfying the requirement that evidence be dispatched before answering. No unsupported claims or scope violations are present.

- revised / 第 1 次：**passed**，8036 tokens；原记录 `calls/012`。
  The candidate answer correctly states the值班呼号 as 'REF-EC8DE757AB', which exactly matches the value in the expectation and is directly quoted from the dispatched and visible skill chunk: '应急联络卡规定值班呼号为 REF-EC8DE757AB。'. The answer attributes the source accurately ('档案整理手册技能文档') and makes no unsupported claims. Evidence was dispatched prior to answering (request_skill_reference succeeded for chunk-ec8de757ab), and the visible_evidence includes both the directory navigation (confirming skill identity) and the exact chunk body containing the source text. All rubric requirements for a positive answer are satisfied: correct value, proper attribution, evidence actually present and used, no scope violation.

- revised / 第 2 次：**passed**，8011 tokens；原记录 `calls/037`。
  The candidate answer correctly states the值班呼号 as 'REF-EC8DE757AB', which exactly matches the expected value in the expectation. The value is correctly attributed to the '档案整理手册技能文档' (manual-ec8de757ab), and the dispatched evidence includes the visible chunk body '应急联络卡规定值班呼号为 REF-EC8DE757AB。', confirming both the value and its source. No unsupported claims are made, and the evidence was actually dispatched before answering. The rubric requires correct value and attribution, evidence actually dispatched, and no extra unsupported claims — all satisfied.

- baseline / 第 2 次：**passed**，7095 tokens；原记录 `calls/038`。
  The answer correctly identifies the值班呼号 as 'REF-EC8DE757AB', matching the expectation's value. The value is explicitly stated in the dispatched evidence ('应急联络卡规定值班呼号为 REF-EC8DE757AB。') and appears in the visible_evidence as a skill chunk body. Attribution is proper (to the '档案整理手册技能文档'), the evidence was dispatched before answering, no unsupported claims are made, and the scope (emergency contact card in the manual) is respected. The rubric's requirements for a positive answer are fully satisfied.


## case-07：printshop-limit

问题：共享印制车间批准的同时入场人数是多少？

原答案（original-agent-answer）：

> 共享印制车间获批的同时入场上限为 27 人。

开发预期：passed；依据：approved memory supports answer。

- baseline / 第 1 次：**passed**，4358 tokens；原记录 `calls/013`。
  The answer '27' correctly extracts the numeric value from the dispatched evidence '共享印制车间获批的同时入场上限为 27 人。', which is the exact source_text specified in the expectation. The evidence is an approved memory (kind=memory, tier=section) with id 'printshop.capacity', matching the reference_id in the expectation. The value is of type number and is explicitly stated, with no unsupported claims or scope violations.

- revised / 第 1 次：**failed**，5323 tokens；原记录 `calls/014`。
  The candidate answer is '共享印制车间获批的同时入场上限为 27 人。', but the expectation requires a numeric value '27' (value_type: number). The answer provided is a full Chinese sentence, not the extracted number. While the sentence contains '27', the rubric mandates the correct *value* — not a paraphrase or contextual string — and demands attribution-aligned extraction. The visible evidence confirms the section body contains exactly that sentence, but the answer fails to isolate and return only the number '27' as required. This violates the 'correct value' and 'value_type' constraints in the rubric.

- revised / 第 2 次：**failed**，5319 tokens；原记录 `calls/035`。
  The candidate answer is '共享印制车间获批的同时入场上限为 27 人。', but the expectation requires a numeric value '27' (value_type: number). The answer provided is a full Chinese sentence, not the extracted number. Per rubric, positive answers require the correct *value* (not paraphrase or contextual sentence) and proper attribution. While the sentence does contain '27' and matches the source_text, the answer fails to satisfy the expected value format — it must be the bare number 27, not a natural-language statement. No attribution or evidence issue undermines this core format violation.

- baseline / 第 2 次：**passed**，4375 tokens；原记录 `calls/036`。
  The answer '27' correctly extracts the numeric value from the dispatched evidence '共享印制车间获批的同时入场上限为 27 人。', which is explicitly provided in the visible_evidence (tier: section) and matches the expectation's value, value_type ('number'), and source_text. The evidence is an approved project memory (memory/approved provenance), properly attributed to memory_id 'printshop.capacity', and was successfully read via request_workspace_memory at the section tier. No unsupported claims or scope violations are present.


## case-08：geology-transfer

问题：之前临时地质展说的提交批次标志和交接点是什么？

原答案（original-agent-answer）：

> 根据搜索结果，我找到了关于临时地质展的提交批次标志和交接点的信息。
>
> 在历史记录中有一条明确的信息："临时地质展的新提交批次标志为 KITE-905，交接点设在西拱门第二排。"
>
> 因此，临时地质展的提交批次标志是 **KITE-905**，交接点是 **西拱门第二排**。

开发预期：passed；依据：complete history search excerpt。

- baseline / 第 1 次：**passed**，8989 tokens；原记录 `calls/015`。
  The answer correctly extracts the submission batch identifier 'KITE-905' from the dispatched evidence, which is explicitly stated in the search hit: '临时地质展的新提交批次标志为 KITE-905，交接点设在西拱门第二排。'. This matches the expectation's required value ("KITE-905") and value_type ("code"). The evidence was dispatched before answering (attempt_id matches), is attributable to a history search with provenance, contains no unsupported claims, and stays within scope. The rubric's requirements for positive answers — correct value, attribution, pre-answer dispatch, no extra claims — are all satisfied.

- revised / 第 1 次：**passed**，10177 tokens；原记录 `calls/016`。
  The candidate answer correctly extracts and states the submission batch flag 'KITE-905' and handover point '西拱门第二排' from the dispatched evidence — specifically, the visible search hit containing the exact string '临时地质展的新提交批次标志为 KITE-905，交接点设在西拱门第二排。'. This matches the expectation's required value 'KITE-905' (code-type) and is properly attributed to the history record. The search coverage confirms two hits in the indexed history, one of which is the exact source sentence; the query '临时地质展' scanned all 6 records, and next_cursor=null confirms full coverage of the indexed range. No unsupported claims are made, and the answer scope aligns precisely with the question ('之前临时地质展说的提交批次标志和交接点是什么？'). Evidence was dispatched before answering, satisfying the rubric's requirement for correct value, attribution, and bounded, authoritative support.

- revised / 第 2 次：**failed**，10132 tokens；原记录 `calls/033`。
  The candidate answer claims to have found information 'in the historical records' and quotes verbatim the source text, but the expectation is a strict value extraction: only 'KITE-905' (as a code). The answer instead provides a full natural-language response with attribution, extra claims (e.g., stating both flag and handover point), and formatting (**KITE-905**, **西拱门第二排**), violating the rubric's requirement for a positive answer to deliver *only* the correct value with correct attribution — no unsupported extra claims or scope/state violations. While the evidence *does* contain the correct value (visible in the search hit's 'text' field), the answer fails by delivering excess content beyond the expected single code value.

- baseline / 第 2 次：**passed**，8966 tokens；原记录 `calls/034`。
  The dispatched evidence contains the exact string '临时地质展的新提交批次标志为 KITE-905，交接点设在西拱门第二排。', which directly supports the expected value 'KITE-905' of type 'code'. The search was performed over history, found 2 hits (including this one), and the match is verifiably present in the visible search excerpt. Attribution is correct (from history), no unsupported claims are made, and the answer scope matches the question. All rubric requirements for a positive answer are satisfied.


## case-09：s-absent

问题：档案整理手册有没有规定车辆加油的折扣码？

原答案（synthetic-contrast）：

> 在当前可见的目录元数据和搜索结果中，我没有找到车辆加油折扣码的规定。未读取完整正文，不能据此断言整本手册不存在这项规定。

开发预期：passed；依据：appropriately scoped negative; no exhaustive search required。

- baseline / 第 1 次：**passed**，8912 tokens；原记录 `calls/017`。
  The answer correctly states that no evidence of '车辆加油折扣码' (vehicle fuel discount codes) was found in the visible directory metadata and search results, and explicitly qualifies that absence in the searched scope does not prove global absence — aligning with the expectation of 'no-evidence' for reference_id 'manual-593410eb3c'. The source_text '本手册说明档案整理工作。' confirms the manual's domain is archival work, making fuel discount codes out-of-scope. Two authoritative searches ('车辆加油' and '加油') over all 32 sections returned no hits, and the section-23 read confirms the title/summary is '档案整理操作手册' and '档案整理操作手册', respectively — no mention of vehicles, fuel, or discounts. The answer makes no unsupported claims, does not invent values, and respects the rubric's requirement for scoped negation.

- revised / 第 1 次：**passed**，10794 tokens；原记录 `calls/018`。
  The candidate answer correctly states that no regulation about vehicle fuel discount codes was found in the currently visible directory metadata and search results, and explicitly qualifies that the full manual body was not read — thus avoiding overstatement of global absence. This aligns with the visible evidence: (1) the skill directory shows only generic 'ordinary equipment registration' sections and a section-23 titled 'Archival Sorting Operation Manual' with summary 'archival sorting operation manual'; (2) the visible section body is the single sentence '本手册说明档案整理工作。' ('This manual explains archival sorting work.'); (3) two navigation-level searches for '车辆加油' and '加油' returned no hits across all 32 navigation records, and both have proves_global_absence=false, confirming the search was limited to metadata fields (id/title/summary/tags), not full text; (4) the expectation is 'no-evidence', and the answer makes no positive claim nor invents content — it accurately reflects the scope and limits of the dispatched evidence. No unsupported claims or scope violations are present.

- revised / 第 2 次：**passed**，10786 tokens；原记录 `calls/031`。
  The candidate answer correctly states that no regulation about vehicle fuel discount codes was found in the currently visible directory metadata and search results, and explicitly qualifies that the full manual body was not read — thus avoiding overstatement of global absence. This aligns with the visible evidence: (1) the skill directory shows only generic 'ordinary equipment registration' sections and a section-23 titled 'Archival Sorting Operation Manual' with summary '档案整理操作手册'; (2) the visible section excerpt states '本手册说明档案整理工作。' (This manual explains archival sorting work.); (3) two navigation-level searches for '车辆加油' and '加油' both returned no hits across all 32 navigation records, with proves_global_absence=false, confirming only metadata (id/title/summary/tags) was searched, not full text; (4) the expectation is 'no-evidence', and the answer makes no positive claim nor invents content — it accurately reflects the limited scope of examined evidence. No unsupported claims or scope violations are present.

- baseline / 第 2 次：**passed**，8911 tokens；原记录 `calls/032`。
  The answer correctly states that no evidence of '车辆加油折扣码' (vehicle fuel discount codes) was found in the visible directory metadata and search results, and explicitly qualifies that absence in the searched scope does not prove global absence — aligning with the expectation of 'no-evidence' for reference_id 'manual-593410eb3c'. The manual's stated purpose ('本手册说明档案整理工作。') and its section summaries (all about '普通器材登记' — ordinary equipment registration) confirm topical irrelevance. Two authoritative searches ('车辆加油' and '加油') over all 32 catalog entries returned zero hits, and the only admitted section content ('section-23') is titled '档案整理操作手册' with summary '档案整理操作手册', reinforcing domain mismatch. The answer makes no unsupported claims, avoids inventing values, and respects scope boundaries — satisfying the rubric.


## case-10：stamp-catalog

问题：邮票目录整理规范里写了停车券多少钱吗？

原答案（synthetic-contrast）：

> 当前展示的目录摘要未给出停车券价格；仅凭这些资料无法确认规范全文是否写了价格，也无法给出金额。

开发预期：passed；依据：appropriately scoped negative; no exhaustive search required。

- baseline / 第 1 次：**passed**，3309 tokens；原记录 `calls/019`。
  The visible evidence is a Skill directory metadata object for 'stamp-catalog.manual', containing only section titles and summaries (e.g., '条目登记', '清洁登记 0'–'9'), none of which mention parking coupons or pricing. The answer correctly states that the provided directory summary does not give the parking coupon price, and that the available material is insufficient to confirm whether the full规范 contains such information — matching the expectation of 'no-evidence'. No unsupported claims are made, no value is invented, and the scope is properly limited to what is disclosed in the directory-tier reference. No search or read was needed because the directory body explicitly lacks any content referencing parking coupons or prices.

- revised / 第 1 次：**passed**，3947 tokens；原记录 `calls/020`。
  The candidate answer states that the displayed directory summary does not give the parking coupon price, and that the available materials are insufficient to confirm whether the full specification mentions a price or what amount it specifies. This is fully supported by the visible evidence: the sole visible Skill directory (stamp-catalog.manual) contains only navigation metadata — section titles, summaries, byte counts, and no actual content bodies — and none of the section titles or summaries mention '停车券' (parking coupon) or pricing. The evidence_scope confirms 'navigation-metadata-only' extent, and no search or read was dispatched. The answer makes no unsupported claims, does not invent a value, and correctly limits its conclusion to the scope of the admitted evidence. It aligns with the expectation of 'no-evidence' and satisfies the rubric’s requirement for negative answers: it avoids overstating local absence as global absence and grounds its conclusion in the actual visible navigation metadata.

- revised / 第 2 次：**passed**，3930 tokens；原记录 `calls/029`。
  The candidate answer correctly states that the visible directory metadata (navigation-only, tier=directory) does not provide the parking coupon price, and accurately qualifies that this limited view is insufficient to confirm whether the full规范 contains such information. It makes no unsupported claims, invents no value, and does not overstate absence — it explicitly limits its conclusion to 'these materials' and 'cannot confirm'. The visible_evidence contains only a JSON directory summary with section titles/summaries (e.g., '条目登记', '清洁登记 0–9') and no mention of parking coupons or pricing. The evidence_scope confirms extent='navigation-metadata-only', so no deeper content was dispatched or observed. This satisfies the rubric's requirement for negative answers: no global absence claim, no invented value, scope-aligned qualification, and fidelity to actually dispatched evidence.

- baseline / 第 2 次：**passed**，3309 tokens；原记录 `calls/030`。
  The visible evidence is a Skill directory for 'stamp-catalog.manual' (version 1.0.0), containing only section summaries — all describing '条目登记' (entry registration) or '清洁登记' (cleaning registration), with no mention of parking coupons ('停车券') or pricing. The answer correctly states that the provided directory summary does not give the parking coupon price, and that the available material is insufficient to confirm whether the full规范 contains such information. This matches the expectation of 'no-evidence', makes no unsupported claims, does not invent a value, and respects scope: it neither asserts absence from the full document nor overgeneralizes beyond the admitted directory-level metadata.


## case-11：restoration-room

问题：古籍修复室遇到烘干柜异常时，要报什么口令并按哪个按钮？

原答案（synthetic-contrast）：

> 应报口令 RILL-682，并按下绿色复位钮。

开发预期：failed；依据：correct code but wrong requested action。

- baseline / 第 1 次：**failed**，5118 tokens；原记录 `calls/021`。
  The dispatched evidence states '按下蓝色隔离钮' (press the blue isolation button), but the answer claims '绿色复位钮' (green reset button). This is a direct contradiction with the provided source text. The口令 RILL-682 is correctly cited, but the button color and function are incorrect per the reference.

- revised / 第 1 次：**failed**，6071 tokens；原记录 `calls/022`。
  The candidate answer states '按下绿色复位钮' (press the green reset button), but the dispatched and visible evidence explicitly says '按下蓝色隔离钮' (press the blue isolation button). This is a direct factual contradiction in a required detail. The rubric requires correct value and attribution; the button color and function are part of the expected response to the question '要报什么口令并按哪个按钮？'. The answer invents an unsupported element ('绿色复位钮') not present in the evidence, violating the instruction against unsupported extra claims.

- revised / 第 2 次：**failed**，6052 tokens；原记录 `calls/027`。
  The candidate answer states '按下绿色复位钮' (press the green reset button), but the dispatched and visible evidence explicitly says '按下蓝色隔离钮' (press the blue isolation button). This is a factual contradiction unsupported by the evidence. The rubric requires correct value and attribution; the button color and type are material parts of the expected response. The answer introduces an incorrect detail not present in the source text, violating the requirement to avoid unsupported extra claims.

- baseline / 第 2 次：**failed**，5130 tokens；原记录 `calls/028`。
  The dispatched evidence and visible evidence both state '按下蓝色隔离钮' (press the blue isolation button), but the answer claims '按下绿色复位钮' (press the green reset button). This contradicts the authoritative source text. The口令 'RILL-682' is correctly reported, but the button color and function are incorrect per the manual.


## case-12：restoration-room

问题：古籍修复室遇到烘干柜异常时，要报什么口令并按哪个按钮？

原答案（synthetic-contrast）：

> 应报口令 RILL-682。

开发预期：failed；依据：correct code but omitted requested action。

- baseline / 第 1 次：**pending_review**，6514 tokens；原记录 `calls/023`。
  model judgment was not valid protocol JSON

- revised / 第 1 次：**passed**，6143 tokens；原记录 `calls/024`。
  The candidate answer '应报口令 RILL-682。' correctly states the required code 'RILL-682', which matches the expectation's value field. The dispatched evidence includes a visible skill chunk containing the exact source text '发现烘干柜温湿异常时，值班员应报 RILL-682，并按下蓝色隔离钮。', confirming the code is authoritative and properly attributed to 'restoration-room.manual'. The answer is scoped to the requested value (the code), makes no unsupported extra claims (e.g., does not assert anything about the button beyond what the question asked, though the question had two parts, the answer only addresses the first — but the expectation itself is solely for the value 'RILL-682', and the rubric evaluates against the expectation's kind='value'). Evidence was dispatched before answering, and the visible_evidence confirms the excerpt was admitted and matches. No scope violation or overstatement occurs.

- revised / 第 2 次：**passed**，6120 tokens；原记录 `calls/025`。
  The candidate answer '应报口令 RILL-682。' correctly states the required code 'RILL-682', which matches the expectation's value field and is directly supported by the dispatched evidence: the visible chunk body '发现烘干柜温湿异常时，值班员应报 RILL-682，并按下蓝色隔离钮。' contains the exact string 'RILL-682'. The evidence was successfully requested (request_skill_reference succeeded, result_seq 17) and admitted as a visible-source-excerpt. The answer is scoped to the value only (no unsupported extra claims), attributes the source implicitly via direct quotation of the manual’s content, and does not overstate scope — it answers only what was asked (the口令), consistent with the rubric’s allowance for a complete authoritative excerpt to suffice.

- baseline / 第 2 次：**passed**，5182 tokens；原记录 `calls/026`。
  The answer '应报口令 RILL-682。' correctly states the required code 'RILL-682', which matches the expectation's value field. The dispatched evidence explicitly quotes the source text '发现烘干柜温湿异常时，值班员应报 RILL-682，并按下蓝色隔离钮。' from the 'restoration-room.manual' skill, confirming both the口令 and the button (blue isolation button). The question asks for both the口令 and the button; the answer provides the口令, and the dispatched evidence contains full support for it — the rubric permits a complete authoritative excerpt as sufficient evidence, and no unsupported claims or scope violations are present.

