"""Discord Channel Backup Bot

Discord bot with the purpose of backing up and storing channel message
history in case there is an event that causes the original channel to
be deleted.

Backup functions include:

Manual backup (with timestamp)
Realtime backup
"""

# TODO: (get working product ASAP)
#   1. Manual Batch Backup
#   2. Realtime Live Backup - listeners
#   3. Server with backup-ped channels
#   4. Format of message (webhook vs normal bot message)
#   5. Message cache in case of edit/delete
#   .
#   Resources:
#   https://github.com/Rapptz/discord.py/issues/516

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
            bot = BackupBot(key, self.__swarm[key], backup_guild_id)
            self.bots.update({key: bot})

        self.master = BackupBotMaster(self.__swarm["master"], target_guild_id, target_channel_ids, backup_guild_id)
        self.master.bots = self.bots

    def start(self):
        """Starts the bot swarm in the asyncio loop"""

        for b in self.bots:
            b = self.bots[b]
            self.loop.create_task(b.start(b.token))
        self.loop.create_task(self.master.start(self.master.token))

        try:
            self.loop.run_forever()
        finally:
            self.loop.close()


class BackupBot(discord.Client):

    def __init__(self, target: int, token: str, backup_guild: int, **options):
        super().__init__(**options)
        self.target = target  # -1 if no target
        self.token = token

        self.backup_guild_id = backup_guild
        self.guild = None
        self.channels = []

    async def on_ready(self):
        self.guild = self.get_guild(self.backup_guild_id)
        self.channels = self.guild.text_channels

    async def send_message(self, message, channel_name):
        channel = discord.utils.find(lambda m: m.name == channel_name, self.channels)
        await channel.send(message)


class BackupBotMaster(BackupBot):

    def __init__(self, token: str, target_guild: int, target_channels: list, backup_guild: int):
        super().__init__(-1, token, backup_guild)

        self.target_guild_id = target_guild
        self.target_channel_ids = target_channels

        self.target_guild = None
        self.target_channels = []

        self.bots = None  # list of backup bots (not including master)

    async def on_ready(self):
        await super().on_ready()
        self.target_guild = self.get_guild(self.target_guild_id)
        for i in self.target_channel_ids:
            channel = self.get_channel(i)
            self.target_channels.append(channel)

            # create channels if doesn't exist
            c = discord.utils.find(lambda m: m.name == channel.name, self.channels)
            if c is None:
                await self.guild.create_text_channel(channel.name)

    async def on_message(self, message):
        if message.author == self.user or message.channel.id not in self.target_channel_ids:
            return
        await self.bots.get(message.author.id, self).send_message(message.content, message.channel.name)


if __name__ == "__main__":
    y = {278102709804990466: "OTY3OTE5NjExMTc5ODQ3Nzgy.YmXTYg.Srd72_YoKaNZMYPXZsyCBa1f6-k"}
    x = BackupBotSwarm("OTY3OTIyNDcwODk0MDUxMzI4.YmXWDA.uXy6WG3_kzuaLpi8oPtWZKy3rbs", 696756360787787837,
                       [765950803453411349], 968010319299481611, swarm=y)
