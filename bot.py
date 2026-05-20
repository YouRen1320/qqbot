"""
NoneBot2 入口
- 加载 OneBot V11 适配器与 plugins 目录下的所有插件
- 监听 0.0.0.0:8080，NapCat 通过 Docker 内部网络（ws://nonebot:8080）反向连接
"""
import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

nonebot.init()

driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

nonebot.load_plugins("plugins")

if __name__ == "__main__":
    nonebot.run()
