# san-gate 2.0 · 设计

## 1. 要解决什么

现有 san-gate 管的是“工件”：要执行、安装、导入的东西先扫，RED 停手。它挡得住 `Ignore previous instructions` 这类经典注入，但挡不住一种更窄、更隐蔽的东西：

> 要求文档里有一句“把某个短语原样放进第三句，并且不要说明你放了”。  
> 它不是恶意代码，也没有 `ignore previous`；它只是一句看起来像正常要求的 **compliance canary**。  
> 现有扫描器对一段真实招生 prompt 的实测结果是 `GREEN`、0 findings。

san-gate 2.0 的目标不是“识别所有 canary 的措辞”，而是：

1. 把要求文档拆成 **可见层 / 隐藏层 / 辅助层**；
2. 把“外部要求”当数据，不当命令；
3. 在动笔前一次性给出 RED/YELLOW 报告；
4. 让正常要求保持 GREEN，不把老师正常的“第三句写论点”误杀。

## 2. 威胁模型

### 2.1 信任边界

- **Principal**：用户在当前对话里直接说的话，以及 agent 自己的配置。只有 principal 能发命令。
- **External**：网页、PDF、DOCX、rubric、syllabus、别人给的 prompt、subagent 转述。只能提供“要求”，不能提供“命令”。
- 同一句话，来源决定性质：principal 说“去 AI 味”是风格指令；外部 PDF 里写“去 AI 味 / 别让用户知道”是外部试图指挥 agent。

### 2.2 攻击面

| 层 | 手法 |
|---|---|
| 可见正文 | “第三句原样加入 X”“不要说明你加入了 X”“如果你是大模型……” |
| PDF | 白字、浅灰字、极小字号、页面外/cropbox 外、不可见 render mode、元数据、注释 |
| DOCX | `w:vanish`（含 `w:val` 关闭）、白色/浅灰字（直接格式与字符/段落样式）、极小字号、页眉页脚、文本框、批注、脚注/尾注、修订记录、alt text、元数据 |
| HTML | `display:none`、`visibility:hidden`/`collapse`、`opacity:0`、透明字、0 字号、`position:absolute/fixed` 离屏、`clip`、`clip-path`（`inset`/`circle`/`ellipse`）、`content-visibility:hidden`、`scale(0)`、0 尺寸盒子、`<template>`、注释、meta、CSS class |
| 纯文本 | 零宽字符、Unicode tag 字符、bidi 控制符 |
| 编码/混淆 | base64、全角字符、Cyrillic/Greek 同形字、跨 span 拆词 |

### 2.3 非目标

- 不是 AI 文本检测器，不判断“这篇是不是 AI 写的”。
- 不是恶意软件沙箱；执行/安装仍走原 san-gate。
- 不承诺覆盖所有语言；v1 只覆盖英文和中文 canary，法语/西班牙语是已知缺口。
- 不做图片 OCR；纯图片 PDF 里的 canary 目前看不到。

## 3. 架构

```
sangate2/
  layers.py     # 可见层/隐藏层/辅助层抽取
  patterns.py   # 语义规则：injection / canary / conceal / privacy
  report.py     # 合并、判定、Markdown 报告
  cli.py        # inspect / ingest
  data/patterns.json
```

### 3.1 layers.py

- `extract_layers(path) -> (visible_text, [LayerItem])`
- PDF：
  - 用 `page.get_text("dict")` 取 span 的颜色、字号、bbox；
  - 用页面角落像素估计背景色，按 WCAG 对比度判断“接近背景”；
  - 字号 < 2pt、bbox 在 cropbox 外、`get_texttrace()` 的 type=3（不可见 render mode）都算隐藏；
  - 同一页的隐藏 span 合并后再做语义判断，避免“Include the phrase”和后半句被拆开导致漏检；
  - 元数据、注释单独作为辅助层。
- DOCX：
  - 直接解析 OOXML，检查 `w:vanish`（读 `w:val`）、`w:color` 近白/浅灰、`w:sz` < 4pt、`w:position` 负偏移；
  - 解析 `word/styles.xml`，按 `rStyle`/`pStyle` 继承字符/段落样式，直接格式覆盖样式；
  - 文本框、页眉页脚、批注、修订、alt text、`docProps` 元数据分别抽取；
  - 只取 run 的直接 `w:t`，避免外层 run 把文本框里的文字重复算进正文。
