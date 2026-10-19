# 铁锈战争 (Rusted Warfare) `.replay` 回放文件格式说明

> 适用版本：**1.15 原版**（存档格式版本 `96`，游戏构建号 `176`）

---

## 1. 总体结构

回放文件本质上是一个 **"回放头 + 内嵌初始存档 + 命令流"** 的容器：

```
rustedWarfareReplay                      ← 顶层头（UTF 魔数）
  ├─ int   游戏构建号 (1.15 = 176)
  ├─ int   存档格式版本 (1.15 = 96)
  ├─ UTF   游戏版本字符串 "1.15"
  ├─ bool  aq（演示/DLC 等特殊标志）
  │
  ├─ [块] gamesave                        ← 内嵌的"开局存档"（非压缩块）
  │     └─ rustedWarfareSave              ← 存档头
  │           ├─ int 构建号 / int 96 / bool aq
  │           └─ [压缩块] saveCompression ← GZIP 压缩的完整开局状态
  │                 └─ （见第 3 节）
  │
  └─ 命令流（可重复出现的块序列）
        rc         命令
        wait       空等待帧
        cs         校验和
        es         扩展校验和
        resync     重同步（附带完整存档快照）
        chat       聊天
        end        回放结束
        endReplayMetaData  回放元数据（文件末尾）
```

相关类路径（均为 `com.corrodinggames.rts` 包下的相对路径，仅作格式来源定位，不对应本仓库中的任何文件）：

| 用途 | 类路径 |
|---|---|
| 回放顶层读写 / `gamesave` 块 | `gameFramework/bp.java` |
| 命令流块写入线程（`rc/cs/wait/es/resync/chat/end/endReplayMetaData`） | `gameFramework/bq.java` |
| 块写入器 `bg` | `gameFramework/j/bg.java` |
| 块读取器 `j` | `gameFramework/j/j.java` |
| 块压缩包装 | `gameFramework/j/bh.java` |
| 初始开局存档 | `gameFramework/aj.java` |
| 单条命令 | `gameFramework/e.java` |
| 航点 | `game/units/en.java` |
| 额外动作项 | `gameFramework/d.java` |
| 路径点（压缩） | `gameFramework/k/j.java` |
| 动作 ID | `game/units/a/c.java` |
| 队伍/玩家 | `game/p.java` |
| 战役/任务脚本 | `gameFramework/n/p.java` |

---

## 2. 底层编码约定

铁锈战争所有存档、回放都基于 **Java `DataOutputStream` / `DataInputStream`**。因此：

- **字节序：大端（Big-Endian）**。
- `boolean`：写 1 字节，`0` = false，非 0 = true。
- `byte / short / int / long / float / double`：标准定长（1/2/4/8/8 字节）。
- 字符串 `writeUTF`：**uint16 长度 + UTF 字节**。注意 Java 用的是修正版 UTF-8（非 BMP 字符按 CESU-8 拆成两个 3 字节代理对）。
- **可空字符串**（`bg.a(String)` / `j.a()`）：先写一个 `boolean`，为真才写 `writeUTF`。
- **可空整数**（`bg.a(Integer)` / `j.b()`）：先写一个 `boolean`，为真才写 `int`。
- **可空枚举**：直接写 `int`，`-1` 表示 null。
- **单位类型**（`bg.a(el)` / `j.d()`）：写 `int`：
  - `-1` = null
  - `-2` = 内建/mod 自定义单位，随后是一个 `writeUTF` 单位名
  - 其它 = 内建单位枚举序号
- **实体引用 / 对象 ID**：写 `long`（实体唯一 `ej` 值），`-1` 表示空。
- **字节数组**：写 `int` 长度 + 原始字节（`bg.a(byte[])` / `j.f()`）。

### 2.1 块机制（Block）

回放的正文由**块**组成，块是自描述的：

```
[ writeUTF  块名 ]        ← 例如 "rc" / "gamesave" / "c"
[ int       载荷长度 N ]
[ byte[N]   载荷 ]
```

- 写入端：`bg.d(name)` 开启一个块，`bg.e(name)` 结束并落盘（此时才写出块名与长度）。
- 读取端：`j.a(z)` 读块，返回载荷字节流（作为一个子 `j` 继续解析）。
- `bg.c(String)` 是**空实现**（调试用），所以源码里出现的 `bgVar.c("Section: ...")` 在文件里**不占任何字节**。

