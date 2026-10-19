#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rwreplay.py -- 铁锈战争 (Rusted Warfare) 回放文件 (.replay) 解析器

用法:
    python3 rwreplay.py info   <file.replay>
    python3 rwreplay.py dump   <file.replay> [-o out.json]
    python3 rwreplay.py list   <dir>
    python3 rwreplay.py viewer <file.replay> [-o viewer.html] [--assets DIR]

本模块仅依赖 Python 标准库。
"""

import argparse
import glob
import gzip
import json
import os
import struct

# ---------------------------------------------------------------------------
# 底层：Java DataInputStream 兼容读取器
# ---------------------------------------------------------------------------


class Reader:
    """模拟 java.io.DataInputStream 的读取器（大端序）。"""

    __slots__ = ("buf", "pos", "version")

    def __init__(self, data, version=999999):
        self.buf = data if isinstance(data, (bytes, bytearray)) else bytes(data)
        self.pos = 0
        # 对应 com.corrodinggames.rts.gameFramework.j.j 的版本字段
        # 回放顶层为 96；999999 表示"最新格式"（写入方默认值）
        self.version = version

    # --- 基础类型 -------------------------------------------------------
    def eof(self):
        return self.pos >= len(self.buf)

    def remaining(self):
        return len(self.buf) - self.pos

    def read(self, n):
        if n < 0:
            raise ValueError("negative length %d" % n)
        end = self.pos + n
        if end > len(self.buf):
            raise EOFError("short read: want %d have %d (at %d)" % (n, len(self.buf) - self.pos, self.pos))
        out = self.buf[self.pos:end]
        self.pos = end
        return out

    def u1(self):
        return self.read(1)[0]

    def byte(self):
        return struct.unpack(">b", self.read(1))[0]

    def boolean(self):
        return self.u1() != 0

    def short(self):
        return struct.unpack(">h", self.read(2))[0]

    def ushort(self):
        return struct.unpack(">H", self.read(2))[0]

    def int(self):
        return struct.unpack(">i", self.read(4))[0]

    def long(self):
        return struct.unpack(">q", self.read(8))[0]

    def f32(self):
        return struct.unpack(">f", self.read(4))[0]

    def f64(self):
        return struct.unpack(">d", self.read(8))[0]

    def utf(self):
        """Java DataOutputStream.writeUTF / readUTF (modified UTF-8)."""
        n = self.ushort()
        raw = self.read(n)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            # 非 BMP 字符：Java 使用 CESU-8（每个 surrogate 单独 3 字节）
            try:
                return raw.decode("utf-8", "surrogatepass").encode(
                    "utf-16", "surrogatepass").decode("utf-16")
            except Exception:
                return raw.decode("utf-8", "replace")

    def nullable_utf(self):
        """bg.a(String): 先写 bool，非 null 才写 UTF。"""
        if not self.boolean():
            return None
        return self.utf()

    def nullable_int(self):
        """bg.a(Integer): 先写 bool，非 null 才写 int。"""
        if not self.boolean():
            return None
        return self.int()

    def bytes_blob(self):
        """bg.a(byte[]): int 长度 + 原始字节。"""
        return self.read(self.int())

    # --- 游戏专用 -------------------------------------------------------
    def read_unit_type(self):
        """bg.a(el) / j.d(): -1=null, -2=内建 mod 单位(后跟 UTF), 否则为枚举序号。"""
        v = self.int()
        if v == -1:
            return None
        if v == -2:
            return {"custom": True, "name": self.utf()}
        return {"custom": False, "ordinal": v}

    def read_enum(self):
        """j.b(Class): -1=null, 否则为枚举序号。"""
        v = self.int()
        if v == -1:
            return None
        return v

    def mark(self, label=""):
        """bg.e() 写入的块结束标记（short 12345）。"""
        s = self.short()
        if s != 12345:
            raise EOFError("block mark mismatch (%s): %d != 12345 at %d" % (label, s, self.pos - 2))
        return s


def read_block(r):
    """读取一个带名字的数据块: [UTF 名称][int 长度][payload]。返回 (名称, payload bytes)。"""
    name = r.utf()
    ln = r.int()
    payload = r.read(ln)
    return name, payload


def unwrap_gzip(b):
    """块数据可能是 GZIP（bh(compressed=true)）。"""
    if len(b) >= 2 and b[0] == 0x1F and b[1] == 0x8B:
        try:
            return gzip.decompress(b)
        except Exception:
            return b
    return b


# ---------------------------------------------------------------------------
# 存档（回放内嵌的初始存档 "gamesave"）
# ---------------------------------------------------------------------------


def parse_game_setup(payload):
    """解析 gameSetup 块：地图规则等。"""
    r = Reader(payload)
    out = {}
    out["flagC"] = r.boolean()          # bU.C: 多人联网游戏
    out["flagG"] = r.boolean()          # bU.G
    z = r.boolean()                     # C || G
    out["hasRules"] = z
    if z:
        out["reserved0"] = r.byte()
        bver = r.byte()
        out["rulesVersion"] = bver
        rules = {}
        rules["fogMode"] = r.int()
        rules["startingCredits"] = r.int()
        rules["revealedMap"] = r.boolean()
        rules["aiDifficulty"] = r.int()
        rules["startingUnits"] = r.int()
        rules["incomeMultiplier"] = r.f32()
        rules["noNukes"] = r.boolean()
        rules["flag_j"] = r.boolean()
        rules["sharedControl"] = r.boolean()
        if bver > 0:
            rules["flag_m"] = r.boolean()
        if bver >= 2:
            rules["flag_n"] = r.boolean()
        if bver >= 3:
            rules["allowSpectators"] = r.boolean()
            rules["lockedRoom"] = r.boolean()
        if bver >= 4:
            rules["randomSeed"] = r.int()
        rules["teamUnitCap"] = r.int()
        rules["teamUnitCap2"] = r.int()
        out["rules"] = rules
    out["_remaining"] = r.remaining()
    return out


def parse_team_core(r):
    """解析 p.b(bg)：队伍/玩家核心数据。

    写入方版本 bg.d 恒为 999999，故所有版本分支（>=26/55/91/97/125/149/156）
    全部取用。逐字段对应 com.corrodinggames.rts.game.p#b(bg)。
    """
    t = {}
    t["teamId"] = r.byte()              # l: byte
    t["credits"] = r.int()              # (int) p
    t["teamIndex"] = r.int()            # s: int
    t["name"] = r.nullable_utf()        # w: String (bg.a(String))
    t["flag_X"] = r.boolean()           # X: boolean
    t["flag_Z"] = r.int()               # Z: int (d>26)
    t["flag_aa"] = r.long()             # aa: long
    t["flag_x"] = r.boolean()           # x: boolean (d>=55)
    t["flag_y"] = r.int()               # y: int
    t["flag_af"] = r.int()              # af: int (d>=91)
    t["flag_b0"] = r.byte()             # constant 0: byte
    t["flag_L"] = r.boolean()           # L: boolean (d>=97)
    t["flag_M"] = r.boolean()           # M: boolean
    t["flag_H"] = r.boolean()           # H: boolean (d>=125)
    t["flag_F"] = r.boolean()           # F: boolean
    t["flag_G"] = r.int()               # G: int
    t["S"] = r.nullable_utf()           # S: String (d>=149)
    t["T"] = r.int()                    # T: int
    t["A"] = r.nullable_int()           # A: Integer (d>=156)
    t["B"] = r.nullable_int()           # B: Integer
    t["C"] = r.nullable_int()           # C: Integer
    t["D"] = r.nullable_int()           # D: Integer
    t["E"] = r.int()                    # E: int
    return t


def parse_mission(r):
    """解析战役/任务脚本块 (com.corrodinggames.rts.gameFramework.n.p)。"""
    m = {}
    m["flag_j"] = r.boolean()
    m["r"] = r.int()
    m["u"] = r.int()
    m["v"] = r.int()
    m["w"] = r.int()
    m["x"] = r.int()
    m["z"] = r.f32()
    m["A"] = r.f32()
    m["B"] = r.f32()
    m["flag_m"] = r.boolean()
    version = r.int()
    m["version"] = version
    triggers = []
    if version > 0:
        count = r.int()
        for _ in range(count):
            t = {"name": r.utf(), "active": r.boolean()}
            if version >= 2:
                t["k"] = r.int()
                t["l"] = r.int()
            if version >= 3:
                t["m"] = r.boolean()
            if version >= 4:
                t["n"] = r.int()
            triggers.append(t)
    m["triggers"] = triggers
    if version >= 5:
        m["y"] = r.int()
    if version >= 6:
        m["flag_l"] = r.boolean()
    return m


def parse_initial_save(inner, warnings):
    """解析 saveCompression 解压后的内容。返回 dict。"""
    r = Reader(inner)
    out = {}

    # 1) customUnitsBlock（mod/自定义单位定义），只记录大小与 mod 名
    name, payload = read_block(r)
    out["customUnitsBlockLen"] = len(payload)
    out["customUnitsBlockName"] = name

    # 2) gameSetup
    name, payload = read_block(r)
    out["gameSetup"] = parse_game_setup(payload)

    # 3) 地图路径 + 可选内嵌地图
    out["mapPath"] = r.utf()
    has_remote = r.boolean()
    out["hasRemoteMap"] = has_remote
    if has_remote:
        blob = r.bytes_blob()
        out["remoteMapLen"] = len(blob)
    else:
        out["remoteMapLen"] = 0

    # 4) 摄像机/初始帧等
    out["startFrame_bv"] = r.int()
    out["camera_x"] = r.f32()
    out["camera_y"] = r.f32()
    out["zoom"] = r.f32()
    out["int_a"] = r.int()
    out["int_b"] = r.int()

    r.mark("end-of-setup")

    # 5) 地图/游戏全局规则
    out["gameRuleFlag"] = r.boolean()
    out["bool_bs"] = r.boolean()
    out["bool_bI_F"] = r.boolean()
    out["bool_bI_G"] = r.boolean()
    out["bool_bI_H"] = r.boolean()
    has_mission = r.boolean()
    out["hasMission"] = has_mission
    if has_mission:
        out["mission"] = parse_mission(r)

    r.mark("end-of-globals")

    # 6) 队伍列表（外壳）
    out["myTeamIndex"] = r.int()
    team_count = r.int()
    out["teamCount"] = team_count
    teams = []
    for i in range(team_count):
        entry = {"index": i}
        entry["isAI"] = r.boolean()
        entry["isNetworkedPlayer"] = r.boolean()
        not_null = r.boolean()
        entry["notNull"] = not_null
        if not_null:
            entry.update(parse_team_core(r))
        teams.append(entry)
    out["teams"] = teams

    # 7) 对象（单位/建筑）外壳表
    obj_count = r.int()
    out["objectCount"] = obj_count
    shells = []
    for _ in range(obj_count):
        kind = r.byte()
        item = {"kind": kind}
        if kind == 1:
            # 单位
            item["type"] = r.read_enum()
        elif kind == 3:
            item["unitTypeName"] = r.utf()
        else:
            item["sub"] = r.byte()
        item["id"] = r.long()
        shells.append(item)
    out["objects"] = shells

    # 8) 下一单位 id
    out["nextUnitId"] = r.long()

    out["_consumed"] = r.pos
    out["_total"] = len(inner)
    return out


# ---------------------------------------------------------------------------
# 命令单位（com.corrodinggames.rts.gameFramework.e）
# ---------------------------------------------------------------------------


def parse_waypoint(r):
    wp = {}
    wp["action"] = r.read_enum()            # eo 枚举
    wp["buildType"] = r.read_unit_type()    # el
    wp["x"] = r.f32()
    wp["y"] = r.f32()
    target = r.long()
    wp["targetUnitId"] = None if target == -1 else target
    wp["quantity"] = r.byte()
    wp["k"] = r.f32()
    wp["l"] = r.f32()
    wp["m"] = r.boolean()
    wp["j"] = r.boolean()
    wp["n"] = r.boolean()
    wp["actionId"] = r.nullable_utf()       # game.units.a.c
    return wp


EO_NAMES = ["move", "attack", "build", "repair", "reclaim", "guard",
            "loadInto", "loadUp", "attackMove", "follow", "touchTarget"]


def parse_path(r):
    """解析内嵌的压缩路径块 "p"（com.corrodinggames.rts.gameFramework.k.j#a(j)）。

    块结构：[UTF "p"][int 长度][payload]；payload 为：
        int 点数；若 >0：short x0, short y0，
        随后 (点数-1) 个位移项：byte b；b<128 为打包位移，否则再跟 2 个 short。
    注意：该块以压缩方式写入（k.j#a(bg) 中 a("p", true)），payload 外层为 GZIP。
    """
    name, payload = read_block(r)
    if name != "p":
        raise ValueError("期望路径块 'p'，实际为 %r" % name)
    payload = unwrap_gzip(payload)
    pr = Reader(payload, version=r.version)
    n = pr.int()
    pts = []
    if n > 0:
        x = pr.short()
        y = pr.short()
        pts.append((x, y))
        for _ in range(1, n):
            b = pr.u1()
            if b >= 128:
                pr.short()
                pr.short()
            else:
                x += (b & 3) - 1
                y += ((b & 12) >> 2) - 1
                pts.append((x, y))
    return {"count": n, "points": pts}


def parse_extra_action(r):
    """解析 e.x 中的一个 d 项（com.corrodinggames.rts.gameFramework.d#a(j)）。

    字段：long / float / float / float / float / int / 枚举(cg)，
    随后 bool（是否存在路径对象 k.j）；
    若存在，则再跟 bool（k.j 内 x 是否非空），非空时才是压缩块 "p"。
    """
    item = {}
    item["b"] = r.long()
    item["c"] = r.f32()
    item["d"] = r.f32()
    item["e"] = r.f32()
    item["f"] = r.f32()
    item["g"] = r.int()
    item["h"] = r.read_enum()             # cg 枚举
    if r.boolean():                       # 是否存在路径对象
        item["hasPathObject"] = True
        if r.boolean():                   # k.j 内是否存在路径点
            item["path"] = parse_path(r)
        else:
            item["path"] = None
    else:
        item["hasPathObject"] = False
        item["path"] = None
    return item


def parse_command(r):
    """解析一个 "c" 命令块（写入方版本为最新格式）。"""
    name, payload = read_block(r)
    if name != "c":
        raise ValueError("期望命令块 'c'，实际为 %r" % name)
    cr = Reader(payload, version=r.version)
    end = len(payload)
    c = {}
    c["teamId"] = cr.byte()
    if cr.boolean():
        c["waypoint"] = parse_waypoint(cr)
        wa = c["waypoint"]["action"]
        c["waypoint"]["actionName"] = EO_NAMES[wa] if wa is not None and 0 <= wa < len(EO_NAMES) else None
    else:
        c["waypoint"] = None
    c["flag_e"] = cr.boolean()
    c["flag_g"] = cr.boolean()
    c["const_int"] = cr.int()
    c["setAttackMode"] = cr.read_enum()
    if cr.boolean():
        c["point_o"] = [cr.f32(), cr.f32()]
    else:
        c["point_o"] = None
    c["flag_p"] = cr.boolean()
    n = cr.int()
    c["unitIds"] = [cr.long() for _ in range(n)]
    # version >= 16
    if cr.boolean():
        c["team_q"] = cr.byte()
    else:
        c["team_q"] = None
    # version >= 29
    if cr.boolean():
        c["point_l"] = [cr.f32(), cr.f32()]
    else:
        c["point_l"] = None
    target = cr.long()
    c["targetUnitId"] = None if target == -1 else target
    # version >= 33
    c["specialActionId"] = cr.utf()
    # version >= 37
    c["flag_f"] = cr.boolean()
    # version >= 52
    c["teamMask_short"] = cr.short()
    # version >= 53
    if cr.boolean():
        cr.byte()
        c["stepRate"] = cr.f32()
        c["u"] = cr.f32()
        c["systemAction"] = cr.int()
    else:
        c["stepRate"] = None
        c["u"] = None
        c["systemAction"] = None
    dcount = cr.int()
    extras = []
    for _ in range(dcount):
        item = parse_extra_action(cr)
        extras.append(item)
    c["extraActions"] = extras
    # version >= 80
    c["flag_h"] = cr.boolean()

    c["_parsedBytes"] = cr.pos
    c["_declaredBytes"] = end
    c["_ok"] = cr.pos == end
    return c


# ---------------------------------------------------------------------------
# 顶层回放解析
# ---------------------------------------------------------------------------


def parse_replay(path):
    raw = open(path, "rb").read()
    r = Reader(raw)
    warnings = []
    result = {
        "file": os.path.basename(path),
        "fileBytes": len(raw),
    }

    # --- 头部 -----------------------------------------------------------
    magic = r.utf()
    result["header"] = {"magic": magic}
    if magic != "rustedWarfareReplay":
        raise ValueError("不是铁锈战争回放文件（头部为 %r）" % magic)
    game_code = r.int()
    fmt_ver = r.int()
    game_ver = r.utf()
    aq = r.boolean()
    result["header"].update({
        "gameVersionCode": game_code,     # kVarT.a(true)，构建号（1.15 = 176）
        "formatVersion": fmt_ver,          # 存档格式版本（1.15 = 96）
        "gameVersionString": game_ver,     # "1.15"
        "aq": aq,                          # 演示/特殊标志
    })

    # --- 初始存档块 gamesave --------------------------------------------
    name, payload = read_block(r)
    if name != "gamesave":
        raise ValueError("期望 'gamesave' 块，实际为 %r" % name)

    sr = Reader(payload)
    inner_magic = sr.utf()                 # "rustedWarfareSave"
    sr.int()
    sr.int()
    sr.boolean()
    sub_name, sub_payload = read_block(sr)  # "saveCompression"
    result["saveHeader"] = {"saveMagic": inner_magic, "compressionBlock": sub_name}
    inner = unwrap_gzip(sub_payload)
    try:
        result["setup"] = parse_initial_save(inner, warnings)
    except NotImplementedError as exc:
        result["setup"] = {"partial": True, "reason": str(exc)}
    # 存档尾部：mark + "<SAVE END>"
    try:
        sr.mark("save-end")
        result["saveTail"] = sr.utf()
    except Exception:
        result["saveTail"] = None

    # --- 命令流 ---------------------------------------------------------
    commands = []
    chats = []
    checksums = []
    extra_checksums = []
    resyncs = []
    end_info = None

    while not r.eof():
        start = r.pos
        blk_name, blk_payload = read_block(r)
        br = Reader(blk_payload, version=fmt_ver)
        if blk_name == "rc":
            frame = br.int()
            try:
                cmd = parse_command(br)
                cmd["frame"] = frame
                cmd["type"] = "command"
                commands.append(cmd)
            except Exception as exc:
                warnings.append("帧 %d 的命令解析失败: %s" % (frame, exc))
                commands.append({"type": "command", "frame": frame, "_error": str(exc)})
        elif blk_name == "wait":
            commands.append({"type": "wait", "frame": br.int()})
        elif blk_name == "cs":
            frame = br.int()
            val = br.long()
            checksums.append({"frame": frame, "checksum": val})
        elif blk_name == "es":
            frame = br.int()
            blob = br.bytes_blob()
            esr = Reader(blob, version=fmt_ver)
            trace = esr.byte()
            count = esr.int()
            values = [esr.long() for _ in range(count)]
            extra_checksums.append({"frame": frame, "trace": trace, "values": values})
        elif blk_name == "resync":
            frame = br.int()
            h = br.int()
            i = br.int()
            j = br.f32()
            k = br.f32()
            blob = br.bytes_blob()
            resyncs.append({"frame": frame, "h": h, "i": i, "f": j, "g": k,
                            "saveBytes": len(blob)})
        elif blk_name == "chat":
            frame = br.int()
            author = br.int()
            chats.append({"frame": frame, "authorTeamId": author,
                          "from": br.nullable_utf(), "message": br.nullable_utf()})
        elif blk_name == "end":
            end_info = {"atByte": start}
        elif blk_name == "endReplayMetaData":
            br.byte()
            meta = {
                "lastFrame": br.int(),
                "time": br.int(),
                "commandCount": br.int(),
                "resyncCount": br.int(),
            }
            meta["raw"] = br.utf()
            result["metadata"] = meta
        else:
            warnings.append("未知块: %r (%d 字节)" % (blk_name, len(blk_payload)))

    result["commands"] = commands
    result["chats"] = chats
    result["checksums"] = checksums
    result["extraChecksums"] = extra_checksums
    result["resyncs"] = resyncs
    result["end"] = end_info
    result.setdefault("metadata", None)
    result["warnings"] = warnings

    # --- 汇总 -----------------------------------------------------------
    cmd_only = [c for c in commands if c.get("type") == "command" and "_error" not in c]
    result["summary"] = {
        "totalBlocks": len(commands),
        "commandCount": len(cmd_only),
        "waitCount": sum(1 for c in commands if c.get("type") == "wait"),
        "chatCount": len(chats),
        "checksumCount": len(checksums),
        "resyncCount": len(resyncs),
        "firstFrame": cmd_only[0]["frame"] if cmd_only else None,
        "lastFrame": cmd_only[-1]["frame"] if cmd_only else None,
    }
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_info(path):
    d = parse_replay(path)
    h = d["header"]
    s = d.get("setup", {})
    print("文件      : %s (%d bytes)" % (d["file"], d["fileBytes"]))
    print("游戏版本  : %s  (构建号 %d, 存档格式 %d)"
          % (h["gameVersionString"], h["gameVersionCode"], h["formatVersion"]))
    print("地图      : %s" % s.get("mapPath"))
    rules = (s.get("gameSetup") or {}).get("rules")
    if rules:
        print("规则      : 起始资金=%s 迷雾=%s 起始单位=%s 收入倍率=%s 随机种子=%s 队伍上限=%s"
              % (rules.get("startingCredits"), rules.get("fogMode"), rules.get("startingUnits"),
                 rules.get("incomeMultiplier"), rules.get("randomSeed"), rules.get("teamUnitCap")))
    teams = s.get("teams") or []
    names = [t.get("name") for t in teams if t.get("notNull")]
    print("队伍      : %d 个 (%s)" % (len(names), ", ".join(str(n) for n in names)))
    print("对象(单位): %d" % (s.get("objectCount", 0)))
    m = d.get("metadata") or {}
    print("时长      : %s 帧 (约 %.1f 分钟)" % (m.get("lastFrame"), (m.get("time") or 0) / 60000.0))
    print("汇总      : %s" % json.dumps(d["summary"], ensure_ascii=False))
    if d["warnings"]:
        print("警告      : %s" % "; ".join(d["warnings"]))
    bad = [c for c in d["commands"] if c.get("type") == "command" and c.get("_error")]
    if bad:
        print("解析失败命令: %d 条" % len(bad))
    else:
        print("命令解析  : 全部成功")


def cmd_dump(path, out):
    d = parse_replay(path)
    text = json.dumps(d, ensure_ascii=False, indent=2)
    if out:
        open(out, "w", encoding="utf-8").write(text)
        print("已写出 %s (%d bytes)" % (out, len(text)))
    else:
        print(text)


def cmd_list(path):
    files = sorted(glob.glob(os.path.join(path, "**", "*.replay"), recursive=True))
    if not files:
        files = sorted(glob.glob(os.path.join(path, "*.replay")))
    for f in files:
        try:
            d = parse_replay(f)
            m = d.get("metadata") or {}
            print("%-60s %-8s %-6s %5s cmd  %6s frames" % (
                os.path.basename(f)[:60],
                d["header"]["gameVersionString"],
                (d.get("setup") or {}).get("mapPath", "?")[-8:],
                d["summary"]["commandCount"],
                m.get("lastFrame")))
        except Exception as exc:
            print("%-60s ERROR %s" % (os.path.basename(f)[:60], exc))


def main():
    ap = argparse.ArgumentParser(description="铁锈战争 .replay 回放解析器")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("info", "dump", "list", "viewer"):
        p = sub.add_parser(name)
        p.add_argument("path")
        if name in ("dump", "viewer"):
            p.add_argument("-o", "--out", default=None)
        if name == "viewer":
            p.add_argument("--assets", default=None, help="APK assets 目录（用于取地图图片）")
    args = ap.parse_args()
    if args.cmd == "info":
        cmd_info(args.path)
    elif args.cmd == "dump":
        cmd_dump(args.path, args.out)
    elif args.cmd == "list":
        cmd_list(args.path)
    elif args.cmd == "viewer":
        from viewer import build_viewer
        build_viewer(args.path, args.out, args.assets)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
