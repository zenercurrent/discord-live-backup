"""Discord Channel Backup Bot

Discord bot with the purpose of backing up and storing channel message
history in case there is an event that causes the original channel to
be deleted.

Backup functions include:

Manual backup (with timestamp)
Realtime backup
"""

import asyncio
import discord
import json
import os
import re

from datetime import timedelta


class BackupBotSwarm:
    """Swarm of Backup Bots with a master controller

    For differentiating users' messages in the backup channel; a swarm of bots with each of
    them connected to a particular user can be used for easier searching.

    Each swarm will contain a master bot, which will be the only bot needed in the target guild.
    """

    def __init__(self, master: str, target_guild_id: int, target_channel_ids: list, backup_guild_id: int, swarm=None):
        """
        :param master: the bot token of the master backup bot
        :param target_guild_id: id of the guild with channels that are targeted for backup
        :param target_channel_ids: list of channel ids for targeted for backup
        :param backup_guild_id: id of the guild containing the backup
        :param swarm: a dictionary of {target user ID: bot token} to be part of the backup swarm (Note: USE OS ENV!)
        """
        if swarm is not None:
            self.__swarm = swarm
        else:
            self.__swarm = json.loads(os.environ["swarm"])

        assert self.__swarm is not None
        self.__swarm.update({"master": master})

        # create and init backup bots
        self.loop = asyncio.get_event_loop()
        self.bots = {}
        for key in self.__swarm:
            if key == "master":
                continue
            key = int(key)
            bot = BackupBot(key, self.__swarm[str(key)], backup_guild_id)
            self.bots.update({key: bot})

        self.master = BackupBotMaster(self.__swarm["master"], target_guild_id, target_channel_ids, backup_guild_id)
        self.master.bots = self.bots

    def start(self):
        """Starts the bot swarm in the asyncio loop"""
        for b in self.bots:
            b = self.bots[b]
            self.loop.create_task(b.start(b.token))
            del b.token
        self.loop.create_task(self.master.start(self.master.token))
        del self.master.token

        try:
            self.loop.run_forever()
        finally:
            self.loop.close()

    def set_admin(self, user_id: int):
        """Sets the admin of the backup swarm for mentioning in case of alarms or errors

        :param user_id: valid user id of the admin user
        """
        self.master.admin = user_id


class BackupBot(discord.Client):

    def __init__(self, target: int, token: str, backup_guild: int, **options):
        super().__init__(**options)
        self.target_id = target  # -1 if no target
        self.token = token

        self.backup_guild_id = backup_guild
        self.guild = None
        self.channels = []

    async def on_ready(self):
        self.guild = self.get_guild(self.backup_guild_id)
        self.channels = self.guild.text_channels

    async def send_message(self, channel_name: str, message="", embeds=None, files=None, stickers=None):
        """Sends a message to the specified channel

        Supported messages:
        - normal string content
        - embed(s)
        - attachment(s)
        - reactions
        - sticker(s)
        """
        if embeds is None:
            embeds = []
        if files is None:
            files = []
        if stickers is None:  # no more sticker support?
            stickers = []

        # TODO: reactions listener, role/colour change listener, pins?
        #       stats logging; live edit/delete w cache?
        #       downtime offset calc
        channel = discord.utils.find(lambda m: m.name == channel_name, self.channels)

        # convert attachments -> files
        files = [await attach.to_file() for attach in files]

        # prevent overwriting link embeds
        if "http://" in message or "https://" in message:
            embeds = []

        msg = await channel.send(content=message,
                                 embed=embeds[0] if len(embeds) > 0 else None,
                                 file=files[0] if len(files) == 1 else None,
                                 files=files if len(files) > 1 else None
                                 )
        return msg

    async def add_reaction(self, channel_name: str, emoji: discord.Emoji, message_id=None):
        """Adds a reaction to the Discord message under the bot user

        if message_id not provided, will get most recent message
        """
        channel = discord.utils.find(lambda m: m.name == channel_name, self.channels)

        assert message_id is int or message_id is None
        if message_id is None:
            message = channel.last_message
        else:
            message = await channel.fetch_message(message_id)

        await message.add_reaction(emoji)

    async def sync_profile(self, avatar=None, username=None, nickname=None, colour=None):
        """Syncs the bot's profile with the target user

        Sync-able parameters include:
        - avatar
        - username (excluding discriminator)
        - nickname
        - colour (based on role)
        """
        if avatar is not None:
            await self.user.edit(avatar=avatar)

        if username is not None:
            await self.user.edit(username=username + "_bot")

        if nickname is not None:
            me = self.guild.get_member(self.user.id)
            await me.edit(nick=nickname)

        if colour is not None:
            await self.guild.self_role.edit(colour=colour)


