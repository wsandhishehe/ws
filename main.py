"""Send the daily WeChat greeting, with fallbacks for optional content."""
import argparse
import json
import logging
import math
import os
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOGGER = logging.getLogger(__name__)
BEIJING_TIME = timezone(timedelta(hours=8))
TIMEOUT = (5, 15)
WEATHER_FALLBACK = ("天气暂时无法获取", "--", "--", "--")
WORDS_FALLBACK = "早安，愿你今天也有好心情。"
WEATHER_CODES = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴",
    45: "雾", 48: "冻雾", 51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
    56: "冻毛毛雨", 57: "冻毛毛雨", 61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "冻雨", 71: "小雪", 73: "中雪", 75: "大雪",
    77: "雪粒", 80: "小阵雨", 81: "阵雨", 82: "强阵雨",
    85: "阵雪", 86: "强阵雪", 95: "雷雨", 96: "雷雨伴冰雹", 99: "雷雨伴冰雹",
}


@dataclass(frozen=True)
class Config:
    app_id: str
    app_secret: str
    template_id: str
    user_ids: tuple[str, ...]
    start_date: date
    birthday: tuple[int, int]
    city: str
    coordinates: str = ""


def load_config(env=None, dry_run=False):
    env = os.environ if env is None else env
    required = ["START_DATE", "BIRTHDAY", "CITY"]
    if not dry_run:
        required += ["APP_ID", "APP_SECRET", "TEMPLATE_ID", "USER_ID"]
    missing = [name for name in required if not env.get(name, "").strip()]
    if missing:
        raise ValueError("缺少配置，请检查 GitHub Actions Secrets：" + ", ".join(missing))
    try:
        start = datetime.strptime(env["START_DATE"].strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("START_DATE 必须是有效日期，格式为 YYYY-MM-DD") from None
    try:
        birthday = datetime.strptime("2000-" + env["BIRTHDAY"].strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("BIRTHDAY 必须是有效生日，格式为 MM-DD") from None
    users = tuple(dict.fromkeys(user.strip() for user in env.get("USER_ID", "").splitlines() if user.strip()))
    return Config(
        env.get("APP_ID", "").strip(), env.get("APP_SECRET", "").strip(),
        env.get("TEMPLATE_ID", "").strip(), users, start,
        (birthday.month, birthday.day), env["CITY"].strip(),
        env.get("CITY_COORDINATES", "").strip(),
    )


def create_session():
    session = requests.Session()
    # Retry only read-only requests. Retrying a message POST could send duplicates.
    retry = Retry(total=2, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET"])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def get_coordinates(session, city, coordinates=""):
    if coordinates:
        latitude, longitude = (float(part.strip()) for part in coordinates.split(","))
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError("CITY_COORDINATES 超出经纬度范围")
        return latitude, longitude
    # Some cities are indexed with 市 (承德市), others without it (北京).
    name = city[:-1] if city.endswith("市") else city
    for query in dict.fromkeys([city, name, name + "市"]):
        response = session.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": query, "count": 10, "language": "zh", "countryCode": "CN"},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        candidates = [item for item in response.json().get("results", [])
                      if item.get("country_code") == "CN" and item.get("population", 0) > 0]
        if candidates:
            place = max(candidates, key=lambda item: item.get("population", 0))
            return place["latitude"], place["longitude"]
    raise ValueError("找不到城市，请检查 CITY 或填写 CITY_COORDINATES")


def get_weather(session, city, coordinates=""):
    try:
        latitude, longitude = get_coordinates(session, city, coordinates)
        response = session.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": latitude, "longitude": longitude,
                    "current": "temperature_2m,weather_code",
                    "daily": "temperature_2m_max,temperature_2m_min",
                    "timezone": "Asia/Shanghai", "forecast_days": 1},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        body = response.json()
        current, daily = body["current"], body["daily"]
        return (WEATHER_CODES.get(current["weather_code"], "未知天气"),
                math.floor(current["temperature_2m"]),
                math.floor(daily["temperature_2m_max"][0]),
                math.floor(daily["temperature_2m_min"][0]))
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError, AttributeError, OverflowError):
        LOGGER.warning("天气查询失败，使用备用文本；检查 CITY、CITY_COORDINATES 或天气服务。")
        return WEATHER_FALLBACK


