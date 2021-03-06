# coding=utf-8
import telegram
import telegram.ext
import config
import datetime
import utils
import urllib
import logging
import time
import magic
import os
import re
import mimetypes
import pydub
import threading
import traceback
from . import db, speech
from .whitelisthandler import WhitelistHandler
from channel import EFBChannel, EFBMsg, MsgType, MsgSource, TargetType, ChannelType
from channelExceptions import EFBChatNotFound, EFBMessageTypeNotSupported
from .msgType import get_msg_type, TGMsgType
from moviepy.editor import VideoFileClip


class Flags:
    # General Flags
    CANCEL_PROCESS = "cancel"
    # Chat linking
    CONFIRM_LINK = 0x11
    EXEC_LINK = 0x12
    # Start a chat
    START_CHOOSE_CHAT = 0x21
    # Command
    COMMAND_PENDING = 0x31


class TelegramChannel(EFBChannel):
    """
    EFB Channel - Telegram (Master)
    Requires python-telegram-bot

    Author: Eana Hufwe <https://github.com/blueset>

    External Services:
        You may need API keys from following service providers to enjoy more functions.
        Baidu Speech Recognition API: http://yuyin.baidu.com/
        Bing Speech API: https://www.microsoft.com/cognitive-services/en-us/speech-api

    Additional configs:
    eh_telegram_master = {
        "token": "Telegram bot token",
        "admins": [12345678, 87654321],
        "bing_speech_api": ["token1", "token2"],
        "baidu_speech_api": {
            "app_id": 123456,
            "api_key": "APIkey",
            "secret_key": "secret_key"
        }
    }
    """

    # Meta Info
    channel_name = "Telegram Master"
    channel_emoji = "✈"
    channel_id = "eh_telegram_master"
    channel_type = ChannelType.Master

    # Data
    slaves = None
    bot = None
    msg_status = {}
    msg_storage = {}
    me = None

    def __init__(self, queue, slaves):
        """
        Initialization.

        Args:
            queue (queue.Queue): global message queue
            slaves (dict): Dictionary of slaves
        """
        super().__init__(queue)
        self.slaves = slaves
        try:
            self.bot = telegram.ext.Updater(config.eh_telegram_master['token'])
        except (AttributeError, KeyError):
            raise ValueError("Token is not properly defined. Please define it in `config.py`.")
        mimetypes.init()
        self.logger = logging.getLogger("plugins.%s.TelegramChannel" % self.channel_id)
        self.me = self.bot.bot.get_me()
        self.bot.dispatcher.add_handler(WhitelistHandler(config.eh_telegram_master['admins']))
        self.bot.dispatcher.add_handler(telegram.ext.CommandHandler("link", self.link_chat_show_list, pass_args=True))
        self.bot.dispatcher.add_handler(telegram.ext.CommandHandler("chat", self.start_chat_list, pass_args=True))
        self.bot.dispatcher.add_handler(telegram.ext.CommandHandler("recog", self.recognize_speech, pass_args=True))
        self.bot.dispatcher.add_handler(telegram.ext.CallbackQueryHandler(self.callback_query_dispatcher))
        self.bot.dispatcher.add_handler(telegram.ext.CommandHandler("start", self.start, pass_args=True))
        self.bot.dispatcher.add_handler(telegram.ext.CommandHandler("extra", self.extra_help))
        self.bot.dispatcher.add_handler(telegram.ext.RegexHandler(r"^/(?P<id>[0-9]+)_(?P<command>[a-z0-9_-]+)", self.extra_call, pass_groupdict=True))
        self.bot.dispatcher.add_handler(telegram.ext.MessageHandler(
            telegram.ext.Filters.text |
            telegram.ext.Filters.photo |
            telegram.ext.Filters.sticker |
            telegram.ext.Filters.document |
            telegram.ext.Filters.venue |
            telegram.ext.Filters.location |
            telegram.ext.Filters.audio |
            telegram.ext.Filters.voice,
            self.msg
        ))
        self.bot.dispatcher.add_error_handler(self.error)

    # Truncate string by bytes
    # Written by Mark Tolonen
    # http://stackoverflow.com/a/13738452/1989455

    def _utf8_lead_byte(self, b):
        """A UTF-8 intermediate byte starts with the bits 10xxxxxx."""
        return (b & 0xC0) != 0x80

    def _utf8_byte_truncate(self, text, max_bytes):
        """If text[max_bytes] is not a lead byte, back up until a lead byte is
        found and truncate before that character."""
        utf8 = text.encode('utf8')
        if len(utf8) <= max_bytes:
            return utf8.decode()
        i = max_bytes
        while i > 0 and not self._utf8_lead_byte(utf8[i]):
            i -= 1
        return utf8[:i].decode()

    def callback_query_dispatcher(self, bot, update):
        """
        Dispatch a callback query based on the message session status.

        Args:
            bot (telegram.bot): bot
            update (telegram.Update): update
        """
        # Get essential information about the query
        query = update.callback_query
        chat_id = query.message.chat.id
        text = query.data
        msg_id = update.callback_query.message.message_id
        msg_status = self.msg_status.get(msg_id, None)
        # dispatch the query
        if msg_status in [Flags.CONFIRM_LINK]:
            self.link_chat_confirm(bot, chat_id, msg_id, text)
        elif msg_status in [Flags.EXEC_LINK]:
            self.link_chat_exec(bot, chat_id, msg_id, text)
        elif msg_status == Flags.START_CHOOSE_CHAT:
            self.make_chat_head(bot, chat_id, msg_id, text)
        elif msg_status == Flags.COMMAND_PENDING:
            self.command_exec(bot, chat_id, msg_id, text)
        else:
            bot.editMessageText(text="Session expired. Please try again. (SE01)",
                                chat_id=chat_id,
                                message_id=msg_id)

    @staticmethod
    def _reply_error(bot, update, errmsg):
        """
        A wrap that directly reply a message with error details.

        Returns:
            telegram.Message: Message sent
        """
        return bot.sendMessage(update.message.chat.id, errmsg, reply_to_message_id=update.message.message_id)

    def process_msg(self, msg):
        """
        Process a message from slave channel and deliver it to the user.

        Args:
            msg (EFBMsg): The message.
        """
        try:
            xid = datetime.datetime.now().timestamp()
            self.logger.debug("%s, Msg text: %s", xid, msg.text)
            self.logger.debug("%s, process_msg_step_0", xid)
            chat_uid = "%s.%s" % (msg.channel_id, msg.origin['uid'])
            tg_chat = db.get_chat_assoc(slave_uid=chat_uid) or False
            msg_prefix = ""
            tg_chat_assoced = False
            if not msg.source == MsgSource.Group:
                msg.member = {"uid": -1, "name": "", "alias": ""}

            # Generate chat text template & Decide type target
            tg_dest = config.eh_telegram_master['admins'][0]
            self.logger.debug("%s, process_msg_step_1, tg_dest=%s", xid, tg_dest)
            if msg.source == MsgSource.Group:
                msg_prefix = msg.member['alias'] if msg.member['name'] == msg.member['alias'] else "%s (%s)" % (
                    msg.member['alias'], msg.member['name'])
            if tg_chat:  # if this chat is linked
                tg_dest = int(tg_chat.split('.')[1])
                tg_chat_assoced = True
                if msg_prefix:  # if group message
                    msg_template = "%s:\n%s" % (msg_prefix, "%s")
                else:
                    msg_template = "%s"
            elif msg.source == MsgSource.User:
                emoji_prefix = msg.channel_emoji + utils.Emojis.get_source_emoji(msg.source)
                name_prefix = msg.origin["alias"] if msg.origin["alias"] == msg.origin["name"] else "%s (%s)" % (
                    msg.origin["alias"], msg.origin["name"])
                msg_template = "%s %s:\n%s" % (emoji_prefix, name_prefix, "%s")
            elif msg.source == MsgSource.Group:
                emoji_prefix = msg.channel_emoji + utils.Emojis.get_source_emoji(msg.source)
                name_prefix = msg.origin["alias"] if msg.origin["alias"] == msg.origin["name"] else "%s (%s)" % (
                    msg.origin["alias"], msg.origin["name"])
                msg_template = "%s %s [%s]:\n%s" % (emoji_prefix, msg_prefix, name_prefix, "%s")
            elif msg.source == MsgSource.System:
                msg_template = "System Message: %s"

            # Type dispatching
            self.logger.debug("%s, process_msg_step_2", xid)
            append_last_msg = False
            if msg.type in [MsgType.Text, MsgType.Link]:
                if tg_chat_assoced:
                    last_msg = db.get_last_msg_from_chat(tg_dest)
                    if last_msg:
                        if last_msg.msg_type == "Text":
                            append_last_msg = str(last_msg.slave_origin_uid) == "%s.%s" % (msg.channel_id, msg.origin['uid'])
                            if msg.source == MsgSource.Group:
                                append_last_msg &= str(last_msg.slave_member_uid) == str(msg.member['uid'])
                            append_last_msg &= datetime.datetime.now() - last_msg.time <= datetime.timedelta(seconds=self._flag('join_msg_threshold_secs', 15))
                        else:
                            append_last_msg = False
                    else:
                        append_last_msg = False
                    self.logger.debug("Text: Append last msg: %s", append_last_msg)
                self.logger.debug("%s, process_msg_step_3_0, tg_dest = %s, tg_chat_assoced = %s, append_last_msg = %s", xid,
                                  tg_dest, tg_chat_assoced, append_last_msg)
                if tg_chat_assoced and append_last_msg:
                    self.logger.debug("%s, process_msg_step_3_0_1", xid)
                    msg.text = "%s\n%s" % (last_msg.text, msg.text)
                    tg_msg = self.bot.bot.editMessageText(chat_id=tg_dest,
                                                          message_id=last_msg.master_msg_id.split(".", 1)[1],
                                                          text=msg_template % msg.text)
                else:
                    self.logger.debug("%s, process_msg_step_3_0_3", xid)
                    tg_msg = self.bot.bot.sendMessage(tg_dest, text=msg_template % msg.text)
                    self.logger.debug("%s, process_msg_step_3_0_4, tg_msg = %s", xid, tg_msg)
                self.logger.debug("%s, process_msg_step_3_1", xid)
            elif msg.type in [MsgType.Image, MsgType.Sticker]:
                self.logger.debug("%s, process_msg_step_3_2", xid)
                self.logger.info("Received %s \nPath: %s\nSize: %s\nMIME: %s", msg.type, msg.path,
                                 os.stat(msg.path).st_size, msg.mime)
                if os.stat(msg.path).st_size == 0:
                    os.remove(msg.path)
                    return self.bot.bot.sendMessage(tg_dest, msg_template % ("Error: Empty %s received. (MS01)" % msg.type))
                if not msg.text:
                    if MsgType.Image:
                        msg.text = "sent a picture."
                    elif msg.type == MsgType.Sticker:
                        msg.text = "sent a sticker."
                if msg.mime == "image/gif":
                    tg_msg = self.bot.bot.sendDocument(tg_dest, msg.file, caption=msg_template % msg.text)
                else:
                    tg_msg = self.bot.bot.sendPhoto(tg_dest, msg.file, caption=msg_template % msg.text)
                os.remove(msg.path)
                self.logger.debug("%s, process_msg_step_3_3", xid)
            elif msg.type == MsgType.File:
                if os.stat(msg.path).st_size == 0:
                    os.remove(msg.path)
                    return self.bot.bot.sendMessage(tg_dest, msg_template % ("Error: Empty %s received. (MS02)" % msg.type))
                if not msg.text:
                    file_name = os.path.basename(msg.path)
                    msg.text = "sent a file."
                else:
                    file_name = msg.text
                tg_msg = self.bot.bot.sendDocument(tg_dest, msg.file, caption=msg_template % msg.text, filename=file_name)
                os.remove(msg.path)
            elif msg.type == MsgType.Audio:
                if os.stat(msg.path).st_size == 0:
                    os.remove(msg.path)
                    return self.bot.bot.sendMessage(tg_dest, msg_template % ("Error: Empty %s received. (MS03)" % msg.type))
                msg.text = msg.text or ''
                self.logger.debug("%s, process_msg_step_4_1, no_conversion = %s", xid, self._flag("no_conversion", False))
                if self._flag("no_conversion", False):
                    self.logger.debug("%s, process_msg_step_4_2, mime = %s", xid, msg.mime)
                    if msg.mime == "audio/mpeg":
                        tg_msg = self.bot.bot.sendAudio(tg_dest, msg.file, caption=msg_template % msg.text)
                    else:
                        tg_msg = self.bot.bot.sendDocument(tg_dest, msg.file, caption=msg_template % msg.text)
                else:
                    pydub.AudioSegment.from_file(msg.file).export("%s.ogg" % msg.path, format="ogg", codec="libopus")
                    ogg_file = open("%s.ogg" % msg.path, 'rb')
                    tg_msg = self.bot.bot.sendVoice(tg_dest, ogg_file, caption=msg_template % msg.text)
                    os.remove("%s.ogg" % msg.path)
                os.remove(msg.path)
            elif msg.type == MsgType.Location:
                self.logger.info("---\nsending venue\nlat: %s, long: %s\ntitle: %s\naddr: %s", msg.attributes['latitude'], msg.attributes['longitude'], msg.text, msg_template % "")
                tg_msg = self.bot.bot.sendVenue(tg_dest, latitude=msg.attributes['latitude'],
                                                longitude=msg.attributes['longitude'], title=msg.text,
                                                address=msg_template % "")
            elif msg.type == MsgType.Video:
                if os.stat(msg.path).st_size == 0:
                    os.remove(msg.path)
                    return self.bot.bot.sendMessage(tg_dest, msg_template % ("Error: Empty %s recieved" % msg.type))
                if not msg.text:
                    msg.text = "sent a video."
                tg_msg = self.bot.bot.sendVideo(tg_dest, video=msg.file, caption=msg_template % msg.text)
                os.remove(msg.path)
            elif msg.type == MsgType.Command:
                buttons = []
                for i, ival in enumerate(msg.attributes['commands']):
                    buttons.append([telegram.InlineKeyboardButton(ival['name'], callback_data=str(i))])
                tg_msg = self.bot.bot.send_message(tg_dest, msg_template % msg.text, reply_markup=telegram.InlineKeyboardMarkup(buttons))
                self.msg_status[tg_msg.message_id] = Flags.COMMAND_PENDING
                self.msg_storage[tg_msg.message_id] = {"channel": msg.channel_id, "text": msg_template % msg.text, "commands": msg.attributes['commands']}
            else:
                tg_msg = self.bot.bot.sendMessage(tg_dest, msg_template % "Unsupported incoming message type. (UT01)")
            self.logger.debug("%s, process_msg_step_4", xid)
            if msg.source in (MsgSource.User, MsgSource.Group):
                msg_log = {"master_msg_id": "%s.%s" % (tg_msg.chat.id, tg_msg.message_id),
                           "text": msg.text,
                           "msg_type": msg.type,
                           "sent_to": "Master",
                           "slave_origin_uid": "%s.%s" % (msg.channel_id, msg.origin['uid']),
                           "slave_origin_display_name": msg.origin['alias'],
                           "slave_member_uid": msg.member['uid'],
                           "slave_member_display_name": msg.member['alias']}
                if tg_chat_assoced and append_last_msg:
                    msg_log['update'] = True
                db.add_msg_log(**msg_log)
            self.logger.debug("%s, process_msg_step_5", xid)
        except Exception as e:
            self.logger.error(repr(e) + traceback.format_exc())

    def slave_chats_pagination(self, message_id, offset=0, filter=""):
        """
        Generate a list of (list of) `InlineKeyboardButton`s of chats in slave channels,
        based on the status of message located by `message_id` and the paging from
        `offset` value.

        Args:
            message_id (int): Message ID for generating the buttons list.
            offset (int): Offset for pagination

        Returns:
            tuple (str, list of list of InlineKeyboardButton):
                A tuple: legend, chat_btn_list
                `legend` is the legend of all Emoji headings in the entire list.
                `chat_btn_list` is a list which can be fit into `telegram.InlineKeyboardMarkup`.
        """
        legend = [
            "%s: Linked" % utils.Emojis.LINK_EMOJI,
            "%s: User" % utils.Emojis.USER_EMOJI,
            "%s: Group" % utils.Emojis.GROUP_EMOJI
        ]

        if self.msg_storage.get(message_id, None):
            chats = self.msg_storage[message_id]['chats']
            channels = self.msg_storage[message_id]['channels']
            count = self.msg_storage[message_id]['count']
        else:
            rfilter = re.compile(filter, re.DOTALL | re.IGNORECASE)
            if filter:
                self.logger.debug("Filter string: %s", filter)
            chats = []
            channels = {}
            for slave_id in self.slaves:
                slave = self.slaves[slave_id]
                slave_chats = slave.get_chats()
                channels[slave.channel_id] = {
                    "channel_name": slave.channel_name,
                    "channel_emoji": slave.channel_emoji
                }
                for chat in slave_chats:
                    c = {
                        "channel_id": slave.channel_id,
                        "channel_name": slave.channel_name,
                        "channel_emoji": slave.channel_emoji,
                        "chat_name": chat['name'],
                        "chat_alias": chat['alias'],
                        "chat_uid": chat['uid'],
                        "type": chat['type']
                    }
                    entry_string = "Channel: %s\nName: %s\nAlias: %s\nID: %s\nType: %s" \
                        % (c['channel_name'], c['chat_name'], c['chat_alias'], c['chat_uid'], c['type'])
                    if not filter or rfilter.search(entry_string):
                        chats.append(c)
            count = len(chats)
            self.msg_storage[message_id] = {
                "offset": offset,
                "count": len(chats),
                "chats": chats.copy(),
                "channels": channels.copy()
            }

        for ch in channels:
            legend.append("%s: %s" % (channels[ch]['channel_emoji'], channels[ch]['channel_name']))

        # Build inline button list
        chat_btn_list = []
        chats_per_page = self._flag("chats_per_page", 10)
        for i in range(offset, min(offset + chats_per_page, count)):
            chat = chats[i]
            linked = utils.Emojis.LINK_EMOJI if bool(db.get_chat_assoc(slave_uid="%s.%s" % (chat['channel_id'], chat['chat_uid']))) else ""
            chat_type = utils.Emojis.get_source_emoji(chat['type'])
            chat_name = chat['chat_alias'] if chat['chat_name'] == chat['chat_alias'] else "%s(%s)" % (chat['chat_alias'], chat['chat_name'])
            button_text = "%s%s: %s %s" % (chat['channel_emoji'], chat_type, chat_name, linked)
            button_callback = "chat %s" % i
            chat_btn_list.append([telegram.InlineKeyboardButton(button_text, callback_data=button_callback)])

        # Pagination
        page_number_row = []

        if offset - chats_per_page >= 0:
            page_number_row.append(telegram.InlineKeyboardButton("< Prev", callback_data="offset %s" % (
                offset - chats_per_page)))
        page_number_row.append(telegram.InlineKeyboardButton("Cancel", callback_data=Flags.CANCEL_PROCESS))
        if offset + chats_per_page < count:
            page_number_row.append(telegram.InlineKeyboardButton("Next >", callback_data="offset %s" % (
                offset + chats_per_page)))
        chat_btn_list.append(page_number_row)

        return legend, chat_btn_list

    def link_chat_show_list(self, bot, update, args=None):
        """
        Show the list of available chats for linking.
        Triggered by `/link`.

        Args:
            bot: Telegram Bot instance
            update: Message update
        """
        args = args or []
        self.link_chat_gen_list(bot, update.message.chat.id, filter=" ".join(args))

    def link_chat_gen_list(self, bot, chat_id, message_id=None, offset=0, filter=""):
        """
        Generate the list for chat linking, and update it to a message.

        Args:
            bot: Telegram Bot instance
            chat_id: Chat ID
            message_id: ID of message to be updated, None to send a new message.
            offset: Offset for pagination.
        """
        if not message_id:
            message_id = bot.sendMessage(chat_id, "Processing...").message_id

        msg_text = "Please choose the chat you want to link with ...\n\nLegend:\n"
        legend, chat_btn_list = self.slave_chats_pagination(message_id, offset, filter=filter)
        for i in legend:
            msg_text += "%s\n" % i

        msg = bot.editMessageText(chat_id=chat_id, message_id=message_id, text=msg_text,
                                   reply_markup=telegram.InlineKeyboardMarkup(chat_btn_list))
        self.msg_status[msg.message_id] = Flags.CONFIRM_LINK

    def link_chat_confirm(self, bot, tg_chat_id, tg_msg_id, callback_uid):
        """
        Confirmation of chat linking. Triggered by callback message on status `Flags.CONFIRM_LINK`.

        Args:
            bot: Telegram Bot instance
            tg_chat_id: Chat ID
            tg_msg_id: Message ID triggered the callback
            callback_uid: Callback message
        """
        if callback_uid.split()[0] == "offset":
            return self.link_chat_gen_list(bot, tg_chat_id, message_id=tg_msg_id, offset=int(callback_uid.split()[1]))
        if callback_uid == Flags.CANCEL_PROCESS:
            txt = "Cancelled."
            self.msg_status.pop(tg_msg_id, None)
            self.msg_storage.pop(tg_msg_id, None)
            return bot.editMessageText(text=txt,
                                       chat_id=tg_chat_id,
                                       message_id=tg_msg_id)
        if callback_uid[:4] != "chat":
            txt = "Invalid parameter (%s). (IP01)" % callback_uid
            self.msg_status.pop(tg_msg_id, None)
            self.msg_storage.pop(tg_msg_id, None)
            return bot.editMessageText(text=txt,
                                       chat_id=tg_chat_id,
                                       message_id=tg_msg_id)

        callback_uid = int(callback_uid.split()[1])
        chat = self.msg_storage[tg_msg_id]['chats'][callback_uid]
        chat_uid = "%s.%s" % (chat['channel_id'], chat['chat_uid'])
        chat_display_name = chat['chat_name'] if chat['chat_name'] == chat['chat_alias'] else "%s(%s)" % (chat['chat_alias'], chat['chat_name'])
        chat_display_name = "'%s' from '%s %s'" % (chat_display_name, chat['channel_emoji'], chat['channel_name'])

        linked = bool(db.get_chat_assoc(slave_uid=chat_uid))
        self.msg_status[tg_msg_id] = Flags.EXEC_LINK
        self.msg_status[chat_uid] = tg_msg_id
        txt = "You've selected chat %s." % chat_display_name
        if linked:
            txt += "\nThis chat has already linked to Telegram."
        txt += "\nWhat would you like to do?"

        if linked:
            btn_list = [telegram.InlineKeyboardButton("Relink", url="https://telegram.me/%s?startgroup=%s" % (
                self.me.username, urllib.parse.quote(" ".join((chat_uid, chat_display_name))))),
                        telegram.InlineKeyboardButton("Unlink", callback_data="unlink %s" % callback_uid)]
        else:
            btn_list = [telegram.InlineKeyboardButton("Link", url="https://telegram.me/%s?startgroup=%s" % (
                self.me.username, urllib.parse.quote(" ".join((chat_uid, chat_display_name)))))]
        btn_list.append(telegram.InlineKeyboardButton("Cancel", callback_data=Flags.CANCEL_PROCESS))

        bot.editMessageText(text=txt,
                            chat_id=tg_chat_id,
                            message_id=tg_msg_id,
                            reply_markup=telegram.InlineKeyboardMarkup([btn_list]))

    def link_chat_exec(self, bot, tg_chat_id, tg_msg_id, callback_uid):
        """
        Action to link a chat. Triggered by callback message with status `Flags.EXEC_LINK`.

        Args:
            bot: Telegram Bot instance
            tg_chat_id: Chat ID
            tg_msg_id: Message ID triggered the callback
            callback_uid: Callback message
        """
        if callback_uid == Flags.CANCEL_PROCESS:
            txt = "Cancelled."
            self.msg_status.pop(tg_msg_id, None)
            self.msg_storage.pop(tg_msg_id, None)

            return bot.editMessageText(text=txt,
                                       chat_id=tg_chat_id,
                                       message_id=tg_msg_id)
        cmd, chat_lid = callback_uid.split()
        chat = self.msg_storage[tg_msg_id]['chats'][int(chat_lid)]
        chat_uid = "%s.%s" % (chat['channel_id'], chat['chat_uid'])
        chat_display_name = chat['chat_name'] if chat['chat_name'] == chat['chat_alias'] else "%s (%s)" % (
            chat['chat_alias'], chat['chat_name'])
        chat_display_name = "'%s' from '%s %s'" % (chat_display_name, chat['channel_emoji'], chat['channel_name']) \
                            if chat['channel_name'] else "'%s'" % chat_display_name
        self.msg_status.pop(tg_msg_id, None)
        self.msg_storage.pop(tg_msg_id, None)
        if cmd == "unlink":
            db.remove_chat_assoc(slave_uid=chat_uid)
            txt = "Chat %s is unlinked." % (chat_display_name)
            return bot.editMessageText(text=txt, chat_id=tg_chat_id, message_id=tg_msg_id)
        txt = "Command '%s' (%s) is not recognised, please try again" % (cmd, callback_uid)
        bot.editMessageText(text=txt, chat_id=tg_chat_id, message_id=tg_msg_id)

    def start_chat_list(self, bot, update, args=None):
        """
        Send a list to for chat list generation.
        Triggered by `/list`.

        Args:
            bot: Telegram Bot instance
            update: Message update
            args: Arguments from the command message
        """
        args = args or []
        msg_id = self.chat_head_req_generate(bot, update.message.from_user.id, filter=" ".join(args))
        self.msg_status[msg_id] = Flags.START_CHOOSE_CHAT

    def chat_head_req_generate(self, bot, chat_id, message_id=None, offset=0, filter=""):
        """
        Generate the list for chat head, and update it to a message.

        Args:
            bot: Telegram Bot instance
            chat_id: Chat ID
            message_id: ID of message to be updated, None to send a new message.
            offset: Offset for pagination.
            filter: Regex String used as a filter.
        """
        if not message_id:
            message_id = bot.sendMessage(chat_id, text="Processing...").message_id

        legend, chat_btn_list = self.slave_chats_pagination(message_id, offset, filter=filter)
        msg_text = "Choose a chat you want to start with...\n\nLegend:\n"
        for i in legend:
            msg_text += "%s\n" % i
        bot.editMessageText(text=msg_text,
                            chat_id=chat_id,
                            message_id=message_id,
                            reply_markup=telegram.InlineKeyboardMarkup(chat_btn_list))
        return message_id

    def make_chat_head(self, bot, tg_chat_id, tg_msg_id, callback_uid):
        """
        Create a chat head. Triggered by callback message with status `Flags.START_CHOOSE_CHAT`.

        Args:
            bot: Telegram Bot instance
            tg_chat_id: Chat ID
            tg_msg_id: Message ID triggered the callback
            callback_uid: Callback message
        """
        if callback_uid.split()[0] == "offset":
            return self.chat_head_req_generate(bot, tg_chat_id, message_id=tg_msg_id, offset=int(callback_uid.split()[1]))
        if callback_uid == Flags.CANCEL_PROCESS:
            txt = "Cancelled."
            self.msg_status.pop(tg_msg_id, None)
            self.msg_storage.pop(tg_msg_id, None)
            return bot.editMessageText(text=txt,
                                       chat_id=tg_chat_id,
                                       message_id=tg_msg_id)

        if callback_uid[:4] != "chat":
            txt = "Invalid parameter. (%s)" % callback_uid
            self.msg_status.pop(tg_msg_id, None)
            self.msg_storage.pop(tg_msg_id, None)
            return bot.editMessageText(text=txt,
                                       chat_id=tg_chat_id,
                                       message_id=tg_msg_id)
        callback_uid = int(callback_uid.split()[1])
        chat = self.msg_storage[tg_msg_id]['chats'][callback_uid]
        chat_uid = "%s.%s" % (chat['channel_id'], chat['chat_uid'])
        chat_display_name = chat['chat_name'] if chat['chat_name'] == chat['chat_alias'] else "%s(%s)" % (
        chat['chat_alias'], chat['chat_name'])
        chat_display_name = "'%s' from '%s %s'" % (chat_display_name, chat['channel_emoji'], chat['channel_name'])
        self.msg_status.pop(tg_msg_id, None)
        self.msg_storage.pop(tg_msg_id, None)
        txt = "Reply to this message to chat with %s." % chat_display_name
        msg_log = {"master_msg_id": "%s.%s" % (tg_chat_id, tg_msg_id),
                   "text": txt,
                   "msg_type": "Text",
                   "sent_to": "Master",
                   "slave_origin_uid": chat_uid,
                   "slave_origin_display_name": chat_display_name,
                   "slave_member_uid": None,
                   "slave_member_display_name": None}
        db.add_msg_log(**msg_log)
        bot.editMessageText(text=txt, chat_id=tg_chat_id, message_id=tg_msg_id)

    def command_exec(self, bot, chat_id, message_id, callback):
        """
        Run a command from a command message.
        Triggered by callback message with status `Flags.COMMAND_PENDING`.

        Args:
            bot: Telegram Bot instance
            chat_id: Chat ID
            message_id: Message ID triggered the callback
            callback: Callback message
        """
        if not callback.isdecimal():
            msg = "Invalid parameter: %s. (CE01)" % callback
            self.msg_status.pop(message_id, None)
            self.msg_storage.pop(message_id, None)
            return bot.editMessageText(text=msg, chat_id=chat_id, message_id=message_id)
        elif not (0 <= int(callback) < len(self.msg_storage[message_id])):
            msg = "Index out of bound: %s. (CE02)" % callback
            self.msg_status.pop(message_id, None)
            self.msg_storage.pop(message_id, None)
            return bot.editMessageText(text=msg, chat_id=chat_id, message_id=message_id)

        callback = int(callback)
        channel_id = self.msg_storage[message_id]['channel']
        command = self.msg_storage[message_id]['commands'][callback]
        msg = self.msg_storage[message_id]['text'] + "\n------\n" + getattr(self.slaves[channel_id], command['callable'])(*command['args'], **command['kwargs'])
        self.msg_status.pop(message_id, None)
        self.msg_storage.pop(message_id, None)
        return bot.editMessageText(text=msg, chat_id=chat_id, message_id=message_id)

    def extra_help(self, bot, update):
        """
        Show list of extra functions and their usage.
        Triggered by `/extra`.

        Args:
            bot: Telegram Bot instance
            update: Message update
        """
        msg = "List of slave channel features:"
        for n, i in enumerate(sorted(self.slaves)):
            i = self.slaves[i]
            msg += "\n\n<b>%s %s</b>" % (i.channel_emoji, i.channel_name)
            xfns = i.get_extra_functions()
            if xfns:
                for j in xfns:
                    fn_name = "/%s_%s" % (n, j)
                    msg += "\n\n%s <i>(%s)</i>\n%s" % (fn_name, xfns[j].name, xfns[j].desc.format(function_name=fn_name))
            else:
                msg += "No command found."
        bot.sendMessage(update.message.chat.id, msg, parse_mode="HTML")

    def extra_call(self, bot, update, groupdict=None):
        """
        Call an extra function from slave channel.

        Args:
            bot: Telegram Bot instance
            update: Message update
            groupdict: Parameters offered by the message
        """
        if int(groupdict['id']) >= len(self.slaves):
            return self._reply_error(bot, update, "Invalid slave channel ID. (XC01)")
        ch = self.slaves[sorted(self.slaves)[int(groupdict['id'])]]
        fns = ch.get_extra_functions()
        if groupdict['command'] not in fns:
            return self._reply_error(bot, update, "Command not found in selected channel. (XC02)")
        header = "%s %s: %s\n-------\n" % (ch.channel_emoji, ch.channel_name, fns[groupdict['command']].name)
        msg = bot.sendMessage(update.message.chat.id, header+"Please wait...")
        result = fns[groupdict['command']](" ".join(update.message.text.split(' ', 1)[1:]))
        bot.editMessageText(text=header+result, chat_id=update.message.chat.id, message_id=msg.message_id)

    def msg(self, bot, update):
        """
        Process, wrap and deliver messages from user.

        Args:
            bot: Telegram Bot instance
            update: Message update
        """
        self.logger.debug("----\nMsg from tg user:\n%s", update.message.to_dict())
        target = None
        if update.message.chat.id != update.message.from_user.id:  # from group
            assoc = db.get_chat_assoc(master_uid="%s.%s" % (self.channel_id, update.message.chat.id))
            if getattr(update.message, "reply_to_message", None):
                try:
                    targetlog = db.get_msg_log(
                        "%s.%s" % (update.message.reply_to_message.chat.id, update.message.reply_to_message.message_id))
                    target = targetlog.slave_origin_uid
                    targetChannel, targetUid = target.split('.', 2)
                except:
                    return self._reply_error(bot, update, "Unknown recipient (UC03).")
        elif (update.message.chat.id == update.message.from_user.id) and getattr(update.message, "reply_to_message",
                                                                                 None):  # reply to user
            assoc = db.get_msg_log("%s.%s" % (
                update.message.reply_to_message.chat.id, update.message.reply_to_message.message_id)).slave_origin_uid
        else:
            return self._reply_error(bot, update, "Unknown recipient (UC01).")
        if not assoc:
            return self._reply_error(bot, update, "Unknown recipient (UC02).")
        self.logger.debug("Destination chat = %s", assoc)
        channel, uid = assoc.split('.', 2)
        if channel not in self.slaves:
            return self._reply_error(bot, update, "Internal error: Channel not found.")
        try:
            m = EFBMsg(self)
            mtype = get_msg_type(update.message)
            # Chat and author related stuff
            m.origin['uid'] = update.message.from_user.id
            if getattr(update.message.from_user, "last_name", None):
                m.origin['alias'] = "%s %s" % (update.message.from_user.first_name, update.message.from_user.last_name)
            else:
                m.origin['alias'] = update.message.from_user.first_name
            if getattr(update.message.from_user, "username", None):
                m.origin['name'] = "@%s" % update.message.from_user.id
            else:
                m.origin['name'] = m.origin['alias']
            m.destination = {
                'channel': channel,
                'uid': uid,
                'name': '',
                'alias': ''
            }
            if target:
                if targetChannel == channel:
                    trgtMsg = EFBMsg(self.slaves[targetChannel])
                    trgtMsg.type = MsgType.Text
                    trgtMsg.text = targetlog.text
                    trgtMsg.member = {
                        "name": targetlog.slave_member_display_name,
                        "alias": targetlog.slave_member_display_name,
                        "uid": targetlog.slave_member_uid
                    }
                    trgtMsg.origin = {
                        "name": targetlog.slave_origin_display_name,
                        "alias": targetlog.slave_origin_display_name,
                        "uid": targetlog.slave_origin_uid.split('.', 2)[1]
                    }
                    m.target = {
                        "type": TargetType.Message,
                        "target": trgtMsg
                    }
            # Type specific stuff
            self.logger.debug("Msg type: %s", mtype)

            if mtype == TGMsgType.Text:
                m.type = MsgType.Text
                m.text = update.message.text
            elif mtype == TGMsgType.Photo:
                m.type = MsgType.Image
                m.text = update.message.caption
                tg_file_id = update.message.photo[-1].file_id
                m.path, m.mime = self._download_file(update.message, tg_file_id, m.type)
                m.file = open(m.path, "rb")
            elif mtype == TGMsgType.Sticker:
                m.type = MsgType.Sticker
                m.text = update.message.sticker.emoji
                tg_file_id = update.message.sticker.file_id
                m.path, m.mime = self._download_file(update.message, tg_file_id, m.type)
                m.file = open(m.path, "rb")
            elif mtype == TGMsgType.Document:
                m.text = update.message.document.file_name
                tg_file_id = update.message.document.file_id
                self.logger.debug("tg: Document file received")
                if update.message.document.mime_type == "video/mp4":
                    self.logger.debug("tg: GIF received")
                    m.type = MsgType.Image
                    m.path, m.mime = self._download_gif(update.message, tg_file_id, m.type)
                else:
                    m.type = MsgType.File
                    m.path, m.mime = self._download_file(update.message, tg_file_id, m.type)
                m.file = open(m.path, "rb")
            elif mtype == TGMsgType.Video:
                m.type = MsgType.Video
                m.text = update.message.document.file_name
                tg_file_id = update.message.document.file_id
                m.path, m.mime = self._download_file(update.message, tg_file_id, m.type)
                m.file = open(m.path, "rb")
            elif mtype == TGMsgType.Audio:
                m.type = MsgType.Audio
                m.text = "%s - %s" % (update.message.audio.title, update.message.audio.perfomer)
                tg_file_id = update.message.audio.file_id
                m.path, m.mime = self._download_file(update.message, tg_file_id, m.type)
            elif mtype == TGMsgType.Voice:
                m.type = MsgType.Audio
                m.text = ""
                tg_file_id = update.message.voice.file_id
                m.path, m.mime = self._download_file(update.message, tg_file_id, m.type)
            elif mtype == TGMsgType.Location:
                m.type = MsgType.Location
                m.text = "Location"
                m.attributes = {
                    "latitude": update.message.location.latitude,
                    "longitude": update.message.location.longitude
                }
            elif mtype == TGMsgType.Venue:
                m.type = MsgType.Location
                m.text = update.message.location.title + "\n" + update.message.location.adderss
                m.attributes = {
                    "latitude": update.message.venue.location.latitude,
                    "longitude": update.message.venue.location.longitude
                }
            else:
                return self._reply_error(bot, update, "Message type not supported. (MN02)")

            self.slaves[channel].send_message(m)
        except EFBChatNotFound:
            return self._reply_error(bot, update, "Internal error: Chat not found in channel. (CN01)")
        except EFBMessageTypeNotSupported:
            return self._reply_error(bot, update, "Message type not supported. (MN01)")

    def _download_file(self, tg_msg, file_id, msg_type):
        """
        Download media file from telegram platform.

        Args:
            tg_msg: Telegram message instance
            file_id: File ID
            msg_type: Type of message

        Returns:
            tuple of str[2]: Full path of the file, MIME type
        """
        path = os.path.join("storage", self.channel_id)
        if not os.path.exists(path):
            os.makedirs(path)
        f = self.bot.bot.getFile(file_id)
        fname = "%s_%s_%s_%s" % (msg_type, tg_msg.chat.id, tg_msg.message_id, int(time.time()))
        fullpath = os.path.join(path, fname)
        f.download(fullpath)
        mime = magic.from_file(fullpath, mime=True)
        if type(mime) is bytes:
            mime = mime.decode()
        ext = mimetypes.guess_extension(mime)
        os.rename(fullpath, "%s.%s" % (fullpath, ext))
        fullpath = "%s.%s" % (fullpath, ext)
        return fullpath, mime

    def _download_gif(self, tg_msg, file_id, msg_type):
        """
        Download and convert GIF image.

        Args:
            tg_msg: Telegram message instance
            file_id: File ID
            msg_type: Type of message

        Returns:
            tuple of str[2]: Full path of the file, MIME type
        """
        fullpath, mime = self._download_file(tg_msg, file_id, msg_type)
        VideoFileClip(fullpath).write_gif(fullpath + ".gif", program="ffmpeg")
        return fullpath + ".gif", "image/gif"

    def start(self, bot, update, args=[]):
        """
        Process bot command `/start`.

        Args:
            bot: Telegram Bot instance
            update: Message update
            args: Arguments from message
        """
        if update.message.from_user.id != update.message.chat.id:  # from group
            chat_uid = args[0]
            chat_display_name = ' '.join(args[1:])
            slave_channel, slave_chat_uid = chat_uid.split('.', 1)
            if slave_channel in self.slaves and chat_uid in self.msg_status:
                db.add_chat_assoc(master_uid="%s.%s" % (self.channel_id, update.message.chat.id), slave_uid=chat_uid)
                txt = "Chat '%s' is now linked." % chat_display_name
                self.msg_status[self.msg_status[chat_uid]] = Flags.EXEC_LINK
                self.msg_storage[self.msg_status[chat_uid]] = {"chats": [{
                    "channel_id": slave_channel,
                    "channel_name": "",
                    "channel_emoji": "",
                    "chat_uid": slave_chat_uid,
                    "chat_name": chat_display_name,
                    "chat_alias": chat_display_name,
                    "type": ""
                }]}
                bot.sendMessage(update.message.chat.id, text=txt)
                bot.editMessageText(chat_id=update.message.from_user.id,
                                    message_id=self.msg_status[chat_uid],
                                    text=txt,
                                    reply_markup=telegram.InlineKeyboardMarkup(
                                        [[telegram.InlineKeyboardButton("Unlink", callback_data="unlink 0")]]))
                self.msg_status.pop(chat_uid, False)
        else:
            txt = "Welcome to EH Forwarder Bot: EFB Telegram Master Channel.\n\n" \
                  "To learn more, please visit https://github.com/blueset/ehForwarderBot ."
            bot.sendMessage(update.message.from_user.id, txt)

    def recognize_speech(self, bot, update, args=[]):
        """
        Recognise voice message. Triggered by `/recog`.

        Args:
            bot: Telegram Bot instance
            update: Message update
            args: Arguments from message
        """

        class speechNotImplemented:
            lang_list = []

            def __init__(self, *args, **kwargs):
                pass

            def recognize(self, *args, **kwargs):
                return ["Not enabled or error in configuration."]

        if not getattr(update.message, "reply_to_message", None):
            txt = "/recog [lang_code]\nReply to a voice with this command to recognize it.\n" \
                  "mples:\n/recog\n/recog zh\n/recog en\n(RS01)"
            return self._reply_error(bot, update, txt)
        if not getattr(update.message.reply_to_message, "voice"):
            return self._reply_error(bot, update,
                                     "Reply only to a voice with this command to recognize it. (RS02)")
        try:
            baidu_speech = speech.BaiduSpeech(config.eh_telegram_master['baidu_speech_api'])
        except:
            baidu_speech = speechNotImplemented()
        try:
            bing_speech = speech.BingSpeech(config.eh_telegram_master['bing_speech_api'])
        except:
            bing_speech = speechNotImplemented()
        if len(args) > 0 and (args[0][:2] not in ['zh', 'en', 'ja'] and args[0] not in bing_speech.lang_list):
            return self._reply_error(bot, update, "Language is not supported. Try with zh, ja or en. (RS03)")
        if update.message.reply_to_message.voice.duration > 60:
            return self._reply_error(bot, update, "Only voice shorter than 60s is supported. (RS04)")
        path, mime = self._download_file(update.message, update.message.reply_to_message.voice.file_id, MsgType.Audio)

        results = {}
        if len(args) == 0:
            results['Baidu (English)'] = baidu_speech.recognize(path, "en")
            results['Baidu (Mandarin)'] = baidu_speech.recognize(path, "zh")
            results['Bing (English)'] = bing_speech.recognize(path, "en-US")
            results['Bing (Mandarin)'] = bing_speech.recognize(path, "zh-CN")
            results['Bing (Japanese)'] = bing_speech.recognize(path, "ja-JP")
        elif args[0][:2] == 'zh':
            results['Baidu (Mandarin)'] = baidu_speech.recognize(path, "zh")
            if args[0] in bing_speech.lang_list:
                results['Bing (%s)' % args[0]] = bing_speech.recognize(path, args[0])
            else:
                results['Bing (Mandarin)'] = bing_speech.recognize(path, "zh-CN")
        elif args[0][:2] == 'en':
            results['Baidu (English)'] = baidu_speech.recognize(path, "en")
            if args[0] in bing_speech.lang_list:
                results['Bing (%s)' % args[0]] = bing_speech.recognize(path, args[0])
            else:
                results['Bing (English)'] = bing_speech.recognize(path, "en-US")
        elif args[0][:2] == 'ja':
            results['Bing (Japanese)'] = bing_speech.recognize(path, "ja-JP")
        elif args[0][:2] == 'ct':
            results['Baidu (Cantonese)'] = baidu_speech.recognize(path, "ct")
        elif args[0] in bing_speech.lang_list:
            results['Bing (%s)' % args[0]] = bing_speech.recognize(path, args[0])

        msg = ""
        for i in results:
            msg += "\n*%s*:\n" % i
            for j in results[i]:
                msg += "%s\n" % j
        msg = "Results:\n%s" % msg
        bot.sendMessage(update.message.reply_to_message.chat.id, msg,
                        reply_to_message_id=update.message.reply_to_message.message_id,
                        parse_mode=telegram.ParseMode.MARKDOWN)
        os.remove(path)

    def poll(self):
        """
        Message polling process.
        """
        self.bot.start_polling(network_delay=10, timeout=10)
        while True:
            try:
                m = self.queue.get()
                self.logger.info("Got message from queue\nType: %s\nText: %s\n----" % (m.type, m.text))
                threading.Thread(target=self.process_msg, args=(m,)).start()
                self.queue.task_done()
                self.logger.info("Msg sent to TG, task_done marked.")
            except Exception as e:
                self.logger.error("Error occurred during message polling")
                self.logger.error(repr(e))
                self.bot.stop()
                self.poll()

    def error(self, bot, update, error):
        """
        Print error to console, Triggered by python-telegram-bot error callback.
        """
        self.logger.warning('ERRORRR! Update %s caused error %s' % (update, error))
        import pprint
        pprint.pprint(error)

    def _flag(self, key, value):
        """
        Retrieve value for experimental flags.

        Args:
            key: Key of the flag.
            value: Default/fallback value.

        Returns:
            Value for the flag.
        """
        return config.eh_telegram_master.get('flags', dict()).get(key, value)
