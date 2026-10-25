from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

VERSION = 'discovery-pool-v1'
KIND = 'daily_discovery'
LIMITS = dict(candidates=100, search_rounds=5, model_calls=16, tokens=60000,
              decisions=2, network_requests=160, seconds=180)
TARGET = 6
DAILY_LIMIT = None
POOL_LIMIT = 300
LOW_WATER = 30
REFILL_TARGET = 100
REFILL_INTERVAL = 1800
VALIDATION_TTL = 3600
CHANGE_INTERVAL = 3
PROFILE_VERSION = 'profile-v1'
MIN_SCORE = .45
WEIGHTS = (.55, .20, .15, .10)
ZONE = ZoneInfo('Asia/Shanghai')


def today(now=None):
    return datetime.fromtimestamp(now, ZONE).date() if now is not None else datetime.now(ZONE).date()


def day_end(now):
    return datetime.combine(today(now) + timedelta(days=1), datetime.min.time(), ZONE).timestamp()
