"""
独立 smoke test：在容器内直接跑可以验证 video_parse 的几条主流程。
用法：docker exec qqbot-nonebot python /app/plugins/chat/_smoke_test.py
"""
import asyncio
import importlib.util
import sys
import types


def load_as(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def main():
    # 用 ModuleType 做假包，绕开 plugins/chat/__init__.py 里的 nonebot 调用
    sys.modules.setdefault("plugins", types.ModuleType("plugins"))
    sys.modules.setdefault("plugins.chat", types.ModuleType("plugins.chat"))

    load_as("plugins.chat.ai_client", "/app/plugins/chat/ai_client.py")
    load_as("plugins.chat.file_server", "/app/plugins/chat/file_server.py")
    vp = load_as("plugins.chat.video_parse", "/app/plugins/chat/video_parse.py")
    pf = load_as("plugins.chat.profile", "/app/plugins/chat/profile.py")

    print("=== smoke test ===")

    async def run():
        r = await vp._fetch_via_douyin_api("https://v.douyin.com/XABjEeywRuc/")
        v_ok = bool(r and r.get("video_url"))
        v_title = (r.get("title", "") if r else "")[:30]
        print(f"[1] douyin video : ok={v_ok} title={v_title!r}")

        r2 = await vp._fetch_via_douyin_api("https://v.douyin.com/Y6cICbHnRQA/")
        img_ok = bool(r2 and r2.get("platform") == "抖音图文")
        img_thumb = (r2.get("thumb_url", "") if r2 else "")[:60]
        print(f"[2] douyin image : ok={img_ok} platform={r2.get('platform') if r2 else None} thumb={img_thumb!r}")

        r3 = await vp._fetch_via_bilibili_api("https://www.bilibili.com/video/BV1GJ411x7h7")
        bili_ok = bool(r3 and r3.get("thumb_url", "").startswith("https://"))
        print(f"[3] bilibili     : ok={bili_ok} title={(r3.get('title','') if r3 else '')[:30]!r}")

        # profile 模块的三个查询函数：top_speakers / personal_stats / group_total_today
        # 灌点假数据：alice 3 条、bob 1 条、carol 2 条；ghost 没说过话
        pf.record(91001, 11, "alice", "x1")
        pf.record(91001, 11, "alice", "x2")
        pf.record(91001, 11, "alice", "x3")
        pf.record(91001, 22, "bob", "y1")
        pf.record(91001, 33, "carol", "z1")
        pf.record(91001, 33, "carol", "z2")
        top = pf.top_speakers(91001, 5)
        prof_top_ok = (
            len(top) == 3
            and top[0] == (11, "alice", 3)
            and top[1] == (33, "carol", 2)
            and top[2] == (22, "bob", 1)
        )
        print(f"[4] profile top  : ok={prof_top_ok} top={top}")

        alice_stats = pf.personal_stats(91001, 11)
        ghost_stats = pf.personal_stats(91001, 99)
        prof_stats_ok = (
            alice_stats == (3, 3, 1, 3) and ghost_stats == (0, 0, 0, 3)
        )
        print(f"[5] profile stats: ok={prof_stats_ok} alice={alice_stats} ghost={ghost_stats}")

        grp = pf.group_total_today(91001)
        empty_grp = pf.group_total_today(99999)
        prof_grp_ok = (grp == (6, 3) and empty_grp == (0, 0))
        print(f"[6] profile group: ok={prof_grp_ok} grp={grp} empty={empty_grp}")

        print("=== summary ===")
        results = [v_ok, img_ok, bili_ok, prof_top_ok, prof_stats_ok, prof_grp_ok]
        print(f"PASS {sum(results)}/{len(results)}")
        sys.exit(0 if all(results) else 1)

    asyncio.run(run())


if __name__ == "__main__":
    main()
