# Media Directory Organizer

这是一个独立 feature 插件。v4 读取 `collection-detail` 作品数据库，对作品父目录中的待整理一级目录逐文件识别作品、压制格式和压制组，再生成可审阅的移动计划。同一个来源目录可以拆成多条路由并写入多个数据库目标；Nimda 的“目录整理”页签支持修正每条路由的目标、为无法自动判断的单个文件人工选择作品，以及在数据库没有作品时预览一次完整落地。

## v4 目录与路由规则

1. 作品父目录来自作品 YAML 的 `collection-type.data.path`，系列作品由同一路径和作品名关系共同确定。
2. 目标压制目录优先严格采用对应 `collectioned[].press_path`。数据库未填写 `press_path` 时，才按 `<作品名>_<压制格式>` 推导；同格式存在多个压制组时追加组后缀。
3. 识别单位是文件而不是整个来源目录。文件名命中作品名或配置的 `classification.work_aliases` 后，会路由到对应作品；因此一个混合发布包可以拆到多个作品目标。
4. 目标内布局由“通用分类器 → 压制组派生类”决定：
   - 通用分类为 `Disc`、`CD`、`OP+ED`、`Image`、`Menu`、`CM`、`PV`、`Preview`、`SP`、`Fonts`、`Subs`、`MV`、`Live`、`Others`。
   - `JsumClassifier` 与 `VcbClassifier` 从通用类派生；媒体组注册表中 `classifier_family: VCB` 的发布共用同一个 VCB 分类器，包括 `VA`、`VAF`、`VAL`、`VTL`、`VCDM` 等不以 VCB 开头的组合简称。数据库真实组码和 `press_path` 不会合并。
   - 每个分类文件夹都有独立、可覆写的 `filter_disc()`、`filter_cd()`、`filter_op_ed()` 等虚函数。后续可按压制组、作品名或来源描述只修正一个 Filter，并通过 `super()` 保留通用规则。
   - 分类器只返回“分类 + 分类内相对路径”。父压制目录仍使用完整 `press_path`；分类子目录去掉末尾已确认的压制组，例如 `Little Busters! EX_BDRip(VCBM)\Little Busters! EX_BDRip_CD`。
   - 目录上下文优先于扩展名，分类内的专辑/卷/Booklet 层级会保留；所有 Filter 都未命中的文件进入 `Others`。
5. 无法唯一确定作品的文件会进入 `unresolved_files` 并阻止执行。桌面页可为每个未决文件选择数据库作品，随后必须重新预览。
6. 所有计划都保留原文件名。目标已存在、计划内重名、越界路径、符号链接/目录联接、识别歧义或扫描状态变化都会阻止执行。

`press_group` 是数据库组码，目标目录名则由 `press_path` 决定。两者不要求字面相同；例如 Little Busters! 的协作包数据库组码仍为 `VCB`，目标目录按既定命名使用 `(VCBM)`。

## Little Busters! 当前状态

`U:\Little Busters!` 现由数据库独立描述三个作品：

- `Little Busters!`
- `Little Busters! Refrain`
- `Little Busters! EX`

2012 年本篇已有目录保持不变：

- `Little Busters!_BDRip(Jsum)`
- `Little Busters!_BDRip(MW)`

Refrain 与 EX 分别拥有 JSUM 和 VCB 两条 BDRip 记录，四个权威目标为：

- `Little Busters! Refrain_BDRip(Jsum)`
- `Little Busters! Refrain_BDRip(VCBM)`
- `Little Busters! EX_BDRip(Jsum)`
- `Little Busters! EX_BDRip(VCBM)`

v4 会逐文件拆分 `[2013-14][Little Busters! Refrain+EX][BDRIP][1080P][1-21Fin+SP]`，将明确属于 Refrain 或 EX 的 JSUM 文件分别送入两个 JSUM 目标。两个 VCB-Studio 来源也会分别进入 Refrain/EX 的 `(VCBM)` 目标，并按 `_Disc`、`_CD`、`_Image`、`_Menu`、`_OP+ED`、`_Others` 等分类目录组织内部布局。