def get_words(session):
    try:
        response = session.get("https://api.shadiao.pro/chp", timeout=TIMEOUT)
        response.raise_for_status()
        text = response.json()["data"]["text"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Empty greeting")
        return text.strip()
    except (requests.RequestException, ValueError, KeyError, TypeError):
        LOGGER.warning("早安文案接口暂不可用，使用备用文案。")
        return WORDS_FALLBACK


def get_birthday(birthday, today):
    month, day = birthday
    year = today.year
    while True:
        try:
            upcoming = date(year, month, day)
        except ValueError:
            # A February 29 birthday counts down to the next real February 29.
            year += 1
            continue
        if upcoming >= today:
            return (upcoming - today).days
        year += 1


def build_data(config, session, today=None):
    today = datetime.now(BEIJING_TIME).date() if today is None else today
    weather, temperature, highest, lowest = get_weather(session, config.city, config.coordinates)
    values = {
        "date": today.isoformat(), "weather": weather, "temperature": temperature,
        "highest": highest, "lowest": lowest,
        "love_days": (today - config.start_date).days,
        "birthday_left": get_birthday(config.birthday, today), "words": get_words(session),
    }
    return {key: {"value": str(value), "color": "#%06x" % random.randint(0, 0xFFFFFF)}
            for key, value in values.items()}


def get_access_token(session, config):
    response = session.get(
        "https://api.weixin.qq.com/cgi-bin/token",
        params={"grant_type": "client_credential", "appid": config.app_id, "secret": config.app_secret},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("微信认证响应格式异常，请检查微信服务。")
    if not body.get("access_token"):
        raise ValueError("微信获取 access_token 失败，错误码：%s；检查 APP_ID、APP_SECRET 和 IP 白名单。"
                         % body.get("errcode", "unknown"))
    return body["access_token"]


def send_messages(session, config, data):
    token = get_access_token(session, config)
    success, failed = 0, 0
    for index, user_id in enumerate(config.user_ids, 1):
        try:
            response = session.post(
                "https://api.weixin.qq.com/cgi-bin/message/template/send",
                params={"access_token": token},
                json={"touser": user_id, "template_id": config.template_id, "data": data},
                timeout=TIMEOUT,
            )
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("Invalid WeChat message response")
            if body.get("errcode") != 0:
                failed += 1
                LOGGER.error("第 %d 个接收人发送失败，微信错误码：%s；检查 USER_ID、关注状态和 TEMPLATE_ID。",
                             index, body.get("errcode", "unknown"))
            else:
                success += 1
        except (requests.RequestException, ValueError, TypeError):
            failed += 1
            LOGGER.error("第 %d 个接收人请求失败，请检查微信服务和网络。", index)
    print("成功发送 %d 条消息，失败 %d 条" % (success, failed))
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="预览内容，不调用微信接口")
    mode.add_argument("--check", action="store_true", help="验证配置与微信认证，不发送消息")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        config = load_config(dry_run=args.dry_run)
        with create_session() as session:
            data = build_data(config, session)
            if args.check:
                get_access_token(session, config)
                print("配置与微信认证检查通过，未发送消息。")
                return 0
            if args.dry_run:
                print("预览模式，未发送微信消息：")
                print(json.dumps(data, ensure_ascii=False, indent=2))
                return 0
            return send_messages(session, config, data)
    except requests.RequestException:
        # HTTP exception URLs may contain WeChat credentials; do not log them.
        LOGGER.error("微信请求失败，请检查网络、APP_ID、APP_SECRET 和微信服务。")
        return 1
    except ValueError as error:
        LOGGER.error("%s", error)
        return 1
    except (KeyError, TypeError):
        LOGGER.error("微信响应格式异常，请检查微信服务。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
