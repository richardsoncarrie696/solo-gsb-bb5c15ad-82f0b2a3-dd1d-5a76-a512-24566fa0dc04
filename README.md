# 本地字幕切分 API（Python + FastAPI）

接收**按时间排序、互不重叠的词级转写**（每个词含文字与起止毫秒），用**动态规划**寻找覆盖全部词、保持原有顺序的合法字幕切分，并支持 SRT 导出。另提供**视频删段重排**接口：给定原视频上的删除区间，丢弃被完整覆盖的词、将保留词按累计删除时长前移，再对各连续片段分别优化切分；以及**遮挡感知的位置决策**接口：给定视频时间轴上的遮挡区间（`top` / `bottom` 区域），联合优化分条与字幕摆放位置（相邻字幕换区加 1 分），并导出带 cue 行位置的 WebVTT。

- 优化目标（总代价）：

  ```
  Σ (该条字幕时长_秒 − 目标时长_秒)²
      + 4 ×（被字幕跨过的句末标点 / 长停顿边界数量）
      + 双行排版代价（仅当传入 max_line_chars 时）
  ```

  - 两个相邻词之间，若前一个词以句末标点（`. ! ? … 。 ！ ？`，允许后面再跟引号/括号）结尾，或两词间隔 ≥ `pause_threshold_ms`，即为一个**自然边界**；
  - 自然边界落在字幕内部（而不是字幕边缘）就算“被跨过”，每条加 4 分；
  - **双行排版（可选）**：传入正整数 `max_line_chars` 后，每条字幕至多两行，换行只能发生在**词与词之间**，每行长度按既有中西文拼接规则计算（包含词间空格；换行处的空格由换行符取代，不计入行宽），且每行不得超过该行宽。单行放得下时保持单行、排版代价为 0；否则枚举所有合法断行，取

    ```
    (第一行长度 − 第二行长度)² / max_line_chars²
    ```

    的最小值，代价相同取**更靠前**的断点；不存在合法双行排版的候选字幕不可选；
  - **总代价平局时，选择字幕条数更少的方案。**

- 硬约束（违反则不存在合法切分，返回 `422`）：
  - 每条字幕字数 ≤ `max_chars`（沿用原有统计口径）；
  - 每条字幕时长 ∈ [`min_duration_ms`, `max_duration_ms`]（含最后一条）；
  - 传入 `max_line_chars` 时，每条字幕必须能排成不超过两行、每行不超过该行宽；
  - **人工编辑边界（可选，两个字段可同时使用）**：
    - `required_break_after`：词下标 `k` 的数组，**必须**在第 `k` 个词与第 `k+1` 个词之间断开（即该处必须是某条字幕的边缘）；
    - `keep_together`：词下标闭区间 `[lo, hi]` 的数组，区间**内部不得断开**（词 `lo..hi` 必须落在同一条字幕里），但区间左右外缘仍可断开；
  - 全部词必须且只能出现一次，顺序不变。

> 下标均以本次请求 `words` 的**零基**顺序计。`required_break_after` 只能取 `0 .. 词数-2`（最后一个词之后不存在边界）；每个 `keep_together` 区间至少含两个词（`lo < hi`）；区间两两不得重叠（端点相接允许，如 `[0,1]` 与 `[2,3]`）；必断下标不得等于某区间的 `lo`、也不得落在区间内部（落在右外缘 `hi` 允许——此时整个区间作为该字幕的结尾）。下标非法或两类约束相互冲突均返回 `422`。
>
> 切分器在上述全部硬约束（时长、字数、双行行宽、人工边界）下**全局**寻找最低代价方案；若人工边界与其他硬约束矛盾导致没有合法切分，同样返回 `422`。两个端点 `/segment` 与 `/segment/srt` 共用同一次切分结果。
>
> 不传这两个字段（或传空数组）时，切分逻辑、代价与输出格式与旧版完全一致；不传 `max_line_chars` 时始终单行。

---

## 一键启动（Docker Compose）

前置条件：已安装 Docker（含 Compose v2）。

```bash
docker compose up --build
```

启动后访问：

| 用途 | 地址 |
| --- | --- |
| 服务根信息 | http://localhost:8000/ |
| 健康检查 | http://localhost:8000/health |
| Swagger 交互文档 | http://localhost:8000/docs |
| OpenAPI 描述 | http://localhost:8000/openapi.json |