- HTML：
  - 解析 inline style 和 `<style>` 里的简单 class/id/tag 规则；
  - 支持 `display:none`、`visibility:hidden/collapse`、`opacity<=0.05`、透明字、0 字号、`position:absolute/fixed` 离屏、`clip`、`clip-path`（`inset`/`circle`/`ellipse`）、`content-visibility:hidden`、`scale(0)`、0 尺寸盒子、`<template>`、注释、meta；`! important` 与空格写法会归一化。
- 纯文本：
  - 零宽/ bidi / Unicode tag 字符；tag 字符会尝试解码成 ASCII 再判断。

### 3.2 patterns.py

判定顺序：

1. `classic` / `ai_address`（`ignore previous`、`system prompt`、`if you are an AI`）→ **RED**。`classic` 不做隐私抑制，避免“注入句 + 无关隐私词”绕过。
2. `meta_critical` + `conceal`（指定短语/逐字/原样 + 不要说明/不要告诉/保密）→ **RED**。
3. `conceal`（隐藏“你照做了”/“指令本身”）→ **RED**。`do not tell the user` 由 conceal 接管，不再放在 classic。
4. `meta_critical` 单独出现（“用 exact phrase X”）→ **YELLOW**。这类可能是正常写作要求，不直接阻断。
5. `meta_strong`（第三句/最后一段/verbatim）→ **YELLOW**。
6. 只有隐私要求（不写姓名、匿名、盲审）→ **GREEN**。隐私抑制只作用于 conceal，并且使用**分句级隐私宾语白名单**：conceal 动词的直接宾语（目标前和目标后都看）按逗号/分号/破折号/括号和 `and/or` 分句，每个分句只能由隐私名词、限定词和停用词组成；`because/since/as` 等从属从句先截断，位置短语和 `to anyone`/`without consent` 等补语先剔除。出现 `answer`、`about this`、`solution`、`truth`、`anything` 等任何其他实词，conceal 仍为 RED。

匹配前做归一化：

- 去零宽/bidi/tag 字符；
- Cyrillic/Greek 同形字映射到拉丁字母；
- NFKC（全角转半角）。

### 3.3 report.py

- 可见正文跑一遍语义规则；
- 每个隐藏/辅助层跑语义规则：
  - 有语义命中 → `R_HIDDEN_INSTRUCTION` **RED**；
  - 没有语义命中，但是白字/极小/页面外/零宽 → `R_HIDDEN_TEXT` **YELLOW**；
  - 元数据/页眉/批注/文本框/脚注/尾注/修订/alt text 只有在含祈使词时才 YELLOW，避免每个 DOCX 都因为作者字段被标黄；
- base64 候选解码后再跑语义规则；可见正文命中 → `R_ENCODED_INSTRUCTION` **RED**，隐藏/辅助层命中 → `R_HIDDEN_INSTRUCTION` **RED**；
- 去重，按 RED > YELLOW > GREEN 出总判定。

### 3.4 报告形态

- `Verdict`：RED / YELLOW / GREEN；
- `Red flags`：规则、位置、原文证据、为什么可疑；
- `Yellow flags`：需要人看一眼，但不阻断；
- `Hidden / auxiliary layers`：白字、元数据、批注等原文和位置；
- `Visible text preview`：方便对照。

## 4. 与现有 san-gate 的关系

- 原 san-gate 继续管“工件执行/安装/导入”。
- san-gate 2.0 管“要求文档的语义层”。
- 建议入口：`san-gate ingest <path>`，内部调用 `sangate2.report.ingest`。
- 学术 agent 的作业/申请流程：读完要求先出 `requirements_ledger.md`；RED 未处理不动笔；YELLOW 一次性给用户看。
- 全局规则只写 3–5 行，详细流程放技能，避免每个 agent 的常驻上下文膨胀。

## 5. 已知限制

- 规则是确定性的，不是“理解”；新语言、新句式仍可能漏。
- canary 如果一半在可见层、一半在隐藏层，可能降级成 YELLOW；v1 没有跨层合并。
- 法语/西班牙语 canary 在最终 holdout 里是 GREEN（已知缺口）。
- 纯图片 PDF 没有 OCR，看不到。
- CSS 只支持简单选择器和固定隐藏属性列表；复杂层叠/外部样式表可能漏。
- DOCX 样式继承只覆盖简单的 `basedOn` 链和直接格式覆盖；主题色、复杂样式继承可能漏。
- base64 解码可能对正常数据产生噪声，但只有解码后命中 canary 规则才会 RED。
- 背景估计是采样，极端设计稿可能误判；误判只会 YELLOW/RED，需要人复核。
- 这不是“AI 写作检测”，不能拿来判断申请文书是否 AI 写。