### 2.2 压缩块

`bg.a(name, true)`（带 `true`）会用一个 **GZIP 包装**该块内容（见 `gameFramework/j/bh.java` 的 `bh(boolean z)`）。因此该块的"载荷字节"本身就是一段 GZIP 流（`1F 8B ...`），读取时先解压再解析。

回放里有两个著名的压缩块：
- 开局存档的 `saveCompression` 块；
- 路径点块 `p`。

### 2.3 块结束标记（Mark）

`bg.e()` 不带参数时写出 **short `12345`**（`0x3039`）。它是块之间的"分隔哨兵"，用来标识某段逻辑结构结束。读取端 `j.a(String)` 校验它是否为 12345。

---

## 3. 内嵌初始存档（`gamesave`）

`gamesave` 块（非压缩）内部又是一层存档结构：

```
UTF  "rustedWarfareSave"     存档魔数（j.b("rustedWarfareSave")，此处是 writeUTF）
int   构建号 (176)
int   存档格式版本 (96)
bool  aq
[压缩块] saveCompression       ← 真正的内容，GZIP 压缩
short 12345                   存档结束标记
UTF   "<SAVE END>"            存档结束字符串
```

`saveCompression` 解压后的内容（见 `gameFramework/aj.java` 的 `a(bg)`）按顺序为：

### 3.1 `customUnitsBlock` 块（非压缩）
mod / 自定义单位定义块。回放查看器一般只需记录长度与 mod 名，不必完全解析。

### 3.2 `gameSetup` 块（非压缩）
地图规则。结构：

```
bool flagC           ← 多人联网游戏 (bU.C)
bool flagG           ← bU.G
bool  z = (flagC||flagG)
if (z):
    byte   reserved0 (写 0)
    byte   rulesVersion (写 4)
    int    fogMode            迷雾模式
    int    startingCredits    起始资金
    bool   revealedMap        开局揭示地图
    int    aiDifficulty        AI 难度
    int    startingUnits      起始单位档位
    float  incomeMultiplier   收入倍率
    bool   noNukes            禁用核弹
    bool   flag_j
    bool   sharedControl      共享控制
    if rulesVersion>0: bool flag_m
    if rulesVersion>=2: bool flag_n
    if rulesVersion>=3: bool allowSpectators ; bool lockedRoom
    if rulesVersion>=4: int randomSeed
    int    teamUnitCap
    int    teamUnitCap2
```

> 实际写入的 `rulesVersion` 恒为 `4`，故上面所有分支都会被读到。

### 3.3 地图路径与远程地图
```
UTF   mapPath               "maps/.../xxx.tmx"（或内置地图名）
bool  hasRemoteMap
if (hasRemoteMap): int len ; byte[len]   ← 内嵌的地图文件完整字节
```

### 3.4 相机与起始帧
```
int   startFrame (bv)
float cameraX        （cv + cF）
float cameraY        （cw + cG）
float zoom           （cS）
int   int_a          （bS.f558a）
int   0
short 12345          ← 标记
```

### 3.5 全局规则 + 战役任务
```
bool  gameRuleFlag   ← 恒为 false（game/b/b.java 的 a(bg) 只写一个 false）
bool  bs
bool  bI.F
bool  bI.G
bool  bI.H
bool  hasMission
if (hasMission): 任务脚本块（见 3.6）
short 12345          ← 标记
```

### 3.6 任务脚本块（`com.corrodinggames.rts.gameFramework.n.p`）
写入版本恒为 `6`：
```
bool  flag_j
int   r / u / v / w / x
float z / A / B
bool  flag_m
int   version (=6)
int   triggerCount
  for each trigger:
     UTF   name
     bool  active
     int   k          (version>=2)
     int   l          (version>=2)
     bool  m          (version>=3)
     int   n          (version>=4)
int   y              (version>=5)
bool  flag_l         (version>=6)
```

### 3.7 队伍/玩家列表
```
int   myTeamIndex        （-1 或某队 id）
int   teamCount          （= game.p.c，通常 10/12/14…）
for each team:
   bool  isAI              （p instanceof game.a.a）
   bool  isNetworkedPlayer （p instanceof game.c）
   bool  notNull
   if (notNull): 队伍核心数据（见 3.7.1）
```