端口固定为 **8000**，在 `docker-compose.yml` 的 `ports` 中映射；如需改端口，例如改成 9000：

```yaml
ports:
  - "9000:8000"
```

后台运行：`docker compose up --build -d`；停止：`docker compose down`。

---

## 接口

### `POST /segment` —— JSON 切分

请求体：

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `words` | 数组 | 必填（至少 1 个） | 词级转写，按时间排序且互不重叠 |
| `words[].text` | string | 必填 | 词文本，非空 |
| `words[].start_ms` | int | 必填 | 起始毫秒，≥ 0 |
| `words[].end_ms` | int | 必填 | 结束毫秒，须 > `start_ms` |
| `max_chars` | int | `42` | 单条字幕字数上限，≥ 1 |
| `min_duration_ms` | int | `1000` | 单条字幕最短时长（毫秒） |
| `max_duration_ms` | int | `7000` | 单条字幕最长时长（毫秒） |
| `target_duration_ms` | int | `3000` | 目标时长（毫秒），代价按秒计算 |
| `pause_threshold_ms` | int | `500` | 词间间隔达到该值即视为长停顿边界 |
| `max_line_chars` | int | 不传 | 可选正整数；双行排版行宽（≥ 1）。传入后每条字幕至多两行、仅在词间断行、每行（含词间空格）不超过该行宽；不传则保持单行旧行为 |
| `required_break_after` | int 数组 | 不传 | 可选；必须在指定词下标之后断开。下标零基，取值 `0 .. 词数-2`，重复值自动去重 |
| `keep_together` | `[lo, hi]` 数组 | 不传 | 可选；词下标闭区间，内部（`lo..hi-1` 之间的边界）不得断开。每段至少含两个词（`lo < hi`），下标在 `0 .. 词数-1` 内；区间两两不重叠（允许端点相接），且不得与 `required_break_after` 冲突 |

> 约束：`max_duration_ms >= min_duration_ms`、`max_line_chars >= 1`；参数省略时使用环境变量配置的默认值（`max_line_chars`、`required_break_after`、`keep_together` 无环境变量默认值）。
>
> **人工边界示例**：9 个词中下标为 `0:Hello 1:world. 2:How 3:are 4:you 5:doing 6:today? 7:Fine 8:thanks.`。要求 `Hello` 单独成条、问候句在 `today?` 后断开，并把 `How are you` 锁在同一条：
>
> ```json
> "required_break_after": [0, 6],
> "keep_together": [[2, 4]]
> ```
>
> 可用 `examples/request_manual_edits.json` 复现：
>
> ```bash
> curl -X POST http://localhost:8000/segment \
>   -H 'Content-Type: application/json' \
>   -d @examples/request_manual_edits.json
> ```
>
> ```json
> {
>   "segment_count": 4,
>   "total_cost": 14.06,
>   "segments": [
>     { "index": 0, "text": "Hello", "start_ms": 0, "end_ms": 500,
>       "duration_ms": 500, "word_start": 0, "word_end": 0, "crossed_boundaries": 0 },
>     { "index": 1, "text": "world.", "start_ms": 500, "end_ms": 1400,
>       "duration_ms": 900, "word_start": 1, "word_end": 1, "crossed_boundaries": 0 },
>     { "index": 2, "text": "How are you doing today?", "start_ms": 2000, "end_ms": 4600,
>       "duration_ms": 2600, "word_start": 2, "word_end": 6, "crossed_boundaries": 0 },
>     { "index": 3, "text": "Fine thanks.", "start_ms": 5200, "end_ms": 6400,
>       "duration_ms": 1200, "word_start": 7, "word_end": 8, "crossed_boundaries": 0 }
>   ]
> }
> ```
>
> 注意 `required_break_after: [0]` 让 `Hello`（500 ms）单独成条，因此该请求把 `min_duration_ms` 放宽为 `0`；若仍用默认 `1000`，没有合法方案，返回 `422`。`keep_together: [[2,4]]` 只禁止词 2/3、3/4 之间的断点，词 4 之后（右外缘）照常断开。

请求示例：

```bash
curl -X POST http://localhost:8000/segment \
  -H 'Content-Type: application/json' \
  -d @examples/request.json
```

响应示例（`200`）：