class BackupBotMaster(BackupBot):

    def __init__(self, token: str, target_guild: int, target_channels: list, backup_guild: int):
        super().__init__(-1, token, backup_guild)

        self.target_guild_id = target_guild
        self.target_channel_ids = target_channels
        self.target_guild = None
        self.target_channels = []
        self.console = None
        self.database = None

        # notable channels, name changeable
        self.console_name = "terminal"
        self.database_name = "cache"

        self.bots = {}  # index of backup bots (excluding master)
        self.targets = {}  # index of users that are targeted
        self.roles = {}  # index of roles in backup channel based on their names

        self.CACHE_SIZE = 50  # sets the size of the link cache
        self.link_cache = {}  # cache of message ids that links: target message -> backup message

        self.unknown_emoji = None
        self.time_offset = 0  # can change this based on desired timezone offset (UTC)

        self.__debug_pt = None  # for dev debug
        self.admin = None  # set user to ping for alarms

    async def on_ready(self):
        await super().on_ready()
        self.target_guild = self.get_guild(self.target_guild_id)
        self.console = self.__fetch_text_channel(self.console_name)
        self.database = self.__fetch_text_channel(self.database_name)
        for i in self.target_channel_ids:
            channel = self.get_channel(i)
            self.target_channels.append(channel)

            # create channels if doesn't exist
            c = discord.utils.find(lambda m: m.name == channel.name, self.channels)
            if c is None:
                await self.guild.create_text_channel(channel.name)

        # get admin user (if set)
        if self.admin is not None:
            self.admin = self.get_user(self.admin)

        # generate index of target users
        for i in self.bots:
            member = await self.target_guild.fetch_member(i)
            self.targets.update({i: member})

        # generate index of roles
        for r in self.guild.roles:
            self.roles.update({r.name: r})

        # "unknown" default emoji for importing custom emoji
        _emojis = await self.guild.fetch_emojis()
        unknown_emoji = discord.utils.find(lambda e: e.name == "unknown_emoji", _emojis)
        if unknown_emoji is None:
            path = os.path.dirname(os.path.abspath(__file__)) + "\\unknown_emoji.png"
            file = open(path, "rb")
            data = file.read()
            file.close()

            unknown_emoji = await self.guild.create_custom_emoji(name="unknown_emoji",
                                                                 image=data,
                                                                 roles=[],
                                                                 reason="\"unknown\" default emoji for importing "
                                                                        "custom emojis")
        self.unknown_emoji = unknown_emoji

    # on_message event listener
    async def on_message(self, message):
        """Listener for messages from target channels and routes them to appropriate bots

            Also has a function to receive commands from a console channel. (if console_channel param is set)
            This is used to manually run certain actions with the bot swarm.
        """
        if message.author == self.user or message.channel.id not in self.target_channel_ids + [self.console.id]:
            return

        if message.channel.id != self.console.id:
            await self.__send(message, realtime=True)
            return

        """Console - execute commands to the bot swarm manually"""

        if message.content == "sync profiles":
            # Sync Profiles - manually sync up the username, avatar and nickname of target to the bot

            print("Command: sync profiles")
            await message.reply("Syncing profile avatars and usernames...")

            for b in self.bots:
                member = self.targets[b]
                avatar = await member.avatar_url.read()
                username = member.name
                nickname = member.nick
                await self.bots[b].sync_profile(avatar=avatar, username=username, nickname=nickname)

            await self.console.send("Syncing complete.")

        elif message.content == "sync roles":
            # Sync Roles - manually sync up roles/colours from target guild to backup guild
            # (roles will not be given to bots, just used for tagging)

            print("Command: sync roles")
            await message.reply("Syncing roles...")

            # create role if doesn't exist
            roles = self.target_guild.roles
            backup_role_names = [_r.name for _r in self.guild.roles]
            for role in roles:
                if role.name not in backup_role_names:
                    await self.guild.create_role(
                        name=role.name,
                        colour=role.colour,
                        mentionable=True,
                        reason="Auto role syncing"
                    )

            # sync target user colours
            for b in self.bots:
                colour = self.targets[b].colour
                await self.bots[b].sync_profile(colour=colour)

            await self.console.send("Syncing complete.")

        elif str(message.content).startswith("get message "):
            # Get Message - helper to get message info based on valid message_id

            print("Command: get message")
            message_id = str(message.content).replace("get message ", "", 1)
            print("message_id ->", message_id)

            message = None
            try:
                message = await self.__get(message_id)
            except self.CommandException:
                return

            print(message)
            self.__debug_pt = message
            await self.console.send(
                f"{message}\ncontent: {message.content}\nembeds: {message.embeds}\nattachments: {message.attachments}")

        elif str(message.content).startswith("manual import "):
            # Manual Import - manually import and routes the messages from the starting point to latest message
            # (requires the message id of the message to start from as a parameter)

            print("Command: manual import")
            message_id = str(message.content).replace("manual import ", "", 1)
            print("message_id ->", message_id)

            await message.reply("Starting manual import operation")
            await self.console.send("Searching for target message with message id: " + message_id)

            # find starting point message
            try:
                starting_point = await self.__get(message_id)
            except self.CommandException:
                return

            await self.console.send(
                f"Message for starting point is found, with content: '{starting_point.content}' from channel: `#{starting_point.channel}`")
            await self.console.send("Proceed with mass import? (yes to continue)")
            confirm = None
            try:
                confirm = await self.wait_for("message", check=lambda m: m.channel == message.channel, timeout=60.0)
            except asyncio.TimeoutError:
                await self.console.send("Cancelling manual import operation")
            if confirm.content != "yes":
                await self.console.send("Cancelling manual import operation")
                return

            # start import
            await self.console.send("Starting importing procedure...")
            counter = 0
            async for import_message in starting_point.channel.history(limit=None, oldest_first=True,
                                                                       after=starting_point):
                await self.__send(import_message)
                counter += 1

            await self.console.send("Importing successful. Total messages imported: " + str(counter))

    # on_message_edit event listener
    async def on_message_edit(self, before, after):

        # get message, match with cache, edit message
        pass

    # on_message_delete event listener
    async def on_message_delete(self, message):

        # get message, match with cache, delete if same author
        pass

    # on_user_update event listener
    async def on_user_update(self, before: discord.User, after: discord.User):
        """Listener for user avatar and username updates and activate profile syncing of the targeted bot"""
        avatar = None
        username = None
        user_id = after.id

        if user_id not in list(self.bots.keys()):
            return

        if before.avatar != after.avatar:
            avatar = after.avatar
        if before.display_name != after.display_name:
            username = after.display_name

        await self.bots[user_id].sync_profile(avatar=avatar, username=username)

    # on_member_update event listener
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Listener for member nickname updates and activate nickname syncing of the targeted bot"""
        nick = None
        user_id = after.id

        if user_id not in list(self.bots.keys()):
            return

        if before.nick != after.nick:
            nick = after.nick

        await self.bots[user_id].sync_profile(nickname=nick)

    async def __get(self, message_id: str):
        """Get the Message object based on message id, throws exception if not found"

        :return message: the queried Message object
        """
        if not message_id.isdigit():
            await self._raise("message_id is not the right format")
        message_id = int(message_id)

        message = None
        found = False
        for c in self.target_channels:
            try:
                message = await c.fetch_message(message_id)
            except discord.NotFound:
                continue
            else:
                found = True
                break
        if not found:
            await self._raise("message was not found")
            return None

        return message

    async def __send(self, message: discord.Message, realtime=False):
        """Determines message content and routes it to appropriate bot

        Also does a few things as master:
        - changes user mentions to the corresponding bot user mention
        - clones the reactions with correct routing

        :param message: Message object for bot to send
        :param realtime: flag to set True if message is from on_message
        """
        content = message.content

        # convert user mentions to bot user mention
        matches = re.findall("<@\d+>", content)
        for m in matches:
            user_id = int(m[2:-1])
            bot = self.bots.get(user_id, None)
            if bot is not None:
                content = content.replace(m, bot.user.mention, 1)

        # convert role mentions to backup guild role mention
        matches = re.findall("<@&\d+>", content)
        for m in matches:
            role_id = int(m[3:-1])
            target_role = self.target_guild.get_role(role_id)
            role = self.roles.get(target_role.name, None)
            if role is not None:
                content = content.replace(m, role.mention, 1)

        # neuter @here and @everyone tags (to prevent spam)
        content = content.replace("@here", "@/here").replace("@everyone", "@/everyone")

        content = "\n" + content

        # add message metadata if imported
        if not realtime:
            dt = "*[" + (message.created_at + timedelta(hours=self.time_offset)).strftime("%m/%d/%Y %I:%M%p") + "]*\n"
            content = dt + content

        bot = self.bots.get(message.author.id, self)

        # add original author metadata if default backup bot
        if bot.user.id == self.user.id:
            content = content + f"*[Message Author: @/{message.author.name}#{message.author.discriminator}]*\n"

        backup_message = await bot.send_message(channel_name=message.channel.name,
                                                message=content,
                                                embeds=message.embeds,
                                                files=message.attachments,
                                                stickers=message.stickers)

        # import and clone reactions
        reactions = message.reactions
        unknown_reactions = {}
        unknown_reactors = {}

        REACTED_UNKNOWN = []  # to prevent un-needed reacting
        for r in reactions:
            BOT_REACTED = False
            BOT_REACTED_UNKNOWN = False

            async for r_user in r.users():
                bot = self.bots.get(r_user.id, self)
                r_emoji = r.emoji
                if r_emoji is discord.Emoji or r_emoji is discord.PartialEmoji:
                    r_emoji = r_emoji.name
                try:
                    if bot.user.name == self.user.name:
                        unknown_reactors.update({r_emoji: unknown_reactors.get(r_emoji, 0) + 1})
                        if BOT_REACTED:
                            continue
                        else:
                            BOT_REACTED = True

                    await bot.add_reaction(message.channel.name, r.emoji)

                except (discord.HTTPException, discord.NotFound):
                    if bot.user.id in REACTED_UNKNOWN and bot.user.id != self.user.id:
                        continue
                    REACTED_UNKNOWN.append(bot.user.id)

                    if bot.user.id == self.user.id and not BOT_REACTED_UNKNOWN:
                        BOT_REACTED_UNKNOWN = True
                    else:
                        continue

                    await bot.add_reaction(message.channel.name, self.unknown_emoji)
                    unknown_reactions.update(
                        {r_emoji: unknown_reactions.get(r_emoji, []) + [bot.user.mention]})

        r_metadata = "\n\n*__Unknown Reactions:__*" if (len(unknown_reactions) > 0 or len(unknown_reactors) > 0) else ""
        for u_emoji in unknown_reactions:
            u_reactors_count = unknown_reactions[u_emoji].count(self.user.mention)
            known_reactors = list(filter(lambda _u: _u != self.user.mention, unknown_reactions[u_emoji]))
            r_metadata += f"\n*[{u_emoji} -> {', '.join(known_reactors)} " \
                          f"{'+' if (len(known_reactors) > 0 and u_reactors_count > 0) else ''} " \
                          f"{(str(u_reactors_count) + ' unknown user(s)') if u_reactors_count > 0 else ''}]*"
        for u_emoji in unknown_reactors:
            r_metadata += f"\n*[{u_emoji} -> +{unknown_reactors[u_emoji]} unknown user(s)]*"

        if r_metadata != "":
            await backup_message.edit(content=backup_message.content + r_metadata)

    async def _raise(self, message: str):
        """
        :param message: message describing the error
        """
        # TODO: client.on_error listener may be better?
        await self.console.send(f"CommandException: {message}")
        raise self.CommandException(message)

    async def __fetch_text_channel(self, channel_name: str):
        """return text channel based on name in backup guild, if missing create channel"""
        channel = discord.utils.find(lambda c: c.name == channel_name, self.guild.text_channels)
        if channel is None:
            channel = await self.guild.create_text_channel(channel_name)
        return channel

    class CommandException(Exception):
        """Raise for incorrect command parameters or command failures"""

        def __init__(self, message: str):
            print(f"CommandException: {message}")

    def __admin(self):
        """get mention of admin user if not None"""
        if self.admin is not None:
            return self.admin.mention
        else:
            return ""