发布包中的共用 Logo 文件同时包含 Refrain 与 EX 标识，自动识别无法唯一决定其作品归属。它会作为未决文件显示在桌面页；必须人工选择 Refrain 或 EX，再点击“重新预览”生成新计划。未解决该文件前，计划不可执行。

## 桌面版使用与安全确认

打开“目录整理”页签后，应用按插件配置中的 `paths.default_work_root` 重新扫描。先核对每条“来源 → 作品 → 压制组 → 目标”路由，以及逐文件布局和所有未决文件。

修改路由目标或未决文件的作品归属会立即使当前计划失效；必须重新预览并取得新的计划 ID。任何实际移动都要依次完成：

1. 确认来源、目标、路由数、文件数、总大小、fallback 数和未决数摘要。
2. 完整输入本次预览的 16 位计划 ID。
3. 通过最终移动确认。

服务端随后仍会重新扫描数据库和文件系统、重建计划，并校验 `acknowledge_move`、预览计划 ID、输入确认码与新计划 ID 完全一致。浏览器提交的移动列表不会被直接执行；任一状态发生变化都会拒绝移动。

### 数据库没有作品时

普通预览不会再直接报错。页面会显示作品名、日期、类型、国家、发行类型，以及每个来源一级目录的压制格式、组简称和 `press_path`。点击“生成完整预览”只读计算以下三个阶段：

1. 按国家与开始年份新增作品 YAML（例如 `[JP][TVInfo][2014].yaml`、`[KR][TVInfo][2025].yaml`）。
2. 按逐文件分类计划整理所有已分配的来源一级目录。
3. 只为该作品增量创建快捷方式并刷新索引数据库，不清空或重建其他作品的快捷方式。

三个阶段共用一个完整落地确认 ID；执行前必须同时确认数据库写入、媒体移动和快捷方式。数据库写入后若媒体移动失败，新增记录会回滚。媒体移动已经完成但快捷方式失败时，页面会进入“快捷方式待补建”，后续重试只校验数据库和目标目录并创建该作品快捷方式，不会重新扫描来源或再次移动媒体。

## 媒体组注册表

`data/features/media-group-registry/db/groups.yaml` 由 `data/source/Animation/Note.h` 与现有作品目录数据库共同生成，分别保存压制组、翻译/字幕组、2–4 组组合简称和仅在旧目录数据库中出现的组码。作品 YAML 继续使用兼容字段 `press_group` 保存原子组码或组合简称。

当前注册表共 242 个全局唯一 code。`VCBE` 的历史 `VCP` 和 `FLTG` 的 `TGTDS` 会归一化为已有成员，同时保留原始行、修正记录和 `review_required`；`MM`、`TUC` 的重复定义也保留来源，等待人工复核。重新生成命令：

```powershell
D:\SoftIDE\Python\python.exe .\scripts\import-media-groups.py
```

## 命令行

只预览，不移动：

```powershell
.\scripts\organize-media-directories.cmd preview --root "U:\Little Busters!"
```

查看每一个文件的源路径和目标路径：

```powershell
.\scripts\organize-media-directories.cmd preview --root "U:\Little Busters!" --details
```

只有计划为可执行状态时，核对输出中的计划 ID 后才能执行：

```powershell
.\scripts\organize-media-directories.cmd apply --root "U:\Little Busters!" --confirm <计划ID>
```

`apply` 会重新扫描并重新计算计划 ID。文件、大小、修改时间、数据库、分类规则或人工修正只要在预览后发生变化，旧 ID 就无法通过确认。`--json` 可输出 UI/API 可直接消费的结构化结果；多个 `--source "一级子目录名"` 可限制参与规划的来源目录。当前命令行不提供未决文件的交互式作品选择；Little Busters! 的共用 Logo 应在桌面页完成归属选择并重新预览。