```json
{
  "segment_count": 3,
  "total_cost": 5.96,
  "segments": [
    {
      "index": 0,
      "text": "Hello world.",
      "start_ms": 0,
      "end_ms": 1400,
      "duration_ms": 1400,
      "word_start": 0,
      "word_end": 1,
      "crossed_boundaries": 0
    },
    {
      "index": 1,
      "text": "How are you doing today?",
      "start_ms": 2000,
      "end_ms": 4600,
      "duration_ms": 2600,
      "word_start": 2,
      "word_end": 6,
      "crossed_boundaries": 0
    },
    {
      "index": 2,
      "text": "Fine thanks.",
      "start_ms": 5200,
      "end_ms": 6400,
      "duration_ms": 1200,
      "word_start": 7,
      "word_end": 8,
      "crossed_boundaries": 0
    }
  ]
}
```

`word_start` / `word_end` 为该字幕覆盖的词下标（含端点）；`crossed_boundaries` 是该字幕内部跨过的自然边界数（理想切分通常为 0）。

传入 `max_line_chars` 后，响应中的 `text` 用换行符 `\n` 分隔两行，单行字幕不含换行符。可用 `examples/request_two_lines.json` 复现（对 4 个等长拉丁词传入 `"max_line_chars": 7`）：

```bash
curl -X POST http://localhost:8000/segment \
  -H 'Content-Type: application/json' \
  -d @examples/request_two_lines.json
```

```json
{
  "index": 0,
  "text": "aaa bbb\nccc ddd",
  "start_ms": 0,
  "end_ms": 1800,
  "duration_ms": 1800,
  "word_start": 0,
  "word_end": 3,
  "crossed_boundaries": 0
}
```

- 单行放得下 → 单行输出，排版代价 0；
- 否则只在词间断行，每行 ≤ `max_line_chars`，优先让两行等长（平局取靠前断点）；
- 中西文混排仍沿用原拼接规则：如 `你好 世界` 这类无空格相邻关系，断行处同样不补空格（例：`你好\n世界`）；
- 原有 `max_chars` 与时长约束继续生效，任何候选没有合法双行排版时不可选，整体无合法切分返回 `422`。

### `POST /segment/srt` —— SRT 导出

请求体与 `/segment` 完全相同，返回 `text/plain; charset=utf-8` 的 SRT 文档：

```bash
curl -X POST http://localhost:8000/segment/srt \
  -H 'Content-Type: application/json' \
  -d @examples/request.json
```

```srt
1
00:00:00,000 --> 00:00:01,400
Hello world.

2
00:00:02,000 --> 00:00:04,600
How are you doing today?

3
00:00:05,200 --> 00:00:06,400
Fine thanks.
```

保存到文件：

```bash
curl -X POST http://localhost:8000/segment/srt \
  -H 'Content-Type: application/json' \
  -d @examples/request.json -o subtitles.srt
```

传入 `max_line_chars` 时，SRT 字幕块内用真实换行输出两行：

```srt
1
00:00:00,000 --> 00:00:01,800
aaa bbb
ccc ddd
```

`required_break_after` 与 `keep_together` 对 SRT 同样生效，且与 `/segment` 使用**同一套参数、同一份切分结果**（仅输出格式不同）：

```bash
curl -X POST http://localhost:8000/segment/srt \
  -H 'Content-Type: application/json' \
  -d @examples/request_manual_edits.json
```

```srt
1
00:00:00,000 --> 00:00:00,500
Hello

2
00:00:00,500 --> 00:00:01,400
world.

3
00:00:02,000 --> 00:00:04,600
How are you doing today?

4
00:00:05,200 --> 00:00:06,400
Fine thanks.
```

### `POST /segment/cut` —— 视频删段后的字幕重排（JSON）

在 `/segment` 的基础上接收**按原视频毫秒计的删除区间**，先“剪掉”被删除的片段再重排字幕：

