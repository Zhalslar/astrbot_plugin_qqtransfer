import asyncio
from datetime import datetime
import random
import re
from astrbot.api.event import filter
from astrbot import logger
from astrbot.api.star import Context, Star, register
from astrbot.core import AstrBotConfig
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.message.components import Plain, Record
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

# 服务端请求指令格式
server_pattern = r"(?P<server_id>\d+)_(?P<client_id>\d+)_+(?P<group_id>\d+)_+(?P<user_id>\d+)_+(?P<text>.+)_+(?P<media_type>mp3|txt|jpg|mp4|json)$"

# 客户端接收文件名格式
file_name_pattern = r"^(\w+)_([\w-]+)_([\w-]+)_([\w-]+)_([\w-]+)_([\w-]+\.[\w-]+)$"


@register(
    "astrbot_plugin_qqtransfer",
    "Zhalslar",
    "基于QQ的文件传输插件",
    "1.0.0",
    "https://github.com/Zhalslar/astrbot_plugin_qqtransfer",
)
class QQTransferPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 中转群
        self.trans_group: str = config.get("trans_group", "0")

        # 是否启用服务端
        self.enable_server: bool = config.get("enable_server", False)
        # 作为服务端时，客服端白名单
        self.server_client_white_list: list = config.get("server_client_white_list", [])

        # 是否启用客户端
        self.enable_client: bool = config.get("enable_client", False)
        # 作为客户端时，服务端白名单，启用时必须配置
        self.client_server_white_list: list = config.get("client_server_white_list", [])

        # 是否启用文件撤回
        self.enable_delete: bool = config.get("enable_delete", False)
        # 撤回文件时间
        self.delete_time: int = config.get("delete_time", 16)

        # 发送语音概率
        self.send_record_probability: float = config.get(
            "send_record_probability", 0.15
        )
        # 最大文本长度
        self.max_resp_text_len: int = config.get("max_resp_text_len", 50)

    async def tts_server(self, text: str, file_name: str) -> str | None:
        """调用TTS服务"""
        # 服务端开关
        if not self.enable_server:
            logger.error("未启用服务端，TTS服务不启动")
            return None
        gpt_sovits_plugin = self.context.get_registered_star(
            "astrbot_plugin_GPT_SoVITS"
        )
        if gpt_sovits_plugin.activated:
            gpt_sovits_plugin_cls = gpt_sovits_plugin.star_cls
            save_path: str | None = await gpt_sovits_plugin_cls.tts_sever( # type: ignore
                text, file_name
            )
            if save_path:
                return save_path
            else:
                logger.error("TTS服务调用失败")
                return None
        else:
            logger.error("astrbot_plugin_GPT_SoVITS插件未加载，无法调用TTS服务")
            return None

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AiocqhttpMessageEvent):
        """将LLM生成的文本按概率生成指令，并调用服务端生成语音"""
        # 未配置服务端白名单则不处理
        if not self.client_server_white_list:
            return
        # 概率控制
        if random.random() > self.send_record_probability:
            return
        chain = event.get_result().chain
        seg = chain[0]
        # 仅允许只含有单条文本的消息链通过
        if not (len(chain) == 1 and isinstance(seg, Plain)):
            return
        # bot将要发送的的文本
        resp_text = seg.text
        # 仅允许一定长度以下的文本通过
        if len(resp_text) > self.max_resp_text_len:
            return

        server_id = self.client_server_white_list[0]
        client_id = event.get_self_id()
        group_id = event.get_group_id() or "0"
        user_id = event.get_sender_id()
        media_type = "mp3"
        command = f"{server_id}_{client_id}_{group_id}_{user_id}_{resp_text}_{media_type}"

        # 向中转群发送请求
        client = event.bot
        await client.send_group_msg(group_id=int(self.trans_group), message=command)

        # 清空消息链
        chain.clear()

    @filter.regex(server_pattern)
    async def server_command(self, event: AiocqhttpMessageEvent):
        """服务器监控，监控中转群中的请求指令"""
        # 服务端开关
        if not self.enable_server:
            return

        # 匹配请求指令
        match = re.search(server_pattern, event.message_str)
        if not match:
            return

        # 获取请求信息
        server_id = match.group("server_id")
        client_id = match.group("client_id")
        target_group_id = match.group("group_id")
        target_user_id = match.group("user_id")
        text = match.group("text")
        media_type = match.group("media_type")
        if not text:
            return
        # 验证是否是向自己发送的请求
        if server_id != event.get_self_id():
            return

        # 验证客户端ID是否在白名单中
        if (
            self.server_client_white_list
            and client_id not in self.server_client_white_list
        ):
            return

        # 生成文件名
        name_text = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]", "", text)[:4]
        current_time = datetime.now().strftime("%Y%m%d%H%M%S")
        file_name = f"{server_id}_{client_id}_{target_group_id}_{target_user_id}_{name_text}_{current_time}.{media_type}"

        # 调用对应媒体服务
        file = None
        if media_type == "mp3":
            file = await self.tts_server(text, file_name)
        if not file:
            yield event.plain_result("TTS服务调用失败")
            return

        # 以文件形式发送
        payloads = {
            "group_id": self.trans_group,
            "message": [{"type": "file", "data": {"file": file, "name": file_name}}],
        }

        # 验证服务端ID
        if (
            self.server_client_white_list
            and server_id not in self.server_client_white_list
        ):
            return

        # 发送文件
        client = event.bot
        result = await client.call_action("send_group_msg", **payloads)
        logger.info(f"服务端{server_id}成功发送文件: {file_name}")

        # 撤回文件
        if self.enable_delete:
            try:
                await asyncio.sleep(self.delete_time)
                await client.delete_msg(message_id=result["message_id"])
            except Exception as e:
                logger.warning(f"撤回文件失败: {e}")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def client_monitor(self, event: AiocqhttpMessageEvent):
        """客户端监控，监控中转群的文件"""
        # 客户端开关
        if not self.enable_client:
            return

        # 匹配文件上传事件
        raw_message = getattr(event.message_obj, "raw_message", None)
        if not raw_message or raw_message.get("notice_type") != "group_upload":
            return

        # 过滤机器人自己发送的文件
        if event.get_sender_id() == event.get_self_id():
            return

        # 获取消息信息
        group_id: int = raw_message.get("group_id", 0)
        file: dict = raw_message.get("file")
        file_id = file["id"]
        file_name = file["name"]
        # 验证文件名格式(server_id_client_id_group_id_user_id_text_date_time.media_type)
        if not re.match(file_name_pattern, file_name):
            return

        # 解析文件名中包含的信息
        infos: list = file_name.split("_")
        server_id: str = str(infos[0])
        client_id: str = str(infos[1])
        target_group_id: int = int(infos[2])
        target_user_id: int = int(infos[3])
        # command: str = str(infos[4])
        # time = infos[5].split(".")[0]
        media_type: str = infos[5].split(".")[1]

        # 验证服务端ID是否在白名单中
        if (
            self.server_client_white_list
            and server_id not in self.server_client_white_list
        ):
            return

        # 验证客户端ID是否是自己
        if client_id != event.get_self_id():
            return

        # 记录日志
        logger.info(f"客户端{client_id}收到来自{server_id}的文件: {file_name}")

        # 获取文件url
        client = event.bot
        file_url_data = await client.get_group_file_url(
            group_id=group_id, file_id=file_id
        )
        file_url = file_url_data.get("url")

        # 解析、构造消息
        obmessage = None
        if media_type == "mp3":
            obmessage = await event._parse_onebot_json(
                MessageChain(chain=[Record(file=str(file_url))])  # type: ignore
            )

        # 发送消息
        if obmessage:
            if target_group_id:
                await client.send_group_msg(group_id=int(target_group_id), message=obmessage)
            else:
                await client.send_private_msg(user_id=int(target_user_id), message=obmessage)
