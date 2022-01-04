from enum import Enum
from yutto.utils.fetcher import MaxRetry
from aiohttp import ClientSession


class Conveter(Enum):
    # （简体化）
    Simplified = "Simplified"
    # （繁体化）
    Traditional = "Traditional"
    # （中国化）
    China = "China"
    # （香港化）
    Hongkong = "Hongkong"
    # （台湾化）
    Taiwan = "Taiwan"
    # （拼音化）
    Pinyin = "Pinyin"
    # （注音化）
    Bopomofo = "Bopomofo"
    # （火星化）
    Mars = "Mars"
    # （维基简体化）
    WikiSimplified = "WikiSimplified"
    # （维基繁体化）
    WikiTraditional = "WikiTraditional"


@MaxRetry(2)
async def translate(text: str, conveter: Conveter = Conveter.China) -> str:
    api = "https://api.zhconvert.org/convert"
    params = {"converter": conveter.value, "text": text, "trimTrailingWhiteSpaces": True}
    async with ClientSession() as session:
        async with session.post(api, json=params) as resp:
            json = await resp.json()
            return json["data"]["text"]