- **删除区间校验**（违反任一规则返回 `422`）：`deletions` 至少含 1 个区间；每个区间为 `[start_ms, end_ms]` 且 `start_ms < end_ms`、均 ≥ 0；区间按起点**时间升序**给出、两两**互不重叠**（端点相接允许，如 `[2000, 3200]` 与 `[3200, 4600]`）；
- **词的去留**：完整落在某个删除区间内的词被**丢弃**；词与删除边界相交但未被完整覆盖（被剪断、或仅被多个区间的并集覆盖）返回 `422`——删除边界不允许切穿任何一个词；与区间端点恰好相接的词不受影响；
- **时间前移**：每个保留词的起止时间减去它之前所有删除区间的累计时长，得到剪后视频的新时间轴（片头删除会让整体提前，片尾删除不影响字幕）；
- **分段切分**：删除区间两侧的词必须分属不同字幕，因此每个连续保留片段独立套用与 `/segment` 完全相同的字数、时长、双行排版规则与代价函数分别优化；`total_cost` 为各片段代价之和；
- **下标**：响应中的 `word_start` / `word_end` 仍是**原请求** `words` 的零基下标（被删除的词不再出现，下标可能跳号）；`start_ms` / `end_ms` 为前移后的新时间；
- **无解**：删除区间覆盖全部词（无词可排），或任一连续片段在现有限制下无合法切分，均返回 `422`（错误信息注明是第几个片段）。

请求体在 `/segment` 字段（`words`、`max_chars`、`min_duration_ms`、`max_duration_ms`、`target_duration_ms`、`pause_threshold_ms`、可选 `max_line_chars`）之上增加：

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `deletions` | `[start_ms, end_ms]` 数组 | 必填（至少 1 个） | 原视频时间轴上的删除区间（毫秒，≥ 0），按起点升序、互不重叠、`start_ms < end_ms` |

> 该接口不接受 `required_break_after` / `keep_together`；删除区间本身即是最强的分段约束。无删除需求时请使用 `/segment`。

请求示例（删除原视频 1.5s–5.0s，覆盖 `How are you doing today?` 四个词）：

```bash
curl -X POST http://localhost:8000/segment/cut \
  -H 'Content-Type: application/json' \
  -d @examples/request_cut.json
```

响应示例（`200`）：

```json
{
  "segment_count": 2,
  "total_cost": 5.8,
  "segments": [
    {
      "index": 0,
      "text": "Hello world.",
      "start_ms": 0,
      "end_ms": 1400,
      "duration_ms": 1400,
      "word_start": 0,
      "word_end": 1,
      "crossed_boundaries": 0
    },
    {
      "index": 1,
      "text": "Fine thanks.",
      "start_ms": 1700,
      "end_ms": 2900,
      "duration_ms": 1200,
      "word_start": 7,
      "word_end": 8,
      "crossed_boundaries": 0
    }
  ]
}
```

注意第二条字幕：`Fine` / `thanks.` 原时间为 5200–6400 ms，减去此前累计删除的 3500 ms 后前移到 1700–2900 ms；`word_start` 仍为原请求下标 `7`。

### `POST /segment/cut/srt` —— 删段重排的 SRT 导出

请求体与 `/segment/cut` 完全相同，返回同一份切分结果的 SRT 文档（`text/plain; charset=utf-8`）：

```bash
curl -X POST http://localhost:8000/segment/cut/srt \
  -H 'Content-Type: application/json' \
  -d @examples/request_cut.json
```

```srt
1
00:00:00,000 --> 00:00:01,400
Hello world.

2
00:00:01,700 --> 00:00:02,900
Fine thanks.
```

### `POST /segment/place` —— 遮挡感知的位置决策（JSON）

在 `/segment` 的基础上接收**视频时间轴上的遮挡区间**（每个区间遮挡 `top` 或 `bottom` 一个区域），联合决定字幕的**分条**与**摆放位置**：

- **遮挡规则**：字幕时段 `[start_ms, end_ms]` 与某遮挡区间有**正长度交集**时，该区间遮挡的区域对这条字幕不可用（端点恰好相接不算相交）；每条字幕必须有一个在其整个时段内都可用的区域，任何词无法被满足该条件的字幕覆盖时整体无解，返回 `422`；
- **联合优化**：分条沿用 `/segment` 的字数、时长、双行排版与自然边界规则及代价；此外每对相邻字幕若分处不同区域，总代价 **+1**（换区罚分）；
- **平局规则**：总代价相同先比字幕条数（少者胜），再逐条比较位置序列，`bottom` 优先于 `top`——因此没有遮挡时全部字幕都在 `bottom`；
- **遮挡区间**：数组元素为 `{start_ms, end_ms, region}`，`region ∈ {top, bottom}`，`start_ms < end_ms` 且均 ≥ 0；区间**无需排序、允许重叠**（同一时刻两个区域都被遮挡时，跨过该时刻的字幕将无区域可用）；不传或传空数组等价于无遮挡。