#### 3.7.1 队伍核心数据（`game/p.java#b(bg)`）
写入端版本 `bg.d = 999999`，故**所有版本分支都生效**：

```
byte    teamId          (l)
int     credits         ((int) p)
int     teamIndex       (s)
UTF?    name            (w)         ← 可空字符串
bool    flag_X          (X)
int     flag_Z          (t(), >26)
long    flag_aa         (aa)
bool    flag_x          (>=55)
int     flag_y          (>=55)
int     flag_af         (>=91)
byte    0               （常量）
bool    flag_L          (>=97)
bool    flag_M          (>=97)
bool    flag_H          (>=125)
bool    flag_F          (>=125)
int     flag_G          (>=125)
UTF?    S               (>=149)     ← 可空字符串
int     T               (>=149)
int?    A               (>=156)     ← 可空整数
int?    B               (>=156)
int?    C               (>=156)
int?    D               (>=156)
int     E               (>=156)
```

### 3.8 对象（单位/建筑）外壳表
只写"外壳"，不含完整状态：
```
int   objectCount
for each object:
   byte kind
      kind=1 → 单位：int 单位类型（枚举，-1/-2 语义见 2 节）
      kind=3 → 自定义单位：UTF 单位名
      kind=2 → 非单位对象：byte 子类型（1=game.n, 2=game.f, 3=gameFramework.d.f）
   long id                ← 实体 ej
```

### 3.9 后续内容（查看器不必解析）
外壳表之后依次还有：`int nextUnitId`，以及若干游戏状态子对象、逐队伍完整数据、逐对象的完整序列化，最后 `short 12345`、`UTF "<SAVE END>"`。
查看器只需要 3.1～3.8 即可（地图、规则、队伍名、初始单位分布）。

---

## 4. 命令流

`gamesave` 块结束后，文件剩余部分是一条由块组成的命令流，读取循环见 `gameFramework/bp.java` 中的 `f()`。

| 块名 | 含义 | 载荷 |
|---|---|---|
| `rc` | 一条玩家命令 | `int frame` + 下面的"命令结构" |
| `wait` | 空等待（仅推进帧） | `int frame` |
| `cs` | 常规校验和 | `int frame` + `long checksum` |
| `es` | 扩展校验和 | `int frame` + `int len` + `byte[len]`（见 4.3） |
| `resync` | 重同步（内嵌完整存档快照） | 见 4.4 |
| `chat` | 聊天消息 | 见 4.5 |
| `end` | 回放结束 | 无 |
| `endReplayMetaData` | 回放元数据 | 见 4.6 |

### 4.1 `rc` 命令结构（`e.java#a(bg)`）

写入端总是写**最新格式**，字段顺序如下（`bool` 为真时才有后续内容）：

```
byte   teamId
bool   hasWaypoint ;  if: 航点 en（见 4.1.1）
bool   e
bool   g
int    -1                  （固定常量）
int    setAttackMode       （枚举 units.a，null = -1）
bool   hasPos ;  if: float x, float y     （PointF o）
bool   p
int    unitCount ;  long × unitCount      （单位 id 列表）
bool   hasTeam ;  if: byte team           （PointF q 对应的队伍）
bool   hasPos2 ;  if: float x, float y    （PointF l）
long   targetUnitId        （-1 = 空）
UTF    specialActionId     （动作 id 字符串）
bool   f
short  teamMask
bool   s ; if: byte 0, float stepRate, float u, int systemAction
int    extraActionCount ;  d × count      （见 4.1.3）
bool   h
```

#### 4.1.1 航点 `en`（`game/units/en.java#a(bg)`）
```
int    action         （枚举 eo；null = -1）—— move/attack/build/repair/reclaim/guard/loadInto/loadUp/attackMove/follow/touchTarget
int    buildType      （单位类型 el）
float  x / y
long   targetUnitId   （-1 = 空）
byte   quantity
float  k / l
bool   m / j / n
UTF?   actionId       （动作 id 字符串，可空）
```

#### 4.1.3 额外动作项 `d`（`d.java#a(j)`）
```
long   b
float  c / d / e / f
int    g
int    h              （枚举 cg，null = -1）
bool   hasPathObject
if (hasPathObject):
    bool hasPathPoints
    if (hasPathPoints): 路径块 p（见 4.1.4）
```

