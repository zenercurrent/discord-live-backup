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

    async def send_message(self, channel_name: str, message=None, embeds=None, files=None, stickers=None):
        """Sends a message to the specified channel

        Supported messages:
        - normal string content
        - embed(s)
        - attachment(s)
        - sticker(s)
        """
        if embeds is None:
            embeds = []
        if files is None:
            files = []
        if stickers is None:
            stickers = []

        channel = discord.utils.find(lambda m: m.name == channel_name, self.channels)
        await channel.send(content=message,
                           embed=embeds if 0 < len(embeds) < 2 else None,
                           embeds=embeds if len(embeds) > 1 else None,
                           file=files if 0 < len(files) < 2 else None,
                           files=files if len(files) > 1 else None,
                           stickers=stickers
                           )

    async def sync_profile(self, avatar=None, username=None, nickname=None):
        """Syncs the bot's username and avatar with the target user"""
        if avatar is not None:
            await self.user.edit(avatar=avatar)
        if username is not None:
            await self.user.edit(username=username)
        if nickname is not None:
            me = self.guild.get_member(self.user.id)
            await me.edit(nick=nickname)


class BackupBotMaster(BackupBot):

    def __init__(self, token: str, target_guild: int, target_channels: list, backup_guild: int,
                 console_channel=968231979701137428):
        super().__init__(-1, token, backup_guild)

        self.target_guild_id = target_guild
        self.target_channel_ids = target_channels
        self.target_guild = None
        self.target_channels = []
        self.console_channel_id = console_channel
        self.console = None

        self.bots = {}  # index of backup bots (excluding master)
        self.targets = {}  # index of users that are targeted

    async def on_ready(self):
        await super().on_ready()
        self.target_guild = self.get_guild(self.target_guild_id)
        self.console = self.guild.get_channel(self.console_channel_id)
        for i in self.target_channel_ids:
            channel = self.get_channel(i)
            self.target_channels.append(channel)

            # create channels if doesn't exist
            c = discord.utils.find(lambda m: m.name == channel.name, self.channels)
            if c is None:
                await self.guild.create_text_channel(channel.name)

        # generate index of target users
        for i in self.bots:
            member = await self.target_guild.fetch_member(i)
            self.targets.update({i: member})

    async def on_message(self, message):
        """Listener for messages from target channels and routes them to appropriate bots

            Also has a function to receive commands from a console channel. (if console_channel param is set)
            This is used to manually run certain actions with the bot swarm.
        """
        if message.author == self.user or message.channel.id not in self.target_channel_ids + [self.console_channel_id]:
            return

        if message.channel.id != self.console_channel_id:
            await self.__send(message)
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

        elif str(message.content).startswith("manual import "):
            # Manual Import - manually import and routes the messages from the starting point to latest message
            # (requires the message id of the message to start from as a parameter)

            print("Command: manual import")
            message_id = str(message.content).replace("manual import ", "", 1)
            print("message_id ->", message_id)

            if not message_id.isdigit():
                await self._raise("message_id is not the right format")
            message_id = int(message_id)

            await message.reply("Starting manual import operation")
            await self.console.send("Searching for target message with message id: " + str(message_id))

            # find starting point message
            found = False
            starting_point = None
            for c in self.target_channels:
                try:
                    starting_point = await c.fetch_message(message_id)
                except discord.NotFound:
                    continue
                else:
                    found = True
                    break
            if not found:
                await self._raise("message was not found")
                return
            await self.console.send(
                f"Message for starting point is found, with content: '{starting_point.content}' from channel: `#{starting_point.channel}`")
            await self.console.send("Proceed with mass import? (yes to continue)")
            confirm = None
            try:
                confirm = await self.wait_for("message", check=lambda m: m.channel == message.channel, timeout=60.0)
            except asyncio.TimeoutError:
                pass
            if confirm.content != "yes":
                await self.console.send("Cancelling manual import operation")
                return

            # start import
            await self.console.send("Starting importing procedure...")
            counter = 0
            async for import_message in starting_point.channel.history(limit=None):
                await self.__send(import_message)
                counter += 1

            await self.console.send("Importing successful. Total messages imported: " + str(counter))

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

    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Listener for member nickname updates and activate nickname syncing of the targeted bot"""
        nick = None
        user_id = after.id

        if user_id not in list(self.bots.keys()):
            return

        if before.nick != after.nick:
            nick = after.nick

        await self.bots[user_id].sync_profile(nickname=nick)

    async def __send(self, message: discord.Message):
        """Determines message content and routes it to appropriate bot"""
        await self.bots.get(message.author.id, self).send_message(message.channel,
                                                                  message=message.content if message.content != "" else None,
                                                                  embeds=message.embeds,
                                                                  files=message.attachments,
                                                                  stickers=message.stickers)

    async def _raise(self, message: str):
        """
        :param message: message describing the error
        """
        await self.console.send(f"CommandException: {message}")
        raise self.CommandException(message)

    class CommandException(Exception):
        """Raise for incorrect command parameters or command failures"""

        def __init__(self, message: str):
            print(f"CommandException: {message}")
