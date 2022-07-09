import discord
import requests
import asyncio

STAT_TITLES = {
    "Total Messages Sent": lambda m: 1,
    "Total Attachments Sent": lambda m: len(m.attachments) if len(m.attachments) > 0 else None
}


# custom create_thread func since v1.0 unsupported
async def create_thread(channel: discord.TextChannel, name: str):
    token = 'Bot ' + channel._state.http.token
    url = f"https://discord.com/api/v9/channels/{channel.id}/threads"
    headers = {
        "authorization": token,
        "content-type": "application/json"
    }
    data = {
        "name": name,
        "type": 11,
        "auto_archive_duration": 10080  # one week
    }

    res = requests.post(url, headers=headers, json=data).json()
    await ([m async for m in channel.history(limit=1)][0]).delete()  # remove thread creation message
    return res


# custom fetch_threads func since v1.0 unsupported
async def fetch_all_stats_threads(guild: discord.Guild, backup_channel_ids: list):
    token = 'Bot ' + guild._state.http.token
    url = f"https://discord.com/api/v9/guilds/{guild.id}/threads/active"
    headers = {
        "authorization": token,
        "content-type": "application/json"
    }
    res = requests.get(url, headers=headers).json()["threads"]

    threads = dict.fromkeys(backup_channel_ids, {})
    stat_titles = tuple(STAT_TITLES.keys())
    for t in res:
        if str(t["name"]).startswith(stat_titles) and int(t["parent_id"]) in backup_channel_ids:
            cate = stat_titles[[i for i, s in enumerate(stat_titles) if str(t["name"]).startswith(s)][0]]
            threads[int(t["parent_id"])].update({cate: str(t["id"])})

    return threads


# custom fetch_thread func since v1.0 unsupported
async def fetch_thread(guild: discord.Guild, thread_id: int):
    token = 'Bot ' + guild._state.http.token
    url = f"https://discord.com/api/v9/channels/{thread_id}"
    headers = {
        "authorization": token,
        "content-type": "application/json"
    }
    res = requests.get(url, headers=headers).json()
    return res


# custom rename_thread func since v1.0 unsupported
async def rename_thread(guild: discord.Guild, thread_id: int, name: str):
    token = 'Bot ' + guild._state.http.token
    url = f"https://discord.com/api/v9/channels/{thread_id}"
    headers = {
        "authorization": token,
        "content-type": "application/json"
    }
    data = {
        "name": name
    }
    res = requests.patch(url, headers=headers, json=data).json()
    return res


class ChannelStatsLogger:
    """Manages the logging of the stats of the channel (total messages, text messages, images)

    Each channel's stats will be displayed as the name of a locked thread.
    Stats are updated in set intervals to prevent getting rate-limited.
    """

    def __init__(self, master):
        self.threads = {}
        self.cache = {}
        self.master = master

        loop = asyncio.get_running_loop()
        loop.create_task(self.setup())

    async def update(self, channel_id: int, topic: str, value: int, incre=True):
        """Updates the stat thread by renaming the appropriate thread based on channel and topic.

        :param channel_id: id of the affected channel
        :param topic: topic to increment
        :param value: value to be updated to

        :param incre: flag to set for value to be incremented from previous value, if not replaces with new value
        """
        threads = self.threads.get(channel_id)
        assert threads is not None
        _id = threads.get(topic)
        assert _id is not None

        # increment mode
        if incre:
            prev = int((await fetch_thread(self.master.guild, _id))["name"].replace(f"{topic} - ", "", 1))
            value += prev

        await rename_thread(self.master.guild, _id, f"{topic} - {str(value)}")

    def check(self, message: discord.Message):
        """Loops through the set STAT_TITLES functions and increments stat counters based on conditions"""
        for st in STAT_TITLES:
            func = STAT_TITLES[st]
            incre = func(message)
            if incre is not None:
                self.cache[st] += incre
                print(f"[DEBUG] Updated <{st}> by {incre}")

    async def setup(self):
        """Run setup before using ChannelStatsLogger"""
        self.threads = await fetch_all_stats_threads(self.master.guild, self.master.backup_channel_ids)
        self.cache = dict.fromkeys(STAT_TITLES.keys(), 0)

        # create stat thread if doesn't exist
        for b in self.master.backup_channels:
            for s in STAT_TITLES:
                if self.threads[b.id].get(s) is None:
                    print(f"Stat Thread ({b.name}:{s}) not found. Creating stat thread...")
                    t = await create_thread(b, s + " - 0")
                    self.threads[b.id].update({s: t["id"]})

    def log(self):
        pass


class LoggedChannel:
    """Indicates a channel that is logged by the ChannelStatsLogger.
    Makes it more convenient to manage the routing of different channel by the logger.
    """

    def __init__(self, channel: discord.TextChannel):
        self.channel = channel

        threads = self.channel.threads
        self.threads = {}  # stores the threads of the logger