#### 4.1.4 路径点块 `p`（`gameFramework/k/j.java#a(j)`）
以 **GZIP 压缩块**写入。解开后：
```
int    count
if (count > 0):
    short x0, y0                    第一个点
    重复 (count-1) 次:
        byte b
        if (b < 128):               打包位移
            dx = (b & 3) - 1         （取值 -1..+1）
            dy = ((b & 12) >> 2) - 1
            x += dx ; y += dy
        else:                       越界回退为绝对坐标
            再读 2 个 short：x, y
```
即相邻点位移限制在 −1..+1 时用 1 字节编码，否则用 128 + 两个 short 兜底。

### 4.2 帧号
`rc` / `wait` / `cs` / `es` / `resync` / `chat` 都以 `int frame` 开头，表示该事件发生的游戏帧。

### 4.3 `es` 扩展校验和载荷
```
byte   trace           （是否追踪模式，custom.d.b 判断）
int    count
long   valueCount × count     ← 各"校验和探针"的当前值
```

### 4.4 `resync` 载荷
```
int    frame
int    h
int    i
float  j
float  k
int    len ; byte[len]        ← 这一帧的完整存档快照（用于重同步）
```

### 4.5 `chat` 载荷
```
int    frame
int    authorTeamId
UTF?   from           （可空字符串）
UTF?   message        （可空字符串）
```

### 4.6 `endReplayMetaData` 载荷
```
byte   0
int    lastFrame
int    time             （毫秒）
int    commandCount
int    resyncCount
UTF    raw              （"{frames:...,time:...,commandCount:...,resyncCount:...}"）
```

---

## 5. 字段速查 / 常量

| 名称 | 值 | 出处 |
|---|---|---|
| 回放魔数 | `rustedWarfareReplay` | bp.java |
| 存档魔数 | `rustedWarfareSave` | aj.java |
| 存档结尾串 | `<SAVE END>` | aj.java |
| 块结束标记 | short `12345` | j/bg.java `e()` |
| 1.15 存档格式版本 | `96` | bp.java 校验 |
| 1.15 构建号 | `176` | `k.a(true)` |
| 单位类型哨兵 | `-1`=null, `-2`=自定义(mod) | j/j.java `d()` |
| 实体引用哨兵 | `-1`=空 | j/bg.java `a(ah)` |
| 空枚举哨兵 | `-1` | j/j.java `b(Class)` |

---

## 6. 已知限制 / 备注

1. **后续游戏状态未完全解析**：初始存档在"对象外壳表"之后还有逐对象的完整数据与逐队伍数据，本解析器不消费这部分（查看器不需要）。因此 `parse_initial_save` 的 `_consumed < _total` 属正常现象。
2. **`customUnitsBlock` 体不解析**：仅记录字节长度，用于定位与统计。
3. **`resync` 快照**：本解析器只记录其字节长度，不递归解析其中的存档（除非查看器需要跳帧重放）。
4. **字符串编码**：非 BMP 字符（如部分 emoji）用 CESU-8 代理对存储，解析器已做兼容处理。
5. **版本门控**：所有"版本分支"的阈值均来自客户端自身的版本判断（`jVar.c >= N` / `bgVar.d >= N`）；1.15 写入端取"最新格式"，读取端按 96 逐分支读取。
6. 上述结论已在 **10 个真实 1.15 回放** 上全部通过（0 失败、0 警告），每条命令的 `_ok`（声明长度 == 实际消费字节）均成立。样本未随仓库分发——`.replay` 内含玩家昵称与聊天记录，属他人对局数据。

---

## 7. 用法

```bash
# 概览
python3 rwreplay.py info "<replay文件>"

# 导出完整 JSON
python3 rwreplay.py dump "<replay文件>" -o out.json

# 批量列出
python3 rwreplay.py list "<目录>"

# 生成自包含 HTML 查看器（--assets 指向你自己安装包解出的 assets 目录，用于取地图底图；不给则不渲染底图）
python3 rwreplay.py viewer "<replay文件>" -o viewer.html --assets "/path/to/assets"
```

---

## 8. 声明

- 本工具仅用于回放文件查看与分析研究，不修改任何游戏文件，不分发游戏资源。