请求体在 `/segment` 字段（`words`、`max_chars`、`min_duration_ms`、`max_duration_ms`、`target_duration_ms`、`pause_threshold_ms`、可选 `max_line_chars`）之上增加：

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `occlusions` | 对象数组 | `[]` | 遮挡区间：`start_ms` / `end_ms` 为视频时间轴毫秒（≥ 0，`start_ms < end_ms`），`region` 为 `top` 或 `bottom` |

> 该接口不接受 `required_break_after` / `keep_together`。

请求示例（原视频 1.8s–4.8s 底部区域被遮挡）：

```bash
curl -X POST http://localhost:8000/segment/place \
  -H 'Content-Type: application/json' \
  -d @examples/request_place.json
```

响应示例（`200`）：

```json
{
  "segment_count": 3,
  "total_cost": 5.96,
  "segments": [
    {
      "index": 0,
      "text": "Hello world.",
      "start_ms": 0,
      "end_ms": 1400,
      "duration_ms": 1400,
      "word_start": 0,
      "word_end": 1,
      "crossed_boundaries": 0,
      "position": "top"
    },
    {
      "index": 1,
      "text": "How are you doing today?",
      "start_ms": 2000,
      "end_ms": 4600,
      "duration_ms": 2600,
      "word_start": 2,
      "word_end": 6,
      "crossed_boundaries": 0,
      "position": "top"
    },
    {
      "index": 2,
      "text": "Fine thanks.",
      "start_ms": 5200,
      "end_ms": 6400,
      "duration_ms": 1200,
      "word_start": 7,
      "word_end": 8,
      "crossed_boundaries": 0,
      "position": "top"
    }
  ]
}
```

每条字幕比 `/segment` 的响应多一个 `position` 字段（`top` / `bottom`）。注意中间字幕（2000–4600 ms）与底部遮挡相交、只能放顶部；若只把它移到顶部会产生 2 次换区（+2 分），因此三条字幕整体放在顶部（5.96 + 0 换区罚分）反而总代价最低——这正是“分条与位置联合优化”的效果。

### `POST /segment/place/vtt` —— 位置决策的 WebVTT 导出

请求体与 `/segment/place` 完全相同，返回同一份切分与位置结果的 WebVTT 文档（`text/vtt; charset=utf-8`），位置用 **cue 行位置**（`line` 设置）呈现：`top` → `line:0%`，`bottom` → `line:100%`：

```bash
curl -X POST http://localhost:8000/segment/place/vtt \
  -H 'Content-Type: application/json' \
  -d @examples/request_place.json
```

```vtt
WEBVTT

00:00:00.000 --> 00:00:01.400 line:0%
Hello world.

00:00:02.000 --> 00:00:04.600 line:0%
How are you doing today?

00:00:05.200 --> 00:00:06.400 line:0%
Fine thanks.
```

保存到文件：

```bash
curl -X POST http://localhost:8000/segment/place/vtt \
  -H 'Content-Type: application/json' \
  -d @examples/request_place.json -o subtitles.vtt
```

传入 `max_line_chars` 时，双行字幕在 cue 文本内用真实换行输出两行。

### `GET /health`

返回 `{"status":"ok"}`（容器健康检查同样使用该端点）。

---

## 错误处理（422）

以下情况返回 HTTP `422`，响应体 `detail` 说明原因：

- 某个词自身字数超过 `max_chars`（单词无法容纳）；
- 传入 `max_line_chars` 时，某个词自身字数超过该行宽（单词无论放在哪一行都放不下）；
- 某个词自身时长超过 `max_duration_ms`；
- 词序列未按时间排序或存在重叠；
- **人工边界输入非法或相互冲突**：
  - `required_break_after` 下标不在 `0 .. 词数-2`（例如只有 1 个词时传 `[0]`，或最后一个词之后的下标）；
  - `keep_together` 区间越界、`lo >= hi`（少于两个词）、或区间两两重叠；
  - 必断下标落在区间内部，或等于区间左外缘 `lo`（词 `lo` 既要结束上一条字幕、又要与词 `lo+1` 同条，互相矛盾）；落在右外缘 `hi` 合法；
- 参数本身不合法（空词、`end_ms <= start_ms`、`max_duration_ms < min_duration_ms`、`max_line_chars < 1`、编辑字段元素不是非负整数 / 区间不是两个整数等）；
- **删除区间相关（`/segment/cut`、`/segment/cut/srt`）**：
  - `deletions` 缺失、为空数组、区间不是两个非负整数、`start_ms >= end_ms`；
  - 区间未按起点升序给出，或两两重叠（端点相接允许）；
  - 某个词与删除边界相交但未被完整覆盖（删除边界切穿了词）；
  - 删除区间覆盖了全部词，没有任何词可排；
  - 某个连续保留片段在现有字数 / 时长 / 行宽限制下无合法切分（错误信息注明片段序号）；
- **遮挡区间相关（`/segment/place`、`/segment/place/vtt`）**：
  - 遮挡区间 `start_ms >= end_ms`、`region` 不是 `top` / `bottom`、毫秒为负数或字段缺失；
  - 某条字幕的整个时段内两个区域都被遮挡（任何切分都无法给每条字幕一个全程可用区域）——会返回类似：

    ```json
    {"detail": "no legal segmentation gives every subtitle an available region under the given constraints (max_chars=42, min_duration_ms=1000, max_duration_ms=7000); every subtitle — including the last one — must satisfy the duration limits and keep one region (top or bottom) free of occlusions for its whole time span, so try relaxing min_duration_ms or the other limits, or narrowing the occlusion intervals"}
    ```
- 没有任何切分能满足全部约束（例如最后一个词短于 `min_duration_ms`，或**人工边界迫使某条字幕违反时长 / 字数 / 行宽约束**，或传入 `max_line_chars` 后不存在满足行宽的双行排版）——会返回类似：

  ```json
  {"detail": "no legal segmentation covers all words under the given constraints (max_chars=42, max_line_chars=20, min_duration_ms=1000, max_duration_ms=7000); every subtitle — including the last one — must satisfy the duration limits (and, when set, fit on at most two lines of max_line_chars) and the required_break_after / keep_together boundaries, so try relaxing min_duration_ms or the other limits"}
  ```

---

## 配置

所有切分参数都可**按请求**传入；省略时使用环境变量配置的默认值（见 `docker-compose.yml`）：

| 环境变量 | 默认值 | 对应请求字段 |
| --- | --- | --- |
| `DEFAULT_MAX_CHARS` | `42` | `max_chars` |
| `DEFAULT_MIN_DURATION_MS` | `1000` | `min_duration_ms` |
| `DEFAULT_MAX_DURATION_MS` | `7000` | `max_duration_ms` |
| `DEFAULT_TARGET_DURATION_MS` | `3000` | `target_duration_ms` |
| `DEFAULT_PAUSE_THRESHOLD_MS` | `500` | `pause_threshold_ms` |

`max_line_chars` 没有环境变量默认值：省略该请求字段即不启用双行排版，行为与旧版完全一致。`required_break_after`、`keep_together` 同样没有环境变量默认值：省略即不施加任何人工边界。`deletions` 为 `/segment/cut` 的必填字段，也没有环境变量默认值。`occlusions` 为 `/segment/place` 的可选字段（默认空数组，等价于无遮挡），没有环境变量默认值；换区罚分固定为 1 分（`REGION_SWITCH_PENALTY`），不通过环境变量配置。

不需要外部数据库或密钥，开箱即用。

---

## 本地开发（不使用 Docker）

需要 Python 3.11+：

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

访问 http://localhost:8000/docs 调试。

运行测试（核心切分逻辑只依赖标准库；API 测试需要 fastapi + httpx）：

```bash
python3 -m unittest discover -s tests
# 或
pytest
```

## 项目结构

```
app/
  config.py      # 环境变量默认值与罚分常量
  models.py      # Pydantic 请求/响应模型
  segmenter.py   # 动态规划切分核心（纯 Python，无 Web 依赖）
  cut.py         # 视频删段后的重排：删词、时间前移、分片段切分
  place.py       # 遮挡感知的位置决策：分条与 top/bottom 放置联合优化
  srt.py         # SRT 时间戳与文档格式化
  vtt.py         # WebVTT 时间戳与带 cue 行位置的文档格式化
  main.py        # FastAPI 入口（/segment、/segment/srt、/segment/cut、/segment/cut/srt、/segment/place、/segment/place/vtt、/health）
tests/           # unittest 测试（核心逻辑 + 删段重排 + 遮挡放置 + API 集成）
examples/        # 请求示例（基础 / 双行排版 / 人工编辑边界 / 删段重排 / 遮挡放置）
Dockerfile
docker-compose.yml
requirements.txt
```

## 算法说明

动态规划状态 `dp[i]` 为“从第 `i` 个词切分到结尾”的最小代价与字幕条数：

```
dp[n] = (0, 0)
dp[i] = 按 (总代价, 字幕条数) 字典序取最小，枚举合法的字幕结尾 j：
        ((dur_ij秒 − target秒)² + 4 × crossed(i,j) + linecost(i,j) + dp[j+1].代价,
         1 + dp[j+1].条数)
```

字幕从 `i` 扩展到 `j` 时字数与时长单调增长，超限即可提前终止；自然边界计数用前缀和做 O(1) 区间查询。

人工边界在同一个 DP 上直接剪枝，不改变目标函数：

- **必断**：再维护一个必断前缀和，候选字幕 `i..j` 内部若含任何必断边界（`i <= k < j`）即非法；一旦吞并第一个必断点，继续扩大 `j` 只会吞并更多，可直接 `break`。段首 `i` 本身位于必断边界时 DP 递归会自然落到正确的前驱；
- **保持同段**：把每个区间 `[lo, hi]` 内部的边界 `lo .. hi-1` 标记为“禁止作为段边缘”；候选段的结束边 `(j, j+1)` 落在被禁边界上时跳过（`continue`，不能 `break`，因为更远的结束位置可能合法）。

下标越界、区间少于两个词、区间重叠、必断与区间冲突等输入错误在进入 DP 前一次性校验并返回 `422`；DP 跑完若 `dp[0]` 不可达，即说明人工边界（或行宽等其他约束）导致无合法方案。人工边界只增减可行候选，枚举仍覆盖所有合法切分，因此求出的仍是满足全部边界的**全局**最低代价方案，时间复杂度保持 O(n² log n)（不传 `max_line_chars` 时 O(n²)）。

传入 `max_line_chars` 时，`linecost(i,j)` 为该候选字幕的排版代价：整段不超行宽则为 0（单行）；否则在词间枚举断点，两行各自不超行宽时计算 `(行长1 − 行长2)² / 行宽²`，取最小值（平局取靠前断点）；没有合法断点时该候选非法。行长用“词长 + 词间空格”的前缀和按二分搜索求值，合法断点范围与等长断点都只需 O(log n) 定位，因此整体时间复杂度仍为 O(n² log n)（不传该参数时为 O(n²)），空间 O(n)。

删段重排在进入 DP 之前做一次线性扫描：删除区间先校验升序、不重叠、`start < end`；随后按时间顺序遍历词，用指针推进区间——完整落入区间的词丢弃，与边界相交但未完整覆盖的词直接报错，保留词减去此前累计删除时长；凡是跨过删除区间的位置都开启新的连续片段。由于每个删除区间强制成为字幕边缘，总代价与硬约束都按字幕可加，因此各片段独立跑一次上述 DP 即等价于全局强制断点后的最优解；各片段的字幕顺序拼接、统一编号，`word_start` / `word_end` 映射回原请求下标。

遮挡放置把 DP 状态扩展为 **(词下标, 区域)**：`dp[i][r]` 表示“从第 `i` 个词切到结尾、且第 `i` 个词所在字幕放在区域 `r`”的最优 `(总代价, 字幕条数, 位置序列)`。候选字幕 `i..j` 先按原有的字数 / 时长 / 行宽规则过滤，再要求区域 `r` 在整个时段 `[start_i, end_j]` 上无遮挡——遮挡区间按区域分别排序后用“起点二分 + 终点前缀最大值”做 O(log n) 查询，端点相接不算相交；转移时枚举下一条字幕的区域 `r'`，`r' ≠ r` 时加 1 分换区罚分。同一状态内所有候选的位置序列都以 `r` 开头，因此序列比较可直接落在后继状态的序列元组上（每个状态只在扫描结束后拼接一次序列）；平局依次比较 `(总代价, 字幕条数, 位置序列)`，位置序列中 `bottom` 排在 `top` 前。最终答案在 `dp[0][bottom]` 与 `dp[0][top]` 之间按同一规则取舍，时间复杂度 O(n² log n)。由于换区罚分只依赖相邻字幕的区域，该状态设计保留了最优子结构，求出的仍是满足全部硬约束的**全局**最低代价方案。
